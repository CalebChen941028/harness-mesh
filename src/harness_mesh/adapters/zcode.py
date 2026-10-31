"""ZCode CLI adapter (``zcode``)."""

from __future__ import annotations

import json

from .base import AdapterCall, AdapterOutcome, BaseHarnessAdapter


class ZCodeAdapter(BaseHarnessAdapter):
    harness_name = "zcode"

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
                extra.extend(self._auto_approve_args(["--mode", "yolo"]))
        if call.session_id and "--resume" not in args:
            extra.extend(["--resume", call.session_id])
        if extra:
            if len(args) > 1:
                return [*args[:-1], *extra, args[-1]]
            return [*args, *extra]
        return args

    def _parse_output(self, outcome: AdapterOutcome, call: AdapterCall) -> None:
        text = (outcome.stdout or "").strip()
        # The CLI may print "ZCode Built-in updated revision=..." before JSON.
        start = text.find("{")
        payload = None
        if start >= 0:
            candidate = text[start:]
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                payload = None
        if isinstance(payload, dict):
            for key in ("response", "result", "output", "content", "message", "text"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    outcome.parsed_output = value
                    break
            if not outcome.parsed_output:
                outcome.parsed_output = text
            session_id = (
                payload.get("sessionId")
                or payload.get("session_id")
                or payload.get("thread_id")
            )
            if isinstance(session_id, str):
                outcome.session_id = session_id
            if payload.get("error"):
                error_value = payload["error"]
                if isinstance(error_value, dict):
                    error_value = error_value.get("message") or json.dumps(error_value)
                outcome.stderr = (outcome.stderr + "\n" + str(error_value)).strip()
                if outcome.exit_code == 0:
                    outcome.exit_code = 1
                    outcome.error_detail = str(error_value)[:500]
            return
        # ZCode can also emit JSONL event streams.
        messages: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                for key in ("response", "result", "output", "content", "text"):
                    if isinstance(event.get(key), str):
                        messages.append(event[key])
                        break
        outcome.parsed_output = "\n\n".join(messages).strip() if messages else text


__all__ = ["ZCodeAdapter"]
