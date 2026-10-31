"""Universal harness adapters and the factory used by the router."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from ..config import MeshConfig
from .antigravity import AntigravityAdapter
from .base import AdapterCall, AdapterOutcome, BaseHarnessAdapter, render_command
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .deepseek import DeepSeekAdapter
from .zcode import ZCodeAdapter

ADAPTER_CLASSES: dict[str, type[BaseHarnessAdapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "deepseek": DeepSeekAdapter,
    "antigravity": AntigravityAdapter,
    "zcode": ZCodeAdapter,
}


def build_adapters(
    config: MeshConfig,
    workspace_root: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    runner: Any | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    http_client: httpx.AsyncClient | None = None,
    now_fn: Callable[[], float] | None = None,
) -> dict[str, BaseHarnessAdapter]:
    adapters: dict[str, BaseHarnessAdapter] = {}
    for name, harness_config in config.harnesses.items():
        if not harness_config.enabled:
            continue
        adapter_class = ADAPTER_CLASSES.get(name)
        if adapter_class is None:
            continue
        kwargs: dict[str, Any] = {
            "env": env,
            "which": which,
            "runner": runner,
            "now_fn": now_fn,
        }
        if name == "deepseek":
            kwargs["http_transport"] = http_transport
            kwargs["http_client"] = http_client
        adapters[name] = adapter_class(config, harness_config, workspace_root, **kwargs)
    return adapters


__all__ = [
    "ADAPTER_CLASSES",
    "AdapterCall",
    "AdapterOutcome",
    "AntigravityAdapter",
    "BaseHarnessAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "DeepSeekAdapter",
    "ZCodeAdapter",
    "build_adapters",
    "render_command",
]
