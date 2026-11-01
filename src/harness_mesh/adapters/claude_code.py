"""Claude Code CLI adapter (``claude``)."""

from __future__ import annotations

import json

from .base import AdapterCall, AdapterOutcome, BaseHarnessAdapter


class ClaudeCodeAdapter(BaseHarnessAdapter):
    harness_name = "claude_code"

    def _augment_command(self, args: list[str], call: AdapterCall) -> list[str]:
        extra: list[str] = []
        if call.read_only:
            if "--permission-mode" not in args:
                extra.extend(["--permission-mode", "plan"])
        elif (
            self._auto_approval_allowed(call)
            and "--dangerously-skip-permissions" not in args
            and "--permission-mode" not in args
        ):
            extra.extend(self._auto_approve_args(["--dangerously-skip-permissions"]))
        if call.session_id and "--resume" not in args:
            extra.extend(["--resume", call.session_id])
        return [*args, *extra] if extra else args

    def _parse_output(self, outcome: AdapterOutcome, call: AdapterCall) -> None:
        text = (outcome.stdout or "").strip()
        payload = None
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
        if not isinstance(payload, dict):
            # Some versions emit one JSON object per line.
            events = []
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            for event in events:
                if isinstance(event, dict) and isinstance(event.get("result"), str):
                    payload = event
            if payload is None:
                outcome.parsed_output = text
                return

        session_id = payload.get("session_id")
        if isinstance(session_id, str):
            outcome.session_id = session_id
        result = payload.get("result")
        if isinstance(result, str):
            outcome.parsed_output = result
        else:
            outcome.parsed_output = text
        outcome.metadata["usage"] = payload.get("usage") or {}
        if payload.get("is_error"):
            detail = payload.get("result") or payload.get("error") or "claude reported an error"
            outcome.stderr = (outcome.stderr + "\n" + str(detail)).strip()
            outcome.error_detail = str(detail)[:500]
            if outcome.exit_code == 0:
                outcome.exit_code = 1
        elif payload.get("subtype") == "error":
            detail = payload.get("result") or payload.get("error") or "claude error response"
            outcome.stderr = (outcome.stderr + "\n" + str(detail)).strip()
            outcome.error_detail = str(detail)[:500]
            if outcome.exit_code == 0:
                outcome.exit_code = 1


__all__ = ["ClaudeCodeAdapter"]
