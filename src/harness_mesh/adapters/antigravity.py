"""Google Antigravity adapter: CLI (``agy``) with an optional Python SDK path."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
from typing import Any

from .base import AdapterCall, AdapterOutcome, BaseHarnessAdapter


class AntigravityAdapter(BaseHarnessAdapter):
    harness_name = "antigravity"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._sdk_module_name = str(self.harness_config.extra.get("sdk_module", "google.antigravity"))
        self._sdk_enabled = bool(self.harness_config.extra.get("sdk_enabled", True))

    @property
    def supports_mcp_config(self) -> bool:
        if any("{mcp_config}" in token for token in self.harness_config.execute_template):
            return True
        return bool(self.harness_config.extra.get("supports_mcp_flag"))

    def _augment_command(self, args: list[str], call: AdapterCall) -> list[str]:
        extra: list[str] = []
        if "--mode" not in args:
            if call.read_only:
                extra.extend(["--mode", "plan"])
            elif self.harness_config.auto_approve:
                extra.extend(self._auto_approve_args(["--dangerously-skip-permissions"]))
        if call.session_id and "--conversation" not in args:
            extra.extend(["--conversation", call.session_id])
        return [*args, *extra] if extra else args

    def _sdk_available(self) -> bool:
        if not self._sdk_enabled:
            return False
        try:
            return importlib.util.find_spec(self._sdk_module_name) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            return False

    async def detect(self):
        binary = self.resolve_binary()
        if binary:
            return await super().detect()
        if self._sdk_available():
            from ..models import HarnessStatus

            return HarnessStatus(
                harness=self.harness_name,
                available=True,
                mode="sdk",
                binary_path=None,
                version_or_detail=f"python SDK detected: {self._sdk_module_name}",
                active_default_model=self._env_default_model(),
            )
        from ..models import HarnessStatus

        return HarnessStatus(
            harness=self.harness_name,
            available=False,
            mode="missing",
            version_or_detail="neither agy CLI nor google.antigravity SDK found",
            active_default_model=self._env_default_model(),
        )

    async def probe(self) -> tuple[bool, str]:
        binary = self.resolve_binary()
        if binary:
            return await super().probe()
        if self._sdk_available():
            return True, "SDK import ok"
        return False, "neither agy CLI nor SDK found"

    async def execute(self, call: AdapterCall) -> AdapterOutcome:
        if self.resolve_binary():
            return await super().execute(call)
        if not self._sdk_available():
            return AdapterOutcome(
                exit_code=127,
                stderr="antigravity CLI/SDK is not installed",
                error_detail="binary_not_found",
            )
        return await self._execute_sdk(call)

    def _template_values(self, call: AdapterCall) -> dict[str, str]:
        values = super()._template_values(call)
        # Antigravity exposes effort-bound model ids (``...-high``); passing a
        # separate --effort then conflicts with --model.
        if any(call.model.endswith(suffix) for suffix in ("-low", "-medium", "-high", "-xhigh", "-max")):
            values["effort"] = ""
        return values

    def _parse_output(self, outcome: AdapterOutcome, call: AdapterCall) -> None:
        text = (outcome.stdout or "").strip()
        if not text:
            outcome.parsed_output = ""
            return
        payload = None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            if start >= 0:
                try:
                    payload = json.loads(text[start:])
                except json.JSONDecodeError:
                    payload = None
        if isinstance(payload, dict):
            for key in ("response", "result", "output", "text", "content", "message"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    outcome.parsed_output = value.strip()
                    break
            if not outcome.parsed_output:
                outcome.parsed_output = text
            session_id = payload.get("conversation_id") or payload.get("conversationId") or payload.get("session_id")
            if isinstance(session_id, str):
                outcome.session_id = session_id
            status = payload.get("status")
            error = payload.get("error")
            if error or (isinstance(status, str) and status.lower() not in {"success", "ok", "completed"}):
                detail = error or payload.get("message") or f"antigravity status={status}"
                if isinstance(detail, dict):
                    detail = detail.get("message") or json.dumps(detail)
                outcome.stderr = (outcome.stderr + "\n" + str(detail)).strip()
                outcome.error_detail = str(detail)[:500]
                if outcome.exit_code == 0:
                    outcome.exit_code = 1
            return
        outcome.parsed_output = text

    async def _execute_sdk(self, call: AdapterCall) -> AdapterOutcome:
        started = self.now_fn()
        try:
            module = importlib.import_module(self._sdk_module_name)
        except Exception as exc:
            return AdapterOutcome(
                exit_code=127,
                stderr=f"cannot import {self._sdk_module_name}: {exc}",
                error_detail="sdk_import_failed",
            )
        prompt = self._compose_prompt(call)
        timeout = int(call.timeout_seconds or self.harness_config.timeout_seconds)
        if hasattr(module, "Agent") and hasattr(module, "LocalAgentConfig"):
            native = await self._execute_native_sdk(module, prompt, call, timeout)
            if native is not None:
                return native
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._invoke_sdk, module, prompt, call),
                timeout=max(1, timeout),
            )
        except asyncio.TimeoutError:
            return AdapterOutcome(
                exit_code=124,
                timed_out=True,
                stderr="[harness-mesh] antigravity SDK call timed out",
                duration_ms=int((self.now_fn() - started) * 1000),
            )
        except Exception as exc:
            return AdapterOutcome(
                exit_code=1,
                stderr=str(exc),
                error_detail=f"sdk_error: {exc}",
                duration_ms=int((self.now_fn() - started) * 1000),
            )
        if inspect.isawaitable(result):
            result = await result
        text = _normalize_sdk_result(result)
        return AdapterOutcome(
            exit_code=0,
            stdout=text,
            parsed_output=text,
            duration_ms=int((self.now_fn() - started) * 1000),
            metadata={"sdk": self._sdk_module_name},
        )

    async def _execute_native_sdk(
        self, module: Any, prompt: str, call: AdapterCall, timeout: int
    ) -> AdapterOutcome | None:
        """Use the documented async ``Agent`` + ``LocalAgentConfig`` surface."""

        config_class = getattr(module, "LocalAgentConfig")
        values = {
            "model": call.model if call.model != "<cli_active_default>" else None,
            "working_directory": str(call.workdir),
            "cwd": str(call.workdir),
            "system_prompt": call.system_prompt or None,
            "reasoning_effort": call.reasoning_effort,
            "mcp_config": str(call.mcp_config_path) if call.mcp_config_path else None,
        }
        try:
            config = config_class(**self._filter_kwargs(config_class, values))
        except Exception:
            config = config_class()
        started = self.now_fn()

        async def _run() -> str:
            agent = module.Agent(config)
            async with agent as active:
                response = await active.chat(prompt)
                if hasattr(response, "__aiter__"):
                    chunks: list[str] = []
                    async for token in response:
                        chunks.append(_normalize_sdk_result(token))
                    return "".join(chunks)
                return _normalize_sdk_result(response)

        try:
            text = await asyncio.wait_for(_run(), timeout=max(1, timeout))
        except asyncio.TimeoutError:
            return AdapterOutcome(exit_code=124, timed_out=True, stderr="native SDK call timed out")
        except Exception as exc:
            return AdapterOutcome(exit_code=1, stderr=str(exc), error_detail=f"native_sdk_error: {exc}")
        return AdapterOutcome(
            exit_code=0,
            stdout=text,
            parsed_output=text,
            duration_ms=int((self.now_fn() - started) * 1000),
            metadata={"sdk": self._sdk_module_name, "native_async": True},
        )

    def _invoke_sdk(self, module: Any, prompt: str, call: AdapterCall) -> Any:
        """Call the SDK through its documented high-level entry points.

        The Antigravity Python SDK surface changes between releases, so the
        adapter locates ``run`` / ``generate`` / ``complete`` and binds only
        keyword arguments the installed function actually accepts.  When no
        function is exported it falls back to an ``Agent`` class.
        """

        values = {
            "prompt": prompt,
            "model": call.model if call.model != "<cli_active_default>" else None,
            "working_directory": str(call.workdir),
            "cwd": str(call.workdir),
            "system_prompt": call.system_prompt or None,
            "mcp_config": str(call.mcp_config_path) if call.mcp_config_path else None,
        }
        for function_name in ("run", "generate", "complete", "run_agent"):
            function = getattr(module, function_name, None)
            if callable(function):
                return function(**self._filter_kwargs(function, values))
        agent_class = getattr(module, "Agent", None)
        if isinstance(agent_class, type):
            kwargs = self._filter_kwargs(agent_class.__init__, values)
            agent = agent_class(**kwargs)
            for method_name in ("run", "execute", "generate", "chat"):
                method = getattr(agent, method_name, None)
                if callable(method):
                    return method(prompt)
        raise RuntimeError(
            f"{self._sdk_module_name} does not expose a recognised run/generate/Agent entry point"
        )

    @staticmethod
    def _filter_kwargs(function: Any, values: dict[str, Any]) -> dict[str, Any]:
        try:
            signature = inspect.signature(function)
        except (TypeError, ValueError):
            return {"prompt": values["prompt"]}
        kwargs: dict[str, Any] = {}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        for name, parameter in signature.parameters.items():
            if name in {"self", "cls"}:
                continue
            if name in values and values[name] is not None:
                kwargs[name] = values[name]
            elif accepts_kwargs and parameter.default is inspect.Parameter.empty:
                continue
        if not kwargs:
            first = next(iter(signature.parameters.values()), None)
            if first is not None:
                return {"prompt": values["prompt"]}
        return kwargs


def _normalize_sdk_result(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("text", "output", "content", "result", "message"):
            value = result.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(result, ensure_ascii=False, default=str)
    for attribute in ("text", "output", "content", "result"):
        value = getattr(result, attribute, None)
        if isinstance(value, str):
            return value
    return str(result)


__all__ = ["AntigravityAdapter"]
