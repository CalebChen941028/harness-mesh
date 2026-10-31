"""OpenAI Codex CLI adapter (``codex exec``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import AdapterCall, AdapterOutcome, BaseHarnessAdapter


class CodexAdapter(BaseHarnessAdapter):
    harness_name = "codex"

    def _augment_command(self, args: list[str], call: AdapterCall) -> list[str]:
        globals_: list[str] = []
        exec_flags: list[str] = []
        if call.reasoning_effort:
            globals_.extend(["-c", f'model_reasoning_effort="{call.reasoning_effort}"'])
        if (
            call.mcp_config_path
            and Path(call.mcp_config_path).exists()
            and "-c" not in args
            and "--config" not in args
        ):
            try:
                payload = json.loads(Path(call.mcp_config_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            servers = payload.get("mcpServers") or payload.get("mcp_servers") or {}
            if isinstance(servers, dict):
                for name, server in servers.items():
                    if isinstance(server, dict):
                        globals_.extend(["-c", f"mcp_servers.{name}={_to_toml_inline(server)}"])
        if call.read_only:
            if "--sandbox" not in args:
                exec_flags.extend(["--sandbox", "read-only"])
        elif (
            self.harness_config.auto_approve
            and "--full-auto" not in args
            and "--sandbox" not in args
            and "--ask-for-approval" not in args
        ):
            exec_flags.extend(self._auto_approve_args(["--full-auto"]))

        augmented = list(args)
        if augmented and globals_:
            augmented = [augmented[0], *globals_, *augmented[1:]]
        if exec_flags and augmented:
            if "exec" in augmented:
                index = augmented.index("exec") + 1
                augmented = [*augmented[:index], *exec_flags, *augmented[index:]]
            else:
                augmented = [augmented[0], *exec_flags, *augmented[1:]]
        if call.session_id and "resume" not in augmented:
            # Codex resumes through a subcommand: codex exec [options] resume <id> <prompt>
            if len(augmented) > 1:
                augmented = [*augmented[:-1], "resume", call.session_id, augmented[-1]]
            else:
                augmented.extend(["resume", call.session_id])
        return augmented

    def _parse_output(self, outcome: AdapterOutcome, call: AdapterCall) -> None:
        text = outcome.stdout or ""
        messages: list[str] = []
        errors: list[str] = []
        session_id: str | None = None
        parsed_any = False
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            parsed_any = True
            event_type = event.get("type")
            if event_type in {"thread.started", "session.created", "session.started"}:
                session = event.get("session") if isinstance(event.get("session"), dict) else {}
                session_id = (
                    event.get("thread_id")
                    or event.get("session_id")
                    or session.get("id")
                )
            elif event_type == "item.completed":
                item = event.get("item") or {}
                if isinstance(item, dict):
                    if item.get("type") in {"agent_message", "assistant_message"}:
                        value = item.get("text") or item.get("content")
                        if isinstance(value, str):
                            messages.append(value)
                    elif item.get("type") == "error":
                        errors.append(str(item.get("message") or item.get("text") or "codex item error"))
            elif event_type in {"message", "assistant_message"}:
                if event.get("role") in {None, "assistant"}:
                    content = event.get("content")
                    if isinstance(content, str):
                        messages.append(content)
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and isinstance(part.get("text"), str):
                                messages.append(part["text"])
            elif event_type in {"error", "turn.failed"}:
                error_value = event.get("error") or event.get("message")
                if isinstance(error_value, dict):
                    error_value = error_value.get("message") or json.dumps(error_value)
                errors.append(str(error_value or "codex error"))
            elif event_type == "result" and isinstance(event.get("result"), str):
                messages.append(event["result"])
        if messages:
            outcome.parsed_output = "\n\n".join(messages).strip()
        elif parsed_any:
            outcome.parsed_output = ""
        else:
            outcome.parsed_output = text.strip()
        if session_id:
            outcome.session_id = str(session_id)
        if errors:
            outcome.stderr = (outcome.stderr + "\n" + "\n".join(errors)).strip()
            if outcome.exit_code == 0 and not messages:
                outcome.exit_code = 1
                outcome.error_detail = errors[-1][:500]
        if outcome.exit_code == 0 and parsed_any and not messages and not errors:
            outcome.error_detail = "codex emitted no assistant message"


def _to_toml_inline(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_to_toml_inline(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key} = {_to_toml_inline(item)}" for key, item in value.items()) + "}"
    return json.dumps(str(value))


__all__ = ["CodexAdapter"]
