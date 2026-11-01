"""MCP tools for reviewing and applying harness integrations."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import harness_mesh.mcp_server as mcp_server
from harness_mesh.approvals import TOOL_APPROVAL_CLASSES
from harness_mesh.installer import HarnessInstaller
from harness_mesh.mcp_server import TOOL_NAMES, create_server
from tests.helpers import make_mesh

BINARIES = {
    "claude_code": "claude",
    "codex": "codex",
    "deepseek": "dsh",
    "antigravity": "agy",
    "zcode": "zcode",
}


def _fake_binary(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _prepare(mesh, tmp_path: Path, detected: tuple[str, ...]) -> None:
    for harness, name in BINARIES.items():
        cfg = mesh.config.harnesses[harness]
        cfg.binary = str(tmp_path / "missing" / name)
        cfg.binary_paths = []
        if harness == "zcode":
            cfg.extra["macos_electron_bin"] = str(tmp_path / "missing" / "ZCode")
            cfg.extra["macos_cjs_entry"] = str(tmp_path / "missing" / "zcode.cjs")
    for harness in detected:
        mesh.config.harnesses[harness].binary = str(
            _fake_binary(tmp_path / "bin", BINARIES[harness])
        )


async def _call(server, name: str, arguments: dict | None = None):
    result = await server.call_tool(name, arguments or {})
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def _server(mesh):
    return create_server(mesh.router)


# ----------------------------------------------------------------------
# registration
# ----------------------------------------------------------------------
def test_integration_tools_are_registered_and_classified():
    assert {
        "get_integration_status",
        "plan_integration",
        "install_integration",
    } <= set(TOOL_NAMES)
    assert TOOL_APPROVAL_CLASSES["get_integration_status"] == "read"
    assert TOOL_APPROVAL_CLASSES["plan_integration"] == "read"
    assert TOOL_APPROVAL_CLASSES["install_integration"] == "write"


def test_mesh_instructions_require_consent_for_installation():
    instructions = mcp_server.MESH_INSTRUCTIONS
    assert "integration_hints" in instructions
    assert "plan_integration" in instructions
    assert "install_integration" in instructions
    assert "consent" in instructions.lower() or "agree" in instructions.lower()


# ----------------------------------------------------------------------
# plan then install
# ----------------------------------------------------------------------
async def test_plan_does_not_write_and_install_writes_after_consent(tmp_path):
    mesh = make_mesh(tmp_path)
    _prepare(mesh, tmp_path, ("claude_code",))
    settings = mesh.home_dir / ".claude" / "settings.json"
    try:
        server = _server(mesh)
        plan = await _call(
            server,
            "plan_integration",
            {"harness": "claude_code", "approve_level": "dispatch"},
        )
        assert isinstance(plan["plan_id"], str) and len(plan["plan_id"]) == 16
        assert plan["detected"] is True
        assert plan["registered"] is False
        assert plan["action"] == "would_install"
        assert plan["restart_required"] is True
        assert any(entry.endswith("dispatch_task") for entry in plan["entries"])
        assert not settings.exists()

        installed = await _call(
            server,
            "install_integration",
            {
                "harness": "claude_code",
                "approve_level": "dispatch",
                "plan_id": plan["plan_id"],
            },
        )
        assert installed["installed"] is True, installed
        assert installed["restart_required"] is True
        assert installed["plan_id"] == plan["plan_id"]
        assert str(settings) in installed["files"]
        assert installed["entries"] == plan["entries"]
        payload = json.loads(settings.read_text(encoding="utf-8"))
        managed = [
            entry
            for entry in payload["permissions"]["allow"]
            if entry.startswith("mcp__harness-mesh__")
        ]
        assert len(managed) == 14
        assert "mcp__harness-mesh__install_integration" not in managed
    finally:
        mesh.storage.close()


async def test_install_matches_hmesh_install_for_the_same_harness(tmp_path):
    mcp_mesh = make_mesh(tmp_path / "mcp")
    _prepare(mcp_mesh, tmp_path / "mcp", ("claude_code",))
    cli_mesh = make_mesh(tmp_path / "cli")
    _prepare(cli_mesh, tmp_path / "cli", ("claude_code",))
    try:
        server = _server(mcp_mesh)
        plan = await _call(
            server,
            "plan_integration",
            {"harness": "claude_code", "approve_level": "dispatch"},
        )
        installed = await _call(
            server,
            "install_integration",
            {
                "harness": "claude_code",
                "approve_level": "dispatch",
                "plan_id": plan["plan_id"],
            },
        )
        direct = HarnessInstaller(
            cli_mesh.config,
            cli_mesh.workspace_root,
            home_dir=cli_mesh.home_dir,
            env=cli_mesh.env,
            which=lambda name: None,
            config_path=None,
            approval_level="dispatch",
        ).install(["claude_code"])[0]

        assert installed["installed"] is True
        mcp_settings = mcp_mesh.home_dir / ".claude" / "settings.json"
        cli_settings = cli_mesh.home_dir / ".claude" / "settings.json"
        assert json.loads(mcp_settings.read_text(encoding="utf-8")) == json.loads(
            cli_settings.read_text(encoding="utf-8")
        )
        assert [Path(path).name for path in installed["files"]] == [
            Path(path).name for path in direct.files
        ]
        assert len(installed["entries"]) == len(direct.approvals["entries"])
    finally:
        mcp_mesh.storage.close()
        cli_mesh.storage.close()


async def test_install_integration_runs_through_the_injected_runner(
    tmp_path, monkeypatch
):
    mesh = make_mesh(tmp_path)
    _prepare(mesh, tmp_path, ("claude_code",))
    calls: list[list[str]] = []

    def fake_runner(argv, cwd, env):
        calls.append(list(argv))
        return 0, "", ""

    def factory(router, *, approve_level="dispatch", dry_run=False):
        return HarnessInstaller(
            router.config,
            router.workspace_root,
            home_dir=router.home_dir,
            env=router.env,
            which=lambda name: None,
            runner=fake_runner,
            config_path=None,
            dry_run=dry_run,
            allow_system_paths=False,
            approval_level=approve_level,
        )

    monkeypatch.setattr(mcp_server, "_installation", factory)
    settings = mesh.home_dir / ".claude" / "settings.json"
    try:
        server = _server(mesh)
        plan = await _call(
            server, "plan_integration", {"harness": "claude_code"}
        )
        assert calls == []
        result = await _call(
            server,
            "install_integration",
            {
                "harness": "claude_code",
                "approve_level": "dispatch",
                "plan_id": plan["plan_id"],
            },
        )
        assert result["installed"] is True, result
        assert any("mcp" in call and "add-json" in call for call in calls)
        assert settings.exists()
    finally:
        mesh.storage.close()


async def test_install_rejects_a_stale_plan_id_without_writing(tmp_path):
    mesh = make_mesh(tmp_path)
    _prepare(mesh, tmp_path, ("claude_code",))
    settings = mesh.home_dir / ".claude" / "settings.json"
    try:
        server = _server(mesh)
        plan = await _call(
            server, "plan_integration", {"harness": "claude_code"}
        )
        result = await _call(
            server,
            "install_integration",
            {
                "harness": "claude_code",
                "approve_level": "dispatch",
                "plan_id": "0" * 16,
            },
        )
        assert result["installed"] is False
        assert "plan_id" in result["error"]
        assert result["plan_id"] == plan["plan_id"]
        assert not settings.exists()
        assert plan["plan_id"] != "0" * 16
    finally:
        mesh.storage.close()


async def test_install_rejects_workers(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    _prepare(mesh, tmp_path, ("claude_code",))
    settings = mesh.home_dir / ".claude" / "settings.json"
    try:
        server = _server(mesh)
        plan = await _call(
            server, "plan_integration", {"harness": "claude_code"}
        )
        monkeypatch.setattr(mcp_server, "_task_id_from_cwd", lambda router: "task-123")
        result = await _call(
            server,
            "install_integration",
            {
                "harness": "claude_code",
                "approve_level": "dispatch",
                "plan_id": plan["plan_id"],
            },
        )
        assert result["installed"] is False
        assert "Workers cannot change harness integrations" in result["error"]
        assert not settings.exists()
    finally:
        mesh.storage.close()


async def test_install_rejects_read_only_mode(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    _prepare(mesh, tmp_path, ("claude_code",))
    settings = mesh.home_dir / ".claude" / "settings.json"
    try:
        server = _server(mesh)
        plan = await _call(
            server, "plan_integration", {"harness": "claude_code"}
        )
        monkeypatch.setenv("HARNESS_MESH_READ_ONLY", "1")
        result = await _call(
            server,
            "install_integration",
            {
                "harness": "claude_code",
                "approve_level": "dispatch",
                "plan_id": plan["plan_id"],
            },
        )
        assert result["installed"] is False
        assert "read-only" in result["error"]
        assert not settings.exists()
    finally:
        mesh.storage.close()


async def test_install_reports_a_harness_that_is_not_detected(tmp_path):
    mesh = make_mesh(tmp_path)
    _prepare(mesh, tmp_path, ())
    try:
        server = _server(mesh)
        plan = await _call(
            server, "plan_integration", {"harness": "claude_code"}
        )
        result = await _call(
            server,
            "install_integration",
            {
                "harness": "claude_code",
                "approve_level": "dispatch",
                "plan_id": plan["plan_id"],
            },
        )
        assert result["installed"] is False
        assert "binary not found" in result["error"] or "cannot proceed" in result["error"]
    finally:
        mesh.storage.close()


# ----------------------------------------------------------------------
# status and hints
# ----------------------------------------------------------------------
async def test_get_integration_status_lists_every_enabled_harness(tmp_path):
    mesh = make_mesh(tmp_path)
    _prepare(mesh, tmp_path, ("claude_code",))
    try:
        server = _server(mesh)
        payload = await _call(server, "get_integration_status")
        by_name = {item["harness"]: item for item in payload["harnesses"]}
        assert by_name["claude_code"]["status"] == "detected_not_integrated"
        assert by_name["codex"]["status"] == "not_detected"
        assert by_name["claude_code"]["detected"] is True
        assert by_name["claude_code"]["registered"] is False
    finally:
        mesh.storage.close()


async def test_list_harnesses_hints_appear_and_disappear(tmp_path):
    mesh = make_mesh(tmp_path)
    _prepare(mesh, tmp_path, ("claude_code",))
    try:
        server = _server(mesh)
        payload = await _call(server, "list_harnesses")
        hints = payload["integration_hints"]
        assert len(hints) == 1
        assert hints[0].startswith("Claude Code is installed")
        assert 'plan_integration(harness="claude_code")' in hints[0]
        assert "before install_integration" in hints[0]

        mesh.config.integration.ignore.append("claude_code")
        payload = await _call(server, "list_harnesses")
        assert payload["integration_hints"] == []

        mesh.config.integration.ignore.clear()
        registration = mesh.home_dir / ".claude.json"
        registration.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "harness-mesh": {
                            "command": sys.executable,
                            "args": ["-m", "harness_mesh.mcp_server"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        payload = await _call(server, "list_harnesses")
        assert payload["integration_hints"] == []
    finally:
        mesh.storage.close()


async def test_stale_registration_produces_a_hint(tmp_path):
    mesh = make_mesh(tmp_path)
    _prepare(mesh, tmp_path, ("claude_code",))
    (mesh.home_dir / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "harness-mesh": {
                        "command": str(tmp_path / "removed" / "python"),
                        "args": ["-m", "harness_mesh.mcp_server"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    try:
        payload = await _call(_server(mesh), "list_harnesses")
        hints = payload["integration_hints"]
        assert len(hints) == 1
        assert "registered but its command no longer starts" in hints[0]
    finally:
        mesh.storage.close()


async def test_hints_are_empty_when_no_harness_is_detected(tmp_path):
    mesh = make_mesh(tmp_path)
    _prepare(mesh, tmp_path, ())
    try:
        payload = await _call(_server(mesh), "list_harnesses")
        assert payload["integration_hints"] == []
    finally:
        mesh.storage.close()


# ----------------------------------------------------------------------
# input validation
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("arguments", "needle"),
    [
        ({"harness": "unknown"}, "unknown or disabled harness"),
        (
            {"harness": "claude_code", "approve_level": "everything"},
            "unknown approval level",
        ),
        ({"harness": "../claude_code"}, "unknown or disabled harness"),
    ],
)
async def test_plan_integration_rejects_invalid_arguments(tmp_path, arguments, needle):
    mesh = make_mesh(tmp_path)
    _prepare(mesh, tmp_path, ("claude_code",))
    try:
        payload = await _call(_server(mesh), "plan_integration", arguments)
        assert "error" in payload
        assert needle in payload["error"]
    finally:
        mesh.storage.close()


async def test_install_integration_rejects_invalid_arguments(tmp_path):
    mesh = make_mesh(tmp_path)
    _prepare(mesh, tmp_path, ("claude_code",))
    try:
        payload = await _call(
            _server(mesh),
            "install_integration",
            {
                "harness": "unknown",
                "approve_level": "dispatch",
                "plan_id": "x" * 16,
            },
        )
        assert payload["installed"] is False
        assert "unknown or disabled harness" in payload["error"]
    finally:
        mesh.storage.close()
