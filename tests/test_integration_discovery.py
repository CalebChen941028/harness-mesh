"""Detection and reviewable plans for newly installed harnesses."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from harness_mesh.config import default_config
from harness_mesh.installer import HarnessInstaller

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


def _installer(
    tmp_path: Path,
    *,
    ignore: tuple[str, ...] = (),
    detect: tuple[str, ...] = tuple(BINARIES),
    allow_system_paths: bool = False,
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    config = default_config()
    for harness in BINARIES:
        cfg = config.harnesses[harness]
        cfg.binary = str(tmp_path / "missing" / BINARIES[harness])
        cfg.binary_paths = []
        if harness == "zcode":
            cfg.extra["macos_electron_bin"] = str(tmp_path / "missing" / "ZCode")
            cfg.extra["macos_cjs_entry"] = str(tmp_path / "missing" / "zcode.cjs")
    for harness in detect:
        config.harnesses[harness].binary = str(
            _fake_binary(tmp_path / "bin", BINARIES[harness])
        )
    config.integration.ignore = list(ignore)
    installer = HarnessInstaller(
        config,
        workspace,
        home_dir=home,
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        which=lambda name: None,
        runner=lambda argv, cwd, env: (0, "", ""),
        config_path=tmp_path / "missing-config.yaml",
        allow_system_paths=allow_system_paths,
    )
    return installer, home, workspace


def _register_claude(home: Path, command: str) -> None:
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "harness-mesh": {
                        "type": "stdio",
                        "command": command,
                        "args": ["-m", "harness_mesh.mcp_server"],
                        "env": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


# ----------------------------------------------------------------------
# five states
# ----------------------------------------------------------------------
def test_detected_but_unregistered_harness(tmp_path):
    installer, _, _ = _installer(tmp_path, detect=("claude_code",))
    statuses = {item.harness: item for item in installer.integration_status()}
    claude = statuses["claude_code"]
    assert claude.status == "detected_not_integrated"
    assert claude.detected is True
    assert claude.registered is False
    assert claude.ignored is False
    assert claude.binary_path
    assert "not registered" in claude.detail
    assert "codex" in statuses
    assert statuses["codex"].status == "not_detected"


def test_integrated_harness(tmp_path):
    installer, home, _ = _installer(tmp_path, detect=("claude_code",))
    _register_claude(home, sys.executable)
    status = {item.harness: item for item in installer.integration_status()}["claude_code"]
    assert status.status == "integrated"
    assert status.registered is True
    assert status.detail == "MCP/plugin registration found"
    assert str(home / ".claude.json") in status.registration_files


def test_stale_registration_is_reported(tmp_path):
    installer, home, _ = _installer(tmp_path, detect=("claude_code",))
    _register_claude(home, str(tmp_path / "removed" / "python"))
    status = {item.harness: item for item in installer.integration_status()}["claude_code"]
    assert status.status == "stale"
    assert status.registered is True
    assert "missing or not executable" in status.detail


def test_registered_command_must_be_executable(tmp_path):
    installer, home, _ = _installer(tmp_path, detect=("claude_code",))
    not_executable = tmp_path / "python"
    not_executable.write_text("", encoding="utf-8")
    _register_claude(home, str(not_executable))
    status = {item.harness: item for item in installer.integration_status()}["claude_code"]
    assert status.status == "stale"


def test_absent_harness_is_not_detected(tmp_path):
    installer, _, _ = _installer(tmp_path, detect=())
    statuses = {item.harness: item for item in installer.integration_status()}
    assert statuses["claude_code"].status == "not_detected"
    assert statuses["zcode"].status == "not_detected"
    assert statuses["claude_code"].detected is False


def test_ignored_harness_is_reported_as_ignored(tmp_path):
    installer, _, _ = _installer(
        tmp_path, ignore=("claude_code",), detect=("claude_code",)
    )
    status = {item.harness: item for item in installer.integration_status()}["claude_code"]
    assert status.status == "ignored"
    assert status.ignored is True
    assert status.detected is True


def test_zcode_detection_requires_both_app_paths(tmp_path):
    installer, _, _ = _installer(tmp_path, detect=())
    electron = tmp_path / "ZCode"
    electron.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cjs = tmp_path / "zcode.cjs"
    cjs.write_text("// entry\n", encoding="utf-8")
    extra = installer.config.harnesses["zcode"].extra
    extra["macos_electron_bin"] = str(electron)
    extra["macos_cjs_entry"] = str(cjs)

    status = {item.harness: item for item in installer.integration_status()}["zcode"]
    assert status.status == "detected_not_integrated"

    cjs.unlink()
    status = {item.harness: item for item in installer.integration_status()}["zcode"]
    assert status.status == "not_detected"


def test_disabled_harness_is_left_out(tmp_path):
    installer, _, _ = _installer(tmp_path, detect=("claude_code",))
    installer.config.harnesses["claude_code"].enabled = False
    assert "claude_code" not in {
        item.harness for item in installer.integration_status()
    }


# ----------------------------------------------------------------------
# plan identifiers
# ----------------------------------------------------------------------
def test_plan_is_reviewable_and_has_a_stable_identifier(tmp_path):
    installer, _, _ = _installer(tmp_path, detect=("claude_code",))
    first = installer.integration_plan("claude_code", "dispatch")
    second = installer.integration_plan("claude_code", "dispatch")

    assert first.plan_id == second.plan_id
    assert len(first.plan_id) == 16
    assert first.plan_id == first.plan_id.lower()
    assert first.action == "would_install"
    assert first.approve_level == "dispatch"
    assert first.commands
    assert any("claude" in command[0] for command in first.commands)
    assert any(entry.endswith("dispatch_task") for entry in first.entries)
    assert any(str(path).endswith("settings.json") for path in first.files)
    payload = first.as_dict()
    assert payload["plan_id"] == first.plan_id
    assert payload["harness"] == "claude_code"
    assert payload["restart_required"] is True


def test_plan_identifier_changes_with_the_approval_level(tmp_path):
    installer, _, _ = _installer(tmp_path, detect=("claude_code",))
    dispatch = installer.integration_plan("claude_code", "dispatch")
    everything = installer.integration_plan("claude_code", "all")
    assert dispatch.plan_id != everything.plan_id
    assert len(everything.entries) > len(dispatch.entries)


def test_plan_identifier_changes_when_the_registration_file_changes(tmp_path):
    installer, home, _ = _installer(tmp_path, detect=("claude_code",))
    before = installer.integration_plan("claude_code", "dispatch")
    _register_claude(home, sys.executable)
    after = installer.integration_plan("claude_code", "dispatch")
    assert before.plan_id != after.plan_id


def test_plan_identifier_changes_when_the_target_file_changes(tmp_path):
    installer, home, _ = _installer(tmp_path, detect=("claude_code",))
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}), encoding="utf-8")
    before = installer.integration_plan("claude_code", "dispatch")
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(pwd)"]}}), encoding="utf-8")
    after = installer.integration_plan("claude_code", "dispatch")
    assert before.plan_id != after.plan_id


def test_plan_writes_no_files(tmp_path):
    installer, _, workspace = _installer(tmp_path, detect=("claude_code", "zcode"))
    before = {path for path in tmp_path.rglob("*") if path.is_file()}
    installer.integration_plan("claude_code", "dispatch")
    installer.integration_plan("zcode", "dispatch")
    after = {path for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before
    assert not (workspace / ".harness-mesh" / "bin" / "zcode").exists()


def test_zcode_plan_does_not_create_the_macos_shim(tmp_path):
    installer, _, workspace = _installer(
        tmp_path, detect=(), allow_system_paths=True
    )
    electron = tmp_path / "ZCode"
    electron.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cjs = tmp_path / "zcode.cjs"
    cjs.write_text("// entry\n", encoding="utf-8")
    extra = installer.config.harnesses["zcode"].extra
    extra["macos_electron_bin"] = str(electron)
    extra["macos_cjs_entry"] = str(cjs)

    plan = installer.integration_plan("zcode", "dispatch")
    assert plan.action == "would_install"
    assert plan.commands
    assert not (workspace / ".harness-mesh" / "bin" / "zcode").exists()


def test_not_detected_plan_is_not_installable(tmp_path):
    installer, _, _ = _installer(tmp_path, detect=())
    plan = installer.integration_plan("claude_code", "dispatch")
    assert plan.action == "skipped"
    assert plan.installable is False
    assert plan.detected is False


@pytest.mark.parametrize("harness", ["unknown", "", "../claude_code", "/tmp/claude"])
def test_plan_rejects_unknown_harness_names(tmp_path, harness):
    installer, _, _ = _installer(tmp_path, detect=("claude_code",))
    with pytest.raises(ValueError, match="unknown or disabled harness"):
        installer.integration_plan(harness, "dispatch")


def test_plan_rejects_a_disabled_harness(tmp_path):
    installer, _, _ = _installer(tmp_path, detect=("claude_code",))
    installer.config.harnesses["claude_code"].enabled = False
    with pytest.raises(ValueError, match="unknown or disabled harness"):
        installer.integration_plan("claude_code", "dispatch")


@pytest.mark.parametrize("level", ["write", "everything", "", "READ "])
def test_plan_rejects_unknown_approval_levels(tmp_path, level):
    installer, _, _ = _installer(tmp_path, detect=("claude_code",))
    with pytest.raises(ValueError, match="unknown approval level"):
        installer.integration_plan("claude_code", level)


def test_zcode_app_detection_respects_the_system_binary_switch(tmp_path):
    installer, _, _ = _installer(tmp_path, detect=(), allow_system_paths=True)
    electron = tmp_path / "ZCode"
    electron.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cjs = tmp_path / "zcode.cjs"
    cjs.write_text("// entry\n", encoding="utf-8")
    extra = installer.config.harnesses["zcode"].extra
    extra["macos_electron_bin"] = str(electron)
    extra["macos_cjs_entry"] = str(cjs)
    installer.env["HARNESS_MESH_DISABLE_SYSTEM_BINARIES"] = "1"
    status = {item.harness: item for item in installer.integration_status()}["zcode"]
    assert status.status == "not_detected"
