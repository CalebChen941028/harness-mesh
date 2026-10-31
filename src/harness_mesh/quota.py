"""Quota exhaustion detection, three-state circuits and recovery probing.

Detection follows the split used by dsh-superagent's ``retry-health`` module:

* account-wide hard budget exhaustion (``budget_exceeded`` / 402 /
  ``insufficient balance``) trips the ``(harness, "*")`` circuit;
* model-scoped rate/quota exhaustion (429 / ``usage limit`` /
  ``RESOURCE_EXHAUSTED``) trips ``(harness, model)`` so the router can first try
  another authorised model on the same harness before crossing harnesses.

Transient errors (408/5xx/network jitter) are never treated as quota events.
A circuit only closes when a probe or a real successful execution proves that
the provider is serving requests again.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
from typing import Any, Awaitable, Callable, Iterable, Sequence

from .models import (
    CircuitState,
    DiscoveredModel,
    QuotaCircuitRecord,
)
from .storage import StateStore

_ACCOUNT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"budget[_ ]exceeded", re.I), "budget_exceeded"),
    (re.compile(r"budget has been exceeded", re.I), "budget_exceeded"),
    (re.compile(r"credit balance is too low", re.I), "credit_balance_too_low"),
    (re.compile(r"insufficient balance", re.I), "insufficient_balance"),
    (re.compile(r"insufficient credits?", re.I), "insufficient_credits"),
    (re.compile(r"billing[_ ]hard[_ ]limit[_ ]reached", re.I), "billing_hard_limit_reached"),
    (re.compile(r"payment required", re.I), "payment_required"),
    (re.compile(r"\b402\b"), "http_402_payment_required"),
    (re.compile(r"account (?:is )?out of credits?", re.I), "insufficient_credits"),
]

_MODEL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b429\b"), "rate_limit_429"),
    (re.compile(r"too many requests", re.I), "rate_limit_429"),
    (re.compile(r"rate[ _-]?limit", re.I), "rate_limit_exceeded"),
    (re.compile(r"usage limit", re.I), "usage_limit"),
    (re.compile(r"insufficient[_ ]quota", re.I), "insufficient_quota"),
    (re.compile(r"resource[ _]?exhausted", re.I), "resource_exhausted"),
    (re.compile(r"quota[ _-]?exceeded", re.I), "quota_exceeded"),
    (re.compile(r"overloaded(?:[_ ]error)?", re.I), "overloaded_error"),
    (re.compile(r"throttl(?:ed|ing)", re.I), "throttled"),
]


def _combined(stdout: str | None, stderr: str | None, exit_code: int | None) -> str:
    parts = [stdout or "", stderr or ""]
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    return "\n".join(parts)


def parse_retry_after(text: str) -> int | None:
    """Extract a cooldown hint from headers or provider prose."""

    patterns = [
        re.compile(
            r"retry[-_ ]after['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)"
            r"\s*(ms|s|sec(?:onds?)?|m|min(?:utes?)?|h|hours?)?",
            re.I,
        ),
        re.compile(
            r"retry after\s+(\d+(?:\.\d+)?)\s*(ms|s|sec(?:onds?)?|m|min(?:utes?)?|h|hours?)?",
            re.I,
        ),
        re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*(ms|s|sec(?:onds?)?|m|min(?:utes?)?|h|hours?)", re.I),
        re.compile(r"resets? (?:after|in)\s+(\d+(?:\.\d+)?)\s*(ms|s|sec(?:onds?)?|m|min(?:utes?)?|h|hours?)", re.I),
        re.compile(r"wait\s+(\d+(?:\.\d+)?)\s*(ms|s|sec(?:onds?)?|m|min(?:utes?)?|h|hours?)", re.I),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        value = float(match.group(1))
        unit = (match.group(2) or "s").lower()
        if unit == "ms":
            seconds = value / 1000.0
        elif unit.startswith("m") and unit not in {"ms"}:
            seconds = value * 60.0
        elif unit.startswith("h"):
            seconds = value * 3600.0
        else:
            seconds = value
        return max(0, int(math.ceil(seconds)))
    return None


class QuotaErrorDetector:
    """Stateless text/exit-code classifier for quota and auth failures."""

    @classmethod
    def detect_quota_exhaustion(
        cls,
        stdout: str | None = None,
        stderr: str | None = None,
        exit_code: int | None = None,
    ) -> tuple[bool, bool, str, int | None]:
        text = _combined(stdout, stderr, exit_code)
        retry_after = parse_retry_after(text)
        for pattern, reason in _ACCOUNT_PATTERNS:
            if pattern.search(text):
                return True, True, reason, retry_after
        for pattern, reason in _MODEL_PATTERNS:
            if pattern.search(text):
                return True, False, reason, retry_after
        if exit_code == 402:
            return True, True, "http_402_payment_required", retry_after
        if exit_code == 429:
            return True, False, "rate_limit_429", retry_after
        return False, False, "", retry_after

    @classmethod
    def detect_unauthorized(
        cls,
        stdout: str | None = None,
        stderr: str | None = None,
        exit_code: int | None = None,
    ) -> tuple[bool, str]:
        text = _combined(stdout, stderr, exit_code).lower()
        patterns = [
            ("model_not_found", r"model[_ ]not[_ ]found"),
            ("model_does_not_exist", r"model (?:does not exist|not exist)"),
            ("no_model_access", r"(?:do(?:es)? not|doesn't|don't|no) (?:have )?access to (?:the )?model"),
            ("unauthorized_model", r"(?:unauthorized|unsupported|invalid) model"),
            ("permission_denied", r"permission[_ ]denied"),
            ("not_authorized", r"not authori[sz]ed"),
            (
                "not_logged_in",
                r"not logged in|please run /login|unauthenticated|authentication[_ ]error",
            ),
        ]
        for reason, pattern in patterns:
            if re.search(pattern, text, re.I):
                return True, reason
        if exit_code == 403 or re.search(r"\b403\b|forbidden", text):
            return True, "http_403_forbidden"
        if re.search(r"\b404\b", text) and re.search(r"model|not found", text):
            return True, "http_404_model_not_found"
        return False, ""

    @classmethod
    def detect_transient(
        cls,
        stdout: str | None = None,
        stderr: str | None = None,
        exit_code: int | None = None,
    ) -> tuple[bool, str]:
        text = _combined(stdout, stderr, exit_code).lower()
        patterns = [
            (r"\b408\b|request timeout", "http_408_request_timeout"),
            (
                r"\b(?:500|502|503|504)\b|internal server error|bad gateway|"
                r"service unavailable|gateway timeout",
                "http_5xx",
            ),
            (r"connection (?:reset|refused|aborted)", "connection_error"),
            (r"econnreset|econnrefused|etimedout", "connection_error"),
            (r"temporary(?:ly)? (?:unavailable|failure)", "temporary_unavailable"),
        ]
        for pattern, reason in patterns:
            if re.search(pattern, text, re.I):
                return True, reason
        return False, ""


class QuotaGuardian:
    """Persistent three-state breaker for ``(harness, model)`` scopes."""

    def __init__(
        self,
        storage: StateStore,
        *,
        cooldown_seconds: int = 300,
        failover_priority: Sequence[str] | None = None,
        now_fn: Callable[[], float] | None = None,
    ):
        self.storage = storage
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self.failover_priority = list(failover_priority or [])
        self.now_fn = now_fn or time.time

    # ------------------------------------------------------------------
    # circuit access
    # ------------------------------------------------------------------
    @staticmethod
    def scope_key(harness: str, model: str) -> str:
        return f"{harness}::{model}"

    def get_circuit(self, harness: str, model: str) -> QuotaCircuitRecord | None:
        return self.storage.get_circuit(harness, model)

    def list_circuits(
        self, harness: str | None = None, states: Iterable[str] | None = None
    ) -> list[QuotaCircuitRecord]:
        return self.storage.list_circuits(harness, states)

    def _base_record(self, harness: str, model: str) -> QuotaCircuitRecord:
        return QuotaCircuitRecord(
            scope_key=self.scope_key(harness, model),
            harness=harness,
            model=model,
            state=CircuitState.CLOSED,
        )

    def _refresh_state(self, record: QuotaCircuitRecord) -> QuotaCircuitRecord:
        now = self.now_fn()
        if record.state == CircuitState.OPEN and record.cooldown_until is not None:
            if now >= record.cooldown_until:
                record.state = CircuitState.HALF_OPEN
                self.storage.upsert_circuit(record)
        return record

    def record_exhaustion(
        self,
        harness: str,
        model: str,
        reason: str,
        *,
        is_account_wide: bool = False,
        retry_after_seconds: int | None = None,
        cooldown_seconds: int | None = None,
    ) -> QuotaCircuitRecord:
        now = self.now_fn()
        target_model = "*" if is_account_wide or model == "*" else model
        base_cooldown = max(1, int(cooldown_seconds if cooldown_seconds is not None else self.cooldown_seconds))
        cooldown = max(base_cooldown, int(retry_after_seconds or 0))
        record = self.storage.get_circuit(harness, target_model) or self._base_record(harness, target_model)
        record.state = CircuitState.OPEN
        record.failure_reason = reason
        record.exhausted_at = now
        record.cooldown_until = now + cooldown
        record.last_probed_at = record.last_probed_at
        return self.storage.upsert_circuit(record)

    def record_success(self, harness: str, model: str) -> None:
        """Close circuits only after a real successful provider response."""

        targets = [model]
        if model != "*":
            targets.append("*")
        for target in targets:
            record = self.storage.get_circuit(harness, target)
            if record is None:
                continue
            record.state = CircuitState.CLOSED
            record.failure_reason = ""
            record.cooldown_until = None
            record.last_probed_at = self.now_fn()
            self.storage.upsert_circuit(record)

    def mark_half_open(self, harness: str, model: str) -> QuotaCircuitRecord:
        record = self.storage.get_circuit(harness, model) or self._base_record(harness, model)
        if record.state != CircuitState.CLOSED:
            record.state = CircuitState.HALF_OPEN
            record.last_probed_at = self.now_fn()
            self.storage.upsert_circuit(record)
        return record

    def record_probe_result(
        self,
        harness: str,
        model: str,
        success: bool,
        *,
        detail: str = "",
        retry_after_seconds: int | None = None,
    ) -> QuotaCircuitRecord:
        now = self.now_fn()
        record = self.storage.get_circuit(harness, model) or self._base_record(harness, model)
        record.last_probed_at = now
        if success:
            record.state = CircuitState.CLOSED
            record.failure_reason = ""
            record.cooldown_until = None
            record.probe_count = 0
        else:
            record.state = CircuitState.OPEN
            record.probe_count += 1
            if detail:
                record.failure_reason = detail
            delay = max(self.cooldown_seconds, int(retry_after_seconds or 0))
            record.cooldown_until = now + delay
        return self.storage.upsert_circuit(record)

    def reset(self, harness: str, model: str = "*") -> int:
        """Manual reset used by MCP/CLI, returning affected circuit count."""

        if model == "*":
            circuits = self.storage.list_circuits(harness=harness)
            for record in circuits:
                self.storage.delete_circuit(record.harness, record.model)
            return len(circuits)
        existed = self.storage.get_circuit(harness, model) is not None
        self.storage.delete_circuit(harness, model)
        return 1 if existed else 0

    # ------------------------------------------------------------------
    # availability and selection
    # ------------------------------------------------------------------
    def availability(self, harness: str, model: str) -> str:
        now = self.now_fn()
        scopes = ["*"] if model == "*" else ["*", model]
        for scope_model in scopes:
            record = self.storage.get_circuit(harness, scope_model)
            if record is None:
                continue
            record = self._refresh_state(record)
            if record.state == CircuitState.OPEN:
                if record.cooldown_until is None or record.cooldown_until > now:
                    return CircuitState.OPEN.value
                return CircuitState.HALF_OPEN.value
            if record.state == CircuitState.HALF_OPEN:
                return CircuitState.HALF_OPEN.value
        return CircuitState.CLOSED.value

    def is_available(self, harness: str, model: str | None = None, *, allow_half_open: bool = True) -> bool:
        state = self.availability(harness, model or "*")
        if state == CircuitState.CLOSED.value:
            return True
        if state == CircuitState.HALF_OPEN.value:
            return allow_half_open
        return False

    def select_failover_candidate(
        self,
        candidates: Sequence[DiscoveredModel | tuple[str, str]],
        current_harness: str,
        current_model: str,
        *,
        exclude: Iterable[tuple[str, str]] | None = None,
        preferred_harnesses: Sequence[str] | None = None,
        allow_half_open: bool = False,
    ) -> tuple[str, str] | None:
        excluded = set(exclude or ())
        preferred = list(preferred_harnesses or [])
        priority: dict[str, int] = {}
        for index, harness in enumerate([*preferred, *self.failover_priority]):
            priority.setdefault(harness, index)

        normalized: list[tuple[str, str, float]] = []
        for candidate in candidates:
            if isinstance(candidate, tuple):
                harness, model = candidate
                score = 0.0
            else:
                harness, model = candidate.harness, candidate.model_id
                score = candidate.capability_score
            if (harness, model) in excluded:
                continue
            if harness == current_harness and model == current_model:
                continue
            if (harness, model) in {(item[0], item[1]) for item in normalized}:
                continue
            state = self.availability(harness, model)
            if state == CircuitState.OPEN.value:
                continue
            if state == CircuitState.HALF_OPEN.value and not allow_half_open:
                continue
            normalized.append((harness, model, score))

        if not normalized:
            return None

        def sort_key(item: tuple[str, str, float]) -> tuple[int, int, int, float]:
            harness, _model, score = item
            same_harness = 0 if harness == current_harness else 1
            harness_priority = 0 if harness == current_harness else priority.get(harness, 999)
            state_rank = 0 if self.availability(harness, _model) == CircuitState.CLOSED.value else 1
            return (same_harness, harness_priority, state_rank, -score)

        normalized.sort(key=sort_key)
        harness, model, _score = normalized[0]
        return harness, model

    def candidate_pairs(self, models: Iterable[DiscoveredModel]) -> list[tuple[str, str]]:
        return [(model.harness, model.model_id) for model in models]

    def status_snapshot(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for record in self.storage.list_circuits():
            self._refresh_state(record)
            output.append(record.model_dump(mode="json"))
        return output

    def open_circuits(self) -> list[QuotaCircuitRecord]:
        records = [*self.storage.list_circuits(states=["open"]), *self.storage.list_circuits(states=["half_open"])]
        unique: dict[str, QuotaCircuitRecord] = {record.scope_key: record for record in records}
        return list(unique.values())


class QuotaRecoveryMonitor:
    """Periodic lightweight probe that wakes ``waiting_for_quota`` tickets."""

    def __init__(
        self,
        guardian: QuotaGuardian,
        *,
        probe: Callable[[str], Awaitable[Any] | Any] | None = None,
        on_quota_recovered: Callable[[str, str], Awaitable[Any] | Any] | None = None,
        interval_seconds: float = 60.0,
        min_probe_interval_seconds: float = 0.0,
        now_fn: Callable[[], float] | None = None,
    ):
        self.guardian = guardian
        self.probe = probe
        self.on_quota_recovered = on_quota_recovered
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.min_probe_interval_seconds = max(0.0, float(min_probe_interval_seconds))
        self.now_fn = now_fn or time.time
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self.last_recovery_results: list[dict[str, Any]] = []

    async def _call_probe(self, harness: str, model: str) -> tuple[bool, str]:
        if self.probe is None:
            return False, "no probe configured"
        result = None
        if self._probe_accepts_model(self.probe):
            result = self.probe(harness, model)
        else:
            result = self.probe(harness)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, tuple):
            success = bool(result[0])
            detail = str(result[1]) if len(result) > 1 else ""
            return success, detail
        if isinstance(result, dict):
            return bool(result.get("success", result.get("ok", False))), str(result.get("detail", ""))
        return bool(result), ""

    @staticmethod
    def _probe_accepts_model(probe: Any) -> bool:
        try:
            parameters = list(inspect.signature(probe).parameters.values())
        except (TypeError, ValueError):
            return False
        for parameter in parameters:
            if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                return True
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        return len(positional) >= 2

    async def _call_recovered(self, harness: str, model: str) -> None:
        if self.on_quota_recovered is None:
            return
        result = self.on_quota_recovered(harness, model)
        if inspect.isawaitable(result):
            await result

    async def check_and_recover_all(self, force: bool = True) -> list[dict[str, Any]]:
        now = self.now_fn()
        circuits = self.guardian.open_circuits()
        results: list[dict[str, Any]] = []
        recovered: list[tuple[str, str]] = []
        seen_scopes: set[str] = set()
        for record in circuits:
            if record.scope_key in seen_scopes:
                continue
            seen_scopes.add(record.scope_key)
            if not force and record.cooldown_until is not None and record.cooldown_until > now:
                results.append(
                    {
                        "harness": record.harness,
                        "model": record.model,
                        "probed": False,
                        "reason": "cooldown_active",
                    }
                )
                continue
            if (
                self.min_probe_interval_seconds > 0
                and record.last_probed_at is not None
                and (now - record.last_probed_at) < self.min_probe_interval_seconds
            ):
                results.append(
                    {
                        "harness": record.harness,
                        "model": record.model,
                        "probed": False,
                        "reason": "probe_interval_not_elapsed",
                    }
                )
                continue
            self.guardian.mark_half_open(record.harness, record.model)
            try:
                success, detail = await self._call_probe(record.harness, record.model)
            except Exception as exc:  # a broken probe must not stop monitoring
                success, detail = False, f"probe error: {exc}"
            updated = self.guardian.record_probe_result(
                record.harness,
                record.model,
                success,
                detail="" if success else detail,
            )
            results.append(
                {
                    "harness": record.harness,
                    "model": record.model,
                    "probed": True,
                    "success": success,
                    "detail": detail,
                    "state": updated.state.value if hasattr(updated.state, "value") else str(updated.state),
                }
            )
            if success:
                recovered.append((record.harness, record.model))

        # Per-model circuits are probed individually; an account-level success
        # does not by itself prove that a model-scoped rate limit has cleared.

        notified: set[str] = set()
        for harness, model in recovered:
            key = f"{harness}::{model}"
            if key in notified:
                continue
            notified.add(key)
            try:
                await self._call_recovered(harness, model)
            except Exception as exc:
                # A failing resume callback must not stop the monitor loop; the
                # ticket stays waiting and the next probe can retry it.
                results.append(
                    {
                        "harness": harness,
                        "model": model,
                        "recovered": True,
                        "resume_error": str(exc)[:300],
                    }
                )

        self.last_recovery_results = results
        return results

    async def run_once(self, force: bool = True) -> list[dict[str, Any]]:
        return await self.check_and_recover_all(force=force)

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.check_and_recover_all(force=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Monitoring must survive transient storage/probe failures.
                pass
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="harness-mesh-quota-monitor")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()


__all__ = [
    "QuotaErrorDetector",
    "QuotaGuardian",
    "QuotaRecoveryMonitor",
    "parse_retry_after",
]
