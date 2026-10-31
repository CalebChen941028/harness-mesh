"""DeepSeek Harness adapter.

Three execution channels are supported:

* ``dsh`` CLI (DeepSeek Harness),
* legacy ``deepseek`` CLI,
* an internal OpenAI-compatible tool-calling runtime used when no CLI is
  installed but ``DEEPSEEK_API_KEY`` is available.

The built-in runtime executes read/write/patch/list/shell tools strictly inside
the assignment worktree, so quota failover can resume on a different harness
without losing local edits.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
from pathlib import Path
from typing import Any

import httpx
import yaml

from .base import AdapterCall, AdapterOutcome, BaseHarnessAdapter


class DeepSeekAdapter(BaseHarnessAdapter):
    harness_name = "deepseek"

    def __init__(
        self,
        *args: Any,
        http_transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.http_transport = http_transport
        self.http_client = http_client
        self._overlays: dict[str, tuple[Path, Path]] = {}

    # ------------------------------------------------------------------
    # detection
    # ------------------------------------------------------------------
    @property
    def _builtin_enabled(self) -> bool:
        return bool(self.harness_config.extra.get("builtin_runtime", True))

    @property
    def supports_mcp_config(self) -> bool:
        joined = " ".join(self.harness_config.execute_template)
        return "{mcp_config}" in joined or bool(self.harness_config.extra.get("supports_mcp_flag"))

    def _api_key(self) -> str | None:
        variable = str(
            self.harness_config.extra.get("builtin_api_key_env")
            or (self.harness_config.api_key_env[0] if self.harness_config.api_key_env else "DEEPSEEK_API_KEY")
        )
        value = self.env.get(variable)
        if value:
            return value
        for name in self.harness_config.api_key_env:
            value = self.env.get(name)
            if value:
                return value
        return None

    def _base_url(self) -> str:
        for name in self.harness_config.base_url_env:
            value = self.env.get(name)
            if value:
                return value.rstrip("/")
        extra_url = self.harness_config.extra.get("builtin_base_url")
        return str(extra_url or self.harness_config.default_base_url or "https://api.deepseek.com").rstrip("/")

    def _api_endpoint(self, path: str) -> str:
        base = self._base_url().rstrip("/")
        normalized = "/" + path.lstrip("/")
        if base.endswith("/v1"):
            return f"{base}{normalized}"
        return f"{base}/v1{normalized}"

    def _dsh_home(self) -> Path:
        value = self.env.get("DSH_HOME")
        if value:
            return Path(value).expanduser()
        home = self.env.get("HOME")
        return Path(home).expanduser() / ".dsh" if home else Path.home() / ".dsh"

    @property
    def _uses_headless_profile(self) -> bool:
        tokens = self.harness_config.execute_template
        return "--profile" in tokens and "headless" in tokens

    async def detect(self):
        binary = self.resolve_binary()
        if binary:
            result = await super().detect()
            result.available = True
            return result
        from ..models import HarnessStatus

        if self._builtin_enabled and self._api_key():
            return HarnessStatus(
                harness=self.harness_name,
                available=True,
                mode="builtin_runtime",
                binary_path=None,
                version_or_detail="built-in OpenAI-compatible tool-calling runtime",
                active_default_model=self._env_default_model(),
            )
        return HarnessStatus(
            harness=self.harness_name,
            available=False,
            mode="missing",
            version_or_detail="neither dsh/deepseek CLI nor DEEPSEEK_API_KEY is available",
            active_default_model=self._env_default_model(),
        )

    async def probe(self) -> tuple[bool, str]:
        binary = self.resolve_binary()
        if binary:
            return await super().probe()
        if not (self._builtin_enabled and self._api_key()):
            return False, "no CLI and no API key"
        created = False
        client = self.http_client
        if client is None:
            client = httpx.AsyncClient(transport=self.http_transport, timeout=httpx.Timeout(10.0))
            created = True
        try:
            response = await client.get(
                self._api_endpoint("/models"),
                headers={"Authorization": f"Bearer {self._api_key()}"},
            )
        except httpx.HTTPError as exc:
            return False, str(exc)
        finally:
            if created:
                await client.aclose()
        if response.status_code == 200:
            return True, "models endpoint reachable"
        return False, f"HTTP {response.status_code}: {response.text[:200]}"

    async def probe_quota(self, model: str | None = None) -> tuple[bool, str] | None:
        """Provider-level probe for the built-in runtime channel.

        The CLI channel uses the shared tiny-completion probe from
        ``BaseHarnessAdapter``.  With no CLI, the built-in runtime sends one
        minimal chat completion (or a models-list request when no model name is
        known), which is the only reliable way to observe a real budget or
        rate-limit response.
        """

        if self.resolve_binary():
            return await super().probe_quota(model)
        if not (self._builtin_enabled and self._api_key()):
            return None
        probe_model = model if model and model != "*" else self._env_default_model()
        created = False
        client = self.http_client
        if client is None:
            client = httpx.AsyncClient(transport=self.http_transport, timeout=httpx.Timeout(30.0))
            created = True
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        try:
            if probe_model:
                response = await client.post(
                    self._api_endpoint("/chat/completions"),
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "model": probe_model,
                        "messages": [{"role": "user", "content": "Reply with OK."}],
                        "max_tokens": 16,
                        "temperature": 0,
                    },
                )
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {}
                    choices = payload.get("choices") or []
                    if choices:
                        return True, "lightweight chat completion succeeded"
                return False, f"HTTP {response.status_code}: {response.text[:300]}"
            response = await client.get(self._api_endpoint("/models"), headers=headers)
            if response.status_code == 200:
                return True, "models endpoint reachable (no probe model configured)"
            return False, f"HTTP {response.status_code}: {response.text[:300]}"
        except httpx.HTTPError as exc:
            return False, str(exc)
        finally:
            if created:
                await client.aclose()

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    async def execute(self, call: AdapterCall) -> AdapterOutcome:
        binary = self.resolve_binary()
        if binary:
            try:
                self._prepare_dsh_overlay(call)
                return await super().execute(call)
            finally:
                self._cleanup_overlay(call.assignment_id)
        if not self._builtin_enabled:
            return AdapterOutcome(
                exit_code=127,
                stderr="dsh/deepseek CLI is not installed",
                error_detail="binary_not_found",
            )
        api_key = self._api_key()
        if not api_key:
            return AdapterOutcome(
                exit_code=127,
                stderr="dsh/deepseek CLI is not installed and DEEPSEEK_API_KEY is not set",
                error_detail="binary_not_found",
            )
        runtime = _BuiltinDeepSeekRuntime(self, call, api_key=api_key)
        return await runtime.run()

    def _augment_command(self, args: list[str], call: AdapterCall) -> list[str]:
        augmented = list(args)
        overlay = self._overlays.get(call.assignment_id)
        if overlay is not None:
            overlay_path = overlay[1]
            if "--patch" not in augmented:
                if "headless" in augmented:
                    index = augmented.index("headless") + 1
                elif "headless" in self.harness_config.execute_template:
                    index = min(3, len(augmented))
                else:
                    index = 1
                augmented = [*augmented[:index], "--patch", str(overlay_path), *augmented[index:]]
        if call.session_id and "--session-id" not in augmented:
            if len(augmented) > 1:
                augmented = [*augmented[:-1], "--session-id", call.session_id, augmented[-1]]
            else:
                augmented.extend(["--session-id", call.session_id])
        return augmented

    def _build_env(self, call: AdapterCall) -> dict[str, str]:
        env = super()._build_env(call)
        env["DSH_PERMISSION_MODE"] = "read-only" if call.read_only else "danger-full-access"
        # The headless patch reads this temporary PTC opt-in; leaving it unset
        # leaves the agent without bash/fs tools.
        env.setdefault("DSH_TOOLS_MODE", "native")
        return env

    def _prepare_dsh_overlay(self, call: AdapterCall) -> Path | None:
        if not self._uses_headless_profile or not self.harness_config.extra.get("overlay_enabled", True):
            return None
        desktop_patch = self._dsh_home() / "profiles" / "desktop" / "cordis.patch.yml"
        preserved: dict[str, dict[str, Any]] = {}
        if desktop_patch.exists():
            try:
                loaded = yaml.safe_load(desktop_patch.read_text(encoding="utf-8")) or []
            except (OSError, yaml.YAMLError):
                loaded = []
            if isinstance(loaded, list):
                for entry in loaded:
                    if isinstance(entry, dict) and entry.get("id") in {"llm-pi-ai", "agent-default-model"}:
                        preserved[str(entry["id"])] = dict(entry)
        agent_entry = preserved.get(
            "agent-default-model",
            {
                "id": "agent-default-model",
                "name": "@deepseek-ai/dsh-agent-default-model",
                "config": {},
            },
        )
        agent_config = dict(agent_entry.get("config") or {})
        if call.model and call.model != "<cli_active_default>":
            agent_config["model"] = call.model
        if call.reasoning_effort:
            agent_config["reasoningEffort"] = call.reasoning_effort
        agent_entry["config"] = agent_config
        llm_entry = preserved.get(
            "llm-pi-ai",
            {"id": "llm-pi-ai", "name": "@deepseek-ai/dsh-llm-pi-ai"},
        )
        overlay_entries = [
            {
                "id": "credentials",
                "name": "@deepseek-ai/dsh-credentials-local",
                "disabled": False,
            },
            llm_entry,
            agent_entry,
        ]
        temp_dir = Path(tempfile.mkdtemp(prefix="harness-mesh-dsh-"))
        overlay_path = temp_dir / f"dsh-overlay-{call.assignment_id}.yml"
        overlay_path.write_text(
            yaml.safe_dump(overlay_entries, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        self._overlays[call.assignment_id] = (temp_dir, overlay_path)
        return overlay_path

    def _cleanup_overlay(self, assignment_id: str) -> None:
        overlay = self._overlays.pop(assignment_id, None)
        if overlay is not None:
            shutil.rmtree(overlay[0], ignore_errors=True)

    def _parse_output(self, outcome: AdapterOutcome, call: AdapterCall) -> None:
        text = (outcome.stdout or "").strip()
        events: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        if events:
            messages: list[str] = []
            errors: list[str] = []
            session_id: str | None = None
            for event in events:
                event_type = str(event.get("type") or "")
                if event_type in {"session", "session.started", "thread.started"}:
                    value = event.get("sessionId") or event.get("session_id") or event.get("id")
                    if isinstance(value, str):
                        session_id = value
                elif event_type == "final":
                    value = event.get("text") or event.get("response") or event.get("content")
                    if isinstance(value, str):
                        messages = [value]
                elif event_type == "text":
                    value = event.get("text") or event.get("content")
                    if isinstance(value, str):
                        messages.append(value)
                elif event_type in {"error", "turn.failed"} or (
                    isinstance(event.get("reason"), dict)
                    and event["reason"].get("kind") == "error"
                ):
                    detail = event.get("message") or event.get("error") or event.get("reason")
                    if isinstance(detail, dict):
                        detail = detail.get("message") or json.dumps(detail)
                    errors.append(str(detail or "dsh error"))
            if session_id:
                outcome.session_id = session_id
            if messages:
                outcome.parsed_output = "\n".join(item for item in messages if item).strip()
            elif not errors:
                outcome.parsed_output = text
            if errors:
                outcome.stderr = (outcome.stderr + "\n" + "\n".join(errors)).strip()
                outcome.error_detail = errors[-1][:500]
                if outcome.exit_code == 0:
                    outcome.exit_code = 1
            return
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                for key in ("result", "output", "content", "message", "text", "answer"):
                    value = payload.get(key)
                    if isinstance(value, str) and value.strip():
                        outcome.parsed_output = value
                        break
                else:
                    outcome.parsed_output = text
                session_id = (
                    payload.get("session_id")
                    or payload.get("sessionId")
                    or payload.get("thread_id")
                    or payload.get("id")
                )
                if isinstance(session_id, str):
                    outcome.session_id = session_id
                if payload.get("error"):
                    outcome.error_detail = str(payload["error"])[:500]
                    if outcome.exit_code == 0:
                        outcome.exit_code = 1
                return
        outcome.parsed_output = text


class _BuiltinDeepSeekRuntime:
    """Small deterministic tool-calling loop for the built-in DeepSeek channel."""

    def __init__(self, adapter: DeepSeekAdapter, call: AdapterCall, *, api_key: str):
        self.adapter = adapter
        self.call = call
        self.api_key = api_key
        self.workdir = Path(call.workdir).resolve()
        self.max_turns = max(1, int(adapter.harness_config.extra.get("builtin_max_turns", 12)))
        self.transcript_parts: list[str] = []

    def _client(self) -> tuple[httpx.AsyncClient, bool]:
        if self.adapter.http_client is not None:
            return self.adapter.http_client, False
        client = httpx.AsyncClient(
            transport=self.adapter.http_transport,
            timeout=httpx.Timeout(300.0),
        )
        return client, True

    async def run(self) -> AdapterOutcome:
        model = self.call.model
        if not model or model == "<cli_active_default>":
            return AdapterOutcome(
                exit_code=2,
                stderr="built-in DeepSeek runtime requires a concrete model id",
                error_detail="missing_model",
            )
        read_only = bool(self.call.read_only)
        if read_only:
            duties = "This assignment is READ-ONLY: inspect files and report only, never write."
        else:
            duties = (
                "Use the provided tools to inspect and edit files. Never write outside the working directory."
            )
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are an autonomous coding assignment. "
                    f"The working directory is {self.workdir}. {duties} "
                    "When the objective is fully complete, reply with a concise summary of changed files "
                    "and remaining risks."
                    + (f"\n\n{self.call.system_prompt}" if self.call.system_prompt else "")
                ),
            },
            {"role": "user", "content": self.call.prompt},
        ]
        tools = (
            [schema for schema in _TOOL_SCHEMAS if schema["function"]["name"] in {"read_file", "list_files"}]
            if read_only
            else _TOOL_SCHEMAS
        )
        client, created = self._client()
        last_error = ""
        try:
            for turn in range(1, self.max_turns + 1):
                try:
                    response = await client.post(
                        self.adapter._api_endpoint("/chat/completions"),
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=self._request_payload(model, messages, tools),
                    )
                except httpx.HTTPError as exc:
                    return AdapterOutcome(exit_code=1, stderr=str(exc), error_detail=f"http_error: {exc}")
                if response.status_code >= 400:
                    body = response.text[:4000]
                    return AdapterOutcome(
                        exit_code=response.status_code,
                        stderr=body,
                        stdout=body,
                        error_detail=f"HTTP {response.status_code}",
                        metadata={"turns": turn},
                    )
                try:
                    payload = response.json()
                except ValueError:
                    return AdapterOutcome(exit_code=1, stderr="invalid JSON from model endpoint")
                session_id = payload.get("id")
                choices = payload.get("choices") or []
                if not choices:
                    return AdapterOutcome(exit_code=1, stderr="model returned no choices")
                message = (choices[0] or {}).get("message") or {}
                content = message.get("content") or ""
                tool_calls = message.get("tool_calls") or []
                self.transcript_parts.append(f"[turn {turn}] {content[:1000]}")
                if not tool_calls:
                    final = content.strip()
                    return AdapterOutcome(
                        exit_code=0,
                        stdout=final,
                        parsed_output=final,
                        session_id=session_id if isinstance(session_id, str) else None,
                        metadata={"turns": turn, "transcript": "\n".join(self.transcript_parts)},
                    )
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content"),
                        "tool_calls": tool_calls,
                    }
                )
                for tool_call in tool_calls:
                    function = (tool_call or {}).get("function") or {}
                    name = str(function.get("name") or "")
                    raw_args = function.get("arguments") or "{}"
                    try:
                        arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        arguments = {}
                    result = await self._execute_tool(name, arguments)
                    last_error = "" if not result.startswith("[tool error]") else result
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": (tool_call or {}).get("id") or name,
                            "content": result[:12000],
                        }
                    )
            return AdapterOutcome(
                exit_code=1,
                stderr=last_error or f"built-in runtime exceeded {self.max_turns} turns",
                error_detail="max_turns_exceeded",
                metadata={"turns": self.max_turns, "transcript": "\n".join(self.transcript_parts)},
            )
        finally:
            if created:
                await client.aclose()

    def _request_payload(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "temperature": 0.1,
        }
        if self.call.reasoning_effort:
            payload["reasoning_effort"] = self.call.reasoning_effort
        return payload

    async def _execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            if name == "read_file":
                return self._read_file(arguments)
            if name == "write_file":
                return self._write_file(arguments)
            if name == "apply_patch":
                return self._apply_patch(arguments)
            if name == "list_files":
                return self._list_files(arguments)
            if name == "run_command":
                return await self._run_command(arguments)
            return f"[tool error] unknown tool: {name}"
        except Exception as exc:
            return f"[tool error] {exc}"

    def _safe_path(self, raw: str) -> Path:
        candidate = Path(raw or ".").expanduser()
        if not candidate.is_absolute():
            candidate = self.workdir / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.workdir)
        except ValueError as exc:
            raise ValueError(f"path escapes worktree: {raw}") from exc
        return resolved

    def _read_file(self, arguments: dict[str, Any]) -> str:
        path = self._safe_path(str(arguments.get("path") or ""))
        if not path.exists() or not path.is_file():
            return f"[tool error] file not found: {arguments.get('path')}"
        offset = max(1, int(arguments.get("offset") or 1))
        limit = max(1, min(int(arguments.get("limit") or 400), 2000))
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        chunk = lines[offset - 1 : offset - 1 + limit]
        numbered = [f"{offset + index}: {line}" for index, line in enumerate(chunk)]
        return "\n".join(numbered) if numbered else "(empty)"

    def _write_file(self, arguments: dict[str, Any]) -> str:
        path = self._safe_path(str(arguments.get("path") or ""))
        content = str(arguments.get("content") or "")
        if len(content) > 2_000_000:
            return "[tool error] content exceeds 2MB limit"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} characters to {path.relative_to(self.workdir)}"

    def _apply_patch(self, arguments: dict[str, Any]) -> str:
        path = self._safe_path(str(arguments.get("path") or ""))
        old = str(arguments.get("old_text") or "")
        new = str(arguments.get("new_text") or "")
        if not path.exists():
            return f"[tool error] file not found: {arguments.get('path')}"
        if not old:
            return "[tool error] old_text is required"
        text = path.read_text(encoding="utf-8")
        if old not in text:
            return "[tool error] old_text was not found in the file"
        updated = text.replace(old, new, 1)
        path.write_text(updated, encoding="utf-8")
        return f"patched {path.relative_to(self.workdir)}"

    def _list_files(self, arguments: dict[str, Any]) -> str:
        base = self._safe_path(str(arguments.get("path") or "."))
        if not base.exists():
            return f"[tool error] path not found: {arguments.get('path')}"
        rows: list[str] = []
        items = [base] if base.is_file() else sorted(base.rglob("*"))
        for item in items:
            if ".git" in item.parts:
                continue
            try:
                relative = item.relative_to(self.workdir)
            except ValueError:
                continue
            rows.append(relative.as_posix() + ("/" if item.is_dir() else ""))
            if len(rows) >= 400:
                rows.append("... (truncated)")
                break
        return "\n".join(rows) if rows else "(empty)"

    async def _run_command(self, arguments: dict[str, Any]) -> str:
        command = str(arguments.get("command") or "").strip()
        if not command:
            return "[tool error] command is required"
        timeout = max(1, min(int(arguments.get("timeout") or 120), 300))
        env = dict(self.adapter.env)
        env.update({"HARNESS_MESH_TASK_ID": self.call.task_id, "HARNESS_MESH_ASSIGNMENT_ID": self.call.assignment_id})
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            return f"[tool error] {exc}"
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.CancelledError:
            await self._kill_shell_process(process)
            raise
        except asyncio.TimeoutError:
            await self._kill_shell_process(process)
            return f"[tool error] command timed out after {timeout}s"
        output = stdout.decode("utf-8", errors="replace")
        error_output = stderr.decode("utf-8", errors="replace")
        combined = output[-8000:]
        if error_output:
            combined += "\n[stderr]\n" + error_output[-4000:]
        return f"exit_code={process.returncode}\n{combined}".strip()

    @staticmethod
    async def _kill_shell_process(process: asyncio.subprocess.Process) -> None:
        try:
            if process.returncode is None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
                await process.wait()
        except ProcessLookupError:
            pass


_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file inside the worktree with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from the worktree root."},
                    "offset": {"type": "integer", "description": "1-based first line."},
                    "limit": {"type": "integer", "description": "Maximum number of lines."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file inside the worktree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Replace one exact block of text in a file (surgical edit).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files under a directory inside the worktree.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the worktree and return its output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
        },
    },
]


__all__ = ["DeepSeekAdapter"]
