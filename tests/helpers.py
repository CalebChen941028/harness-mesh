"""Shared fixtures and fakes for the harness-mesh test suite."""

from __future__ import annotations

import inspect
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from harness_mesh.adapters.base import AdapterCall, AdapterOutcome
from harness_mesh.config import MeshConfig, default_config
from harness_mesh.context import ProjectContextManager
from harness_mesh.discovery import ModelDiscoveryEngine
from harness_mesh.models import HarnessStatus
from harness_mesh.quota import QuotaGuardian, QuotaRecoveryMonitor
from harness_mesh.router import MeshRouter
from harness_mesh.skills import SkillPluginManager
from harness_mesh.storage import StateStore
from harness_mesh.workspace import WorkspaceManager


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_claude_settings(
    home: Path,
    *,
    model: str | None = None,
    env: dict[str, str] | None = None,
    include_api_key: bool = False,
) -> None:
    payload: dict[str, Any] = {}
    if model:
        payload["model"] = model
    if env or include_api_key:
        env_payload = dict(env or {})
        if include_api_key:
            env_payload.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
            env_payload.setdefault("ANTHROPIC_BASE_URL", "https://anthropic.test")
        payload["env"] = env_payload
    write_json(home / ".claude" / "settings.json", payload)


def write_codex_config(
    home: Path,
    *,
    model: str | None = None,
    profiles: dict[str, str] | None = None,
    provider: str = "openai",
) -> None:
    lines: list[str] = []
    if provider:
        lines.append(f'model_provider = "{provider}"')
    if model:
        lines.append(f'model = "{model}"')
    for name, profile_model in (profiles or {}).items():
        lines.append(f"[profiles.{name}]")
        lines.append(f'model = "{profile_model}"')
    write_text(home / ".codex" / "config.toml", "\n".join(lines) + "\n")


def write_dsh_config(home: Path, *, models: list[str], default_model: str | None = None) -> None:
    lines = ["providers:", "  deepseek:", "    models:"]
    for model in models:
        lines.append(f"      {model}:")
        lines.append("        context_window: 128000")
    if default_model:
        lines.append(f"model: {default_model}")
    write_text(home / ".dsh" / "profiles" / "default" / "cordis.patch.yml", "\n".join(lines) + "\n")


def write_gemini_settings(home: Path, *, model: str) -> None:
    write_json(home / ".gemini" / "settings.json", {"model": model})


def write_zcode_config(home: Path, *, model: str) -> None:
    write_text(home / ".zcode" / "config.yaml", f"model: {model}\nprovider: zcode\n")


def init_git_repo(root: Path, files: dict[str, str] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in (files or {}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=root, check=True)


class FakeAdapter:
    """Adapter double used by router scenarios.

    The handler receives ``(adapter, call)`` and may return an
    :class:`AdapterOutcome`, an awaitable or a plain string.
    """

    def __init__(
        self,
        harness: str,
        handler: Callable[["FakeAdapter", AdapterCall], Any],
        *,
        available: bool = True,
    ):
        self.harness_name = harness
        self.handler = handler
        self.available = available
        self.calls: list[AdapterCall] = []

    async def execute(self, call: AdapterCall) -> Any:
        self.calls.append(call)
        result = self.handler(self, call)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def detect(self) -> HarnessStatus:
        return HarnessStatus(
            harness=self.harness_name,
            available=self.available,
            mode="fake",
            version_or_detail="fake adapter",
            authorized_models=[],
        )

    async def probe(self) -> tuple[bool, str]:
        return True, "fake probe ok"


def success(text: str = "assignment completed", *, session_id: str | None = None) -> AdapterOutcome:
    return AdapterOutcome(
        exit_code=0,
        stdout=text,
        parsed_output=text,
        session_id=session_id,
        duration_ms=5,
    )


def failure(
    message: str,
    *,
    exit_code: int = 1,
    stderr: str | None = None,
) -> AdapterOutcome:
    return AdapterOutcome(
        exit_code=exit_code,
        stdout="",
        stderr=stderr if stderr is not None else message,
        error_detail=message,
        duration_ms=5,
    )


def quota_failure(
    message: str = "HTTP 429 too many requests: rate_limit_exceeded",
    *,
    exit_code: int = 1,
    stdout: str = "",
) -> AdapterOutcome:
    return AdapterOutcome(
        exit_code=exit_code,
        stdout=stdout,
        stderr=message,
        duration_ms=5,
    )


@dataclass
class MeshHarness:
    config: MeshConfig
    storage: StateStore
    discovery: ModelDiscoveryEngine
    skills: SkillPluginManager
    context: ProjectContextManager
    workspace: WorkspaceManager
    guardian: QuotaGuardian
    monitor: QuotaRecoveryMonitor
    router: MeshRouter
    workspace_root: Path
    home_dir: Path
    env: dict[str, str]


def make_mesh(
    base_dir: Path,
    *,
    adapters: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    probe: Any | None = None,
    config_updates: dict[str, Any] | None = None,
    http_transport: Any | None = None,
    command_runner: Any | None = None,
    module_available: Any | None = None,
) -> MeshHarness:
    home = base_dir / "home"
    workspace = base_dir / "workspace"
    home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)

    config = default_config()
    if config_updates:
        for key, value in config_updates.items():
            setattr(config, key, value)

    env_map = {
        "HOME": str(home),
        "DSH_HOME": str(home / ".dsh"),
    }
    if env:
        env_map.update(env)

    state_dir = workspace / config.state_dir
    storage = StateStore(state_dir / "state.db")
    discovery = ModelDiscoveryEngine(
        config,
        storage,
        workspace,
        home_dir=home,
        env=env_map,
        which=which or (lambda name: None),
        command_runner=command_runner,
        module_available=module_available,
        http_transport=http_transport,
        cache_ttl_seconds=300,
    )
    skills = SkillPluginManager(config, workspace, home_dir=home, env=env_map)
    workspace_manager = WorkspaceManager(workspace, config.state_dir, auto_init_repo=True)
    guardian = QuotaGuardian(
        storage,
        cooldown_seconds=1,
        failover_priority=config.failover_priority,
    )
    router = MeshRouter(
        config,
        storage,
        discovery=discovery,
        skills=skills,
        workspace=workspace_manager,
        adapters=adapters or {},
        guardian=guardian,
        workspace_root=workspace,
        env=env_map,
        home_dir=home,
        probe=probe,
    )
    return MeshHarness(
        config=config,
        storage=storage,
        discovery=discovery,
        skills=skills,
        context=router.context,
        workspace=workspace_manager,
        guardian=guardian,
        monitor=router.monitor,
        router=router,
        workspace_root=workspace,
        home_dir=home,
        env=env_map,
    )


def make_skill(root: Path, name: str, description: str, tags: list[str] | None = None, body: str = "") -> Path:
    tag_line = ", ".join(tags or [])
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"tags: [{tag_line}]\n"
        "---\n\n"
        f"# {name}\n\n{body or description}\n"
    )
    path.write_text(content, encoding="utf-8")
    return path
