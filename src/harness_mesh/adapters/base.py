"""Shared adapter runtime: command rendering, subprocess lifecycle, parsing."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..config import HarnessConfig, MeshConfig, resolve_harness_binary
from ..models import HarnessStatus, TaskRequest

Runner = Callable[
    [list[str], Path, int, dict[str, str]],
    Awaitable[tuple[int, str, str, bool] | tuple[int, str, str]],
]

_PURE_PLACEHOLDER = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_ANY_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class AdapterCall:
    task_id: str
    assignment_id: str
    prompt: str
    workdir: Path
    model: str
    system_prompt: str = ""
    reasoning_effort: str | None = None
    mcp_config_path: Path | None = None
    plugins: list[str] = field(default_factory=list)
    timeout_seconds: int = 1800
    env: dict[str, str] = field(default_factory=dict)
    trace_chain: list[str] = field(default_factory=list)
    call_depth: int = 0
    read_only: bool = False
    shared_write: bool = False
    session_id: str | None = None
    skills: list[str] = field(default_factory=list)
    task: TaskRequest | None = None
    # Called with the pid of the spawned harness process, right after the
    # process exists.  The router uses it to record assignment lineage.
    on_process_start: Callable[[int], None] | None = None


@dataclass
class AdapterOutcome:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    session_id: str | None = None
    duration_ms: int = 0
    error_detail: str | None = None
    parsed_output: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error_detail is None

    @property
    def assistant_text(self) -> str:
        return self.parsed_output or self.stdout


class BaseHarnessAdapter:
    """Abstract harness adapter.

    Subclasses only need to provide ``harness_name`` and (optionally) output
    parsing or harness-specific argument injection.  Every adapter inherits the
    same process-group kill semantics, timeout handling and safe command
    template rendering.
    """

    harness_name: str = ""

    def __init__(
        self,
        config: MeshConfig,
        harness_config: HarnessConfig,
        workspace_root: str | Path,
        *,
        env: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] | None = None,
        runner: Runner | None = None,
        now_fn: Callable[[], float] | None = None,
    ):
        self.config = config
        self.harness_config = harness_config
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.env = dict(env if env is not None else os.environ)
        self._which = which or shutil.which
        self._runner = runner
        self.now_fn = now_fn or time.time
        self._last_transcript: str = ""

    # ------------------------------------------------------------------
    # discovery / health
    # ------------------------------------------------------------------
    def resolve_binary(self) -> str | None:
        state_dir = self.config.state_path(self.workspace_root)
        home_dir = Path(self.env.get("HOME")) if self.env.get("HOME") else Path.home()
        return resolve_harness_binary(
            self.harness_config,
            self._which,
            state_dir,
            home_dir,
            allow_system_paths=(self._which is shutil.which),
        )

    @property
    def binary_path(self) -> str | None:
        return self.resolve_binary()

    @property
    def supports_mcp_config(self) -> bool:
        """Whether the compiled ``.mcp.<task>.json`` can actually be mounted."""

        return True

    async def detect(self) -> HarnessStatus:
        binary = self.resolve_binary()
        if not binary:
            return HarnessStatus(
                harness=self.harness_name,
                available=False,
                mode="missing",
                binary_path=None,
                version_or_detail="binary not found",
                active_default_model=self._env_default_model(),
            )
        code, stdout, stderr, _timed_out = await self._run_subprocess(
            [binary, *self.harness_config.version_args], self.workspace_root, timeout=15
        )
        detail = (stdout or stderr).strip().splitlines()
        return HarnessStatus(
            harness=self.harness_name,
            available=code == 0,
            mode="cli",
            binary_path=binary,
            version_or_detail=detail[0][:300] if detail else ("ok" if code == 0 else f"exit {code}"),
            active_default_model=self._env_default_model(),
        )

    async def probe(self) -> tuple[bool, str]:
        """Lightweight liveness probe (no model tokens are consumed)."""

        binary = self.resolve_binary()
        if not binary:
            return False, "binary not found"
        code, stdout, stderr, _timed_out = await self._run_subprocess(
            [binary, *self.harness_config.version_args], self.workspace_root, timeout=15
        )
        if code == 0:
            return True, (stdout.strip().splitlines() or ["ok"])[0][:200]
        return False, (stderr or stdout).strip()[:300] or f"exit {code}"

    async def probe_quota(self, model: str | None = None) -> tuple[bool, str] | None:
        """Real provider probe used before a quota circuit is re-closed.

        A binary liveness check or a local config scan proves nothing about a
        provider budget.  This method therefore runs one deliberately tiny
        assignment (short prompt, temporary directory, no MCP/skills) and
        treats only a valid assistant response as recovery.  ``None`` means
        this adapter cannot probe (for example the DeepSeek built-in runtime,
        which overrides this method with a direct HTTP completion).
        """

        if self.harness_config.extra.get("quota_probe_enabled") is False:
            return None
        timeout_seconds = int(self.harness_config.extra.get("quota_probe_timeout_seconds", 90))
        prompt = str(
            self.harness_config.extra.get(
                "quota_probe_prompt",
                "Quota recovery probe. Reply with the single word OK. "
                "Do not use tools and do not modify any files.",
            )
        )
        probe_dir = Path(tempfile.mkdtemp(prefix="harness-mesh-quota-probe-"))
        try:
            call = AdapterCall(
                task_id=f"quota-probe-{self.harness_name}",
                assignment_id="quota-probe",
                prompt=prompt,
                workdir=probe_dir,
                model=model if model and model != "*" else "<cli_active_default>",
                timeout_seconds=timeout_seconds,
            )
            outcome = await self.execute(call)
            if outcome.timed_out:
                return False, "quota probe timed out"
            from ..quota import QuotaErrorDetector

            is_quota, _account_wide, quota_reason, _retry = QuotaErrorDetector.detect_quota_exhaustion(
                outcome.stdout, outcome.stderr, outcome.exit_code
            )
            unauthorized, unauthorized_reason = QuotaErrorDetector.detect_unauthorized(
                outcome.stdout, outcome.stderr, outcome.exit_code
            )
            if is_quota:
                return False, f"quota still exhausted: {quota_reason}"
            if unauthorized:
                return False, f"model still unauthorised: {unauthorized_reason}"
            if outcome.exit_code != 0 or outcome.error_detail:
                return False, (outcome.error_detail or outcome.stderr or f"exit {outcome.exit_code}")[:300]
            if (outcome.parsed_output or outcome.stdout or "").strip():
                return True, "lightweight model completion succeeded"
            return False, "model probe produced no assistant message"
        except Exception as exc:
            return False, f"quota probe failed: {exc}"
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)

    def _auto_approval_allowed(self, call: AdapterCall) -> bool:
        if call.read_only:
            return False
        if call.shared_write and not self.config.auto_approve_shared_write:
            return False
        return bool(self.harness_config.auto_approve)

    def _auto_approve_args(self, default: list[str]) -> list[str]:
        configured = self.harness_config.extra.get("auto_approve_args")
        if isinstance(configured, list) and configured:
            return [str(item) for item in configured]
        return list(default)

    def _env_default_model(self) -> str | None:
        for variable in self.harness_config.env_model_variables:
            value = self.env.get(variable)
            if value:
                return value
        return None

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    async def execute(self, call: AdapterCall) -> AdapterOutcome:
        started = self.now_fn()
        binary = self.resolve_binary()
        if not binary:
            return AdapterOutcome(
                exit_code=127,
                stderr=f"{self.harness_name} binary not found",
                error_detail="binary_not_found",
                duration_ms=int((self.now_fn() - started) * 1000),
            )
        system_prompt_file: Path | None = None
        try:
            values = self._template_values(call)
            if any("{system_prompt_file}" in token for token in self.harness_config.execute_template):
                fd, temp_name = tempfile.mkstemp(prefix="harness-mesh-system-", suffix=".md")
                os.close(fd)
                system_prompt_file = Path(temp_name)
                system_prompt_file.write_text(call.system_prompt or "", encoding="utf-8")
                values["system_prompt_file"] = str(system_prompt_file)
            args = render_command(self.harness_config.execute_template, values)
            if not args:
                return AdapterOutcome(
                    exit_code=2,
                    stderr="empty command template",
                    error_detail="invalid_command_template",
                )
            if args[0] in self.harness_config.binary_candidates():
                args[0] = binary
            args = self._augment_command(args, call)
            env = self._build_env(call)
            timeout = int(call.timeout_seconds or self.harness_config.timeout_seconds)
            code, stdout, stderr, timed_out = await self._run_subprocess(
                args,
                Path(call.workdir),
                timeout=timeout,
                env=env,
                on_process_start=call.on_process_start,
            )
            outcome = AdapterOutcome(
                exit_code=124 if timed_out else code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                duration_ms=int((self.now_fn() - started) * 1000),
            )
            self._parse_output(outcome, call)
            self._last_transcript = _format_transcript(args, stdout, stderr)
            outcome.metadata.setdefault("command", args)
            return outcome
        except Exception as exc:
            return AdapterOutcome(
                exit_code=1,
                stderr=str(exc),
                error_detail=f"adapter_error: {exc}",
                duration_ms=int((self.now_fn() - started) * 1000),
            )
        finally:
            if system_prompt_file is not None:
                try:
                    system_prompt_file.unlink()
                except OSError:
                    pass

    def _template_values(self, call: AdapterCall) -> dict[str, str]:
        model = "" if call.model in {"", "<cli_active_default>"} else call.model
        prompt = self._compose_prompt(call)
        return {
            "prompt": prompt,
            "system_prompt": call.system_prompt,
            "system_prompt_file": "",
            "model": model,
            "effort": call.reasoning_effort or "",
            "workdir": str(call.workdir),
            "mcp_config": str(call.mcp_config_path) if call.mcp_config_path else "",
            "task_id": call.task_id,
            "assignment_id": call.assignment_id,
        }

    def _compose_prompt(self, call: AdapterCall) -> str:
        if call.system_prompt and self.harness_config.skill_injection != "system_prompt":
            return f"{call.system_prompt}\n\n{call.prompt}"
        return call.prompt

    def _augment_command(self, args: list[str], call: AdapterCall) -> list[str]:
        return args

    def _build_env(self, call: AdapterCall) -> dict[str, str]:
        """Build the child process environment.

        The full parent environment is inherited on purpose: real harness
        CLIs need their own login, session and proxy state to work.  Credential
        scrubbing is *not* applied here; it belongs to the built-in runtime's
        shell tool (``security.scrub_subprocess_env``, used by the DeepSeek
        adapter).  Adding blanket scrubbing here would break authenticated
        harnesses without closing the trust boundary, which is enforced at the
        configuration layer instead.
        """

        env = dict(self.env)
        env.update(
            {
                "HARNESS_MESH_TASK_ID": call.task_id,
                "HARNESS_MESH_ASSIGNMENT_ID": call.assignment_id,
                "HARNESS_MESH_HARNESS": self.harness_name,
                "HARNESS_MESH_MODEL": call.model,
                "HARNESS_MESH_WORKDIR": str(call.workdir),
                "HARNESS_MESH_CALL_DEPTH": str(int(call.call_depth)),
                "HARNESS_MESH_TRACE_CHAIN": json.dumps(list(call.trace_chain)),
                "HARNESS_MESH_REASONING_EFFORT": str(call.reasoning_effort or ""),
                "HARNESS_MESH_READ_ONLY": "1" if call.read_only else "0",
            }
        )
        env.update({str(key): str(value) for key, value in call.env.items()})
        return env

    async def _run_subprocess(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        timeout: int = 1800,
        env: Mapping[str, str] | None = None,
        on_process_start: Callable[[int], None] | None = None,
    ) -> tuple[int, str, str, bool]:
        args = [str(item) for item in args]
        if self._runner is not None:
            result = await self._runner(args, cwd, timeout, dict(env or self.env))
            if len(result) == 4:
                return int(result[0]), result[1], result[2], bool(result[3])
            return int(result[0]), result[1], result[2], False
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(env or self.env),
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as exc:
            return 127, "", str(exc), False
        if on_process_start is not None and process.pid:
            # Recorded before the child can start a nested MCP server, so the
            # ancestor lookup always sees the pid.
            on_process_start(int(process.pid))
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=max(1, timeout))
            return (
                process.returncode or 0,
                stdout_bytes.decode("utf-8", errors="replace"),
                stderr_bytes.decode("utf-8", errors="replace"),
                False,
            )
        except asyncio.CancelledError:
            # Router cancellation (cancel_task) must not leave the harness
            # process group running against the worktree.
            await self._kill_process(process)
            raise
        except asyncio.TimeoutError:
            await self._kill_process(process)
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                stdout_bytes, stderr_bytes = b"", b""
            return (
                124,
                stdout_bytes.decode("utf-8", errors="replace"),
                stderr_bytes.decode("utf-8", errors="replace") + "\n[harness-mesh] assignment timed out",
                True,
            )

    @staticmethod
    async def _kill_process(process: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:  # pragma: no cover - Windows fallback
                process.kill()
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass

    def _parse_output(self, outcome: AdapterOutcome, call: AdapterCall) -> None:
        outcome.parsed_output = outcome.stdout.strip()

    def transcript(self) -> str:
        return self._last_transcript


def render_command(template: Sequence[str], values: Mapping[str, str]) -> list[str]:
    """Render a command template, dropping flags whose value is empty.

    Example: ``["claude", "--model", "{model}", "-p", "{prompt}"]`` with an
    empty model becomes ``["claude", "-p", "<prompt>"]`` instead of passing an
    empty ``--model`` argument.
    """

    rendered: list[str] = []
    index = 0
    tokens = [str(token) for token in template]
    while index < len(tokens):
        token = tokens[index]
        match = _PURE_PLACEHOLDER.match(token)
        if match:
            value = values.get(match.group(1), "")
            if value:
                rendered.append(value)
            index += 1
            continue
        if token.startswith("-") and "{" not in token and index + 1 < len(tokens):
            next_match = _PURE_PLACEHOLDER.match(tokens[index + 1])
            if next_match and not values.get(next_match.group(1), ""):
                index += 2
                continue
        try:
            expanded = _ANY_PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), token)
        except (KeyError, ValueError):
            expanded = token
        if expanded.endswith("=") and "{" in token:
            # ``--model={model}`` with an empty value must disappear entirely.
            index += 1
            continue
        if expanded:
            rendered.append(expanded)
        index += 1
    return rendered


def _format_transcript(args: Sequence[str], stdout: str, stderr: str) -> str:
    parts = [f"$ {' '.join(str(item) for item in args)}"]
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append("[stderr]\n" + stderr)
    return "\n".join(parts)


__all__ = [
    "AdapterCall",
    "AdapterOutcome",
    "BaseHarnessAdapter",
    "Runner",
    "render_command",
]
