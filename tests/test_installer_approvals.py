"""Approval writes for Claude Code, Codex and Antigravity configuration files."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

from harness_mesh.config import default_config
from harness_mesh.installer import (
    CODEX_APPROVAL_BLOCK_END,
    CODEX_APPROVAL_BLOCK_START,
    HarnessInstaller,
)


def _fake_binary(directory: Path, name: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _installer(
    tmp_path: Path,
    *,
    level: str = "dispatch",
    dry_run: bool = False,
    runner=None,
    home: Path | None = None,
):
    home = home or tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir(exist_ok=True)
    workspace.mkdir(exist_ok=True)
    config = default_config()
    binaries = {
        "claude_code": _fake_binary(tmp_path / "bin", "claude"),
        "codex": _fake_binary(tmp_path / "bin", "codex"),
        "deepseek": _fake_binary(tmp_path / "bin", "dsh"),
        "antigravity": _fake_binary(tmp_path / "bin", "agy"),
        "zcode": _fake_binary(tmp_path / "bin", "zcode"),
    }
    for harness, binary in binaries.items():
        config.harnesses[harness].binary = binary
        config.harnesses[harness].binary_paths = []
    calls: list[list[str]] = []

    def fake_runner(argv, cwd, env):
        calls.append(list(argv))
        return 0, "", ""

    return (
        HarnessInstaller(
            config,
            workspace,
            home_dir=home,
            env={"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            which=lambda name: None,
            runner=runner or fake_runner,
            config_path=tmp_path / "missing-config.yaml",
            dry_run=dry_run,
            approval_level=level,
        ),
        home,
        calls,
    )


def _claude_path(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _agy_path(home: Path) -> Path:
    return home / ".gemini" / "config" / "config.json"


def _codex_path(home: Path) -> Path:
    return home / ".codex" / "config.toml"


CLAUDE_PREFIX = "mcp__harness-mesh__"
AGY_PREFIX = "mcp(harness-mesh/"


def _claude_allow(home: Path) -> list[str]:
    return json.loads(_claude_path(home).read_text(encoding="utf-8"))["permissions"]["allow"]


def _agy_allow(home: Path) -> list[str]:
    payload = json.loads(_agy_path(home).read_text(encoding="utf-8"))
    return payload["userSettings"]["globalPermissionGrants"]["allow"]


def _managed(entries: list[str], prefix: str) -> list[str]:
    return [entry for entry in entries if entry.startswith(prefix)]


# ----------------------------------------------------------------------
# Claude Code
# ----------------------------------------------------------------------
def test_claude_writes_approvals_and_preserves_unrelated_settings(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    settings = {
        "model": "sonnet",
        "env": {"KEEP_ME": "1"},
        "permissions": {"allow": ["Bash(ls)", "mcp__other__tool"], "deny": ["Read(.env)"]},
    }
    _claude_path(home).parent.mkdir(parents=True)
    _claude_path(home).write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    _claude_path(home).chmod(0o640)
    installer, home, _ = _installer(tmp_path, home=home)

    result = installer.install(["claude_code"])[0]
    assert result.action == "installed"
    payload = json.loads(_claude_path(home).read_text(encoding="utf-8"))
    assert payload["model"] == "sonnet"
    assert payload["env"] == {"KEEP_ME": "1"}
    assert payload["permissions"]["deny"] == ["Read(.env)"]
    allow = payload["permissions"]["allow"]
    assert "Bash(ls)" in allow
    assert "mcp__other__tool" in allow
    assert len(_managed(allow, CLAUDE_PREFIX)) == 14
    assert "mcp__harness-mesh__dispatch_task" in allow
    assert "mcp__harness-mesh__apply_task_patch" not in allow
    assert _claude_path(home).stat().st_mode & 0o777 == 0o640
    backup = _claude_path(home).with_name("settings.json.harness-mesh.bak")
    assert backup.read_text(encoding="utf-8") == json.dumps(settings, indent=2) + "\n"
    assert result.approvals["status"] == "applied"
    assert len(result.approvals["tools"]) == 14


def test_claude_reinstall_is_idempotent_and_keeps_first_backup(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    settings = {"permissions": {"allow": ["Bash(ls)"]}}
    _claude_path(home).parent.mkdir(parents=True)
    _claude_path(home).write_text(json.dumps(settings) + "\n", encoding="utf-8")
    installer, home, _ = _installer(tmp_path, home=home)

    installer.install(["claude_code"])
    first = _claude_path(home).read_text(encoding="utf-8")
    installer.install(["claude_code"])
    assert _claude_path(home).read_text(encoding="utf-8") == first
    assert len(_managed(_claude_allow(home), CLAUDE_PREFIX)) == 14
    backup = _claude_path(home).with_name("settings.json.harness-mesh.bak")
    assert backup.read_text(encoding="utf-8") == json.dumps(settings) + "\n"


def test_claude_approval_levels_raise_lower_and_clear(tmp_path):
    installer, home, _ = _installer(tmp_path, level="read")
    installer.install(["claude_code"])
    assert len(_managed(_claude_allow(home), CLAUDE_PREFIX)) == 11
    assert "mcp__harness-mesh__dispatch_task" not in _claude_allow(home)

    installer.approval_level = "all"
    installer.install(["claude_code"])
    assert len(_managed(_claude_allow(home), CLAUDE_PREFIX)) == 18
    assert "mcp__harness-mesh__apply_task_patch" in _claude_allow(home)

    installer.approval_level = "read"
    installer.install(["claude_code"])
    allow = _claude_allow(home)
    assert len(_managed(allow, CLAUDE_PREFIX)) == 11
    assert "mcp__harness-mesh__apply_task_patch" not in allow
    assert "mcp__harness-mesh__dispatch_task" not in allow


def test_claude_none_and_uninstall_remove_only_managed_rules(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _claude_path(home).parent.mkdir(parents=True)
    _claude_path(home).write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls)", "mcp__other__tool"]}}) + "\n",
        encoding="utf-8",
    )
    installer, home, _ = _installer(tmp_path, home=home)
    installer.install(["claude_code"])

    installer.approval_level = "none"
    installer.install(["claude_code"])
    allow = _claude_allow(home)
    assert _managed(allow, CLAUDE_PREFIX) == []
    assert allow == ["Bash(ls)", "mcp__other__tool"]

    installer.approval_level = "all"
    installer.install(["claude_code"])
    installer.install(["claude_code"], uninstall=True)
    allow = _claude_allow(home)
    assert _managed(allow, CLAUDE_PREFIX) == []
    assert allow == ["Bash(ls)", "mcp__other__tool"]


def test_claude_creates_missing_settings_file(tmp_path):
    installer, home, _ = _installer(tmp_path)
    result = installer.install(["claude_code"])[0]
    assert result.action == "installed"
    assert len(_managed(_claude_allow(home), CLAUDE_PREFIX)) == 14
    assert not _claude_path(home).with_name("settings.json.harness-mesh.bak").exists()


def test_claude_validation_failure_restores_previous_content(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _claude_path(home).parent.mkdir(parents=True)
    original = '{"permissions": {"allow": ["Bash(ls)"]}}\n'
    _claude_path(home).write_text(original, encoding="utf-8")
    installer, home, _ = _installer(tmp_path, home=home)

    import harness_mesh.installer as installer_module

    def fail_validation(text, tools):
        raise ValueError("claude validation failed")

    monkeypatch.setattr(installer_module, "_validate_claude_approvals", fail_validation)
    result = installer.install(["claude_code"])[0]
    assert result.action == "failed"
    assert "claude validation failed" in result.detail
    assert _claude_path(home).read_text(encoding="utf-8") == original


def test_claude_dry_run_lists_entries_without_writing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _claude_path(home).parent.mkdir(parents=True)
    original = '{"permissions": {"allow": ["Bash(ls)"]}}\n'
    _claude_path(home).write_text(original, encoding="utf-8")
    installer, home, _ = _installer(tmp_path, home=home, dry_run=True)

    result = installer.install(["claude_code"])[0]
    assert result.action == "would_install"
    assert result.approvals["status"] == "planned"
    assert len(result.approvals["entries"]) == 14
    assert str(_claude_path(home)) in result.approvals["files"]
    assert _claude_path(home).read_text(encoding="utf-8") == original
    assert not _claude_path(home).with_name("settings.json.harness-mesh.bak").exists()


# ----------------------------------------------------------------------
# Antigravity
# ----------------------------------------------------------------------
def test_antigravity_writes_approvals_and_preserves_other_keys(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    payload = {
        "theme": "dark",
        "userSettings": {
            "globalPermissionGrants": {
                "allow": ["write_file(/Users/me/AGENTS.md)", "mcp(other/tool)"],
                "deny": ["shell(rm)"],
            },
            "telemetry": False,
        },
    }
    _agy_path(home).parent.mkdir(parents=True)
    _agy_path(home).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _agy_path(home).chmod(0o600)
    installer, home, _ = _installer(tmp_path, home=home)

    result = installer.install(["antigravity"])[0]
    assert result.action == "installed"
    written = json.loads(_agy_path(home).read_text(encoding="utf-8"))
    assert written["theme"] == "dark"
    assert written["userSettings"]["telemetry"] is False
    assert written["userSettings"]["globalPermissionGrants"]["deny"] == ["shell(rm)"]
    allow = written["userSettings"]["globalPermissionGrants"]["allow"]
    assert "write_file(/Users/me/AGENTS.md)" in allow
    assert "mcp(other/tool)" in allow
    assert len(_managed(allow, AGY_PREFIX)) == 14
    assert "mcp(harness-mesh/dispatch_task)" in allow
    assert "mcp(harness-mesh/apply_task_patch)" not in allow
    assert _agy_path(home).stat().st_mode & 0o777 == 0o600
    backup = _agy_path(home).with_name("config.json.harness-mesh.bak")
    assert json.loads(backup.read_text(encoding="utf-8")) == payload


def test_antigravity_creates_missing_containers(tmp_path):
    installer, home, _ = _installer(tmp_path, level="read")
    result = installer.install(["antigravity"])[0]
    assert result.action == "installed"
    allow = _agy_allow(home)
    assert len(_managed(allow, AGY_PREFIX)) == 11


def test_antigravity_reinstall_is_idempotent(tmp_path):
    installer, home, _ = _installer(tmp_path)
    installer.install(["antigravity"])
    first = _agy_path(home).read_text(encoding="utf-8")
    installer.install(["antigravity"])
    assert _agy_path(home).read_text(encoding="utf-8") == first
    assert len(_managed(_agy_allow(home), AGY_PREFIX)) == 14


def test_antigravity_levels_none_and_uninstall_clear_only_managed(tmp_path):
    installer, home, _ = _installer(tmp_path, level="all")
    installer.install(["antigravity"])
    assert len(_managed(_agy_allow(home), AGY_PREFIX)) == 18

    installer.approval_level = "read"
    installer.install(["antigravity"])
    assert len(_managed(_agy_allow(home), AGY_PREFIX)) == 11

    installer.approval_level = "none"
    installer.install(["antigravity"])
    assert _managed(_agy_allow(home), AGY_PREFIX) == []

    installer.approval_level = "dispatch"
    installer.install(["antigravity"])
    installer.install(["antigravity"], uninstall=True)
    assert _managed(_agy_allow(home), AGY_PREFIX) == []


def test_antigravity_validation_failure_restores_previous_content(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _agy_path(home).parent.mkdir(parents=True)
    original = json.dumps({"userSettings": {"globalPermissionGrants": {"allow": ["keep"]}}})
    _agy_path(home).write_text(original, encoding="utf-8")
    installer, home, _ = _installer(tmp_path, home=home)

    import harness_mesh.installer as installer_module

    def fail_validation(text, tools):
        raise ValueError("antigravity validation failed")

    monkeypatch.setattr(installer_module, "_validate_antigravity_approvals", fail_validation)
    result = installer.install(["antigravity"])[0]
    assert result.action == "failed"
    assert _agy_path(home).read_text(encoding="utf-8") == original


def test_antigravity_dry_run_does_not_write(tmp_path):
    installer, home, _ = _installer(tmp_path, dry_run=True)
    result = installer.install(["antigravity"])[0]
    assert result.action == "would_install"
    assert len(result.approvals["entries"]) == 14
    assert not _agy_path(home).exists()


# ----------------------------------------------------------------------
# Codex
# ----------------------------------------------------------------------
def test_codex_writes_managed_block_and_preserves_unrelated_toml(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    original = (
        'model = "gpt-5"\n\n'
        "[profiles.fast]\n"
        'model = "gpt-5-mini"\n\n'
        "[mcp_servers.harness-mesh]\n"
        'command = "/old/python"\n'
        'args = ["-m", "harness_mesh.mcp_server"]\n'
    )
    _codex_path(home).parent.mkdir(parents=True)
    _codex_path(home).write_text(original, encoding="utf-8")
    _codex_path(home).chmod(0o640)
    installer, home, _ = _installer(tmp_path, home=home)

    result = installer.install(["codex"])[0]
    assert result.action == "installed"
    text = _codex_path(home).read_text(encoding="utf-8")
    assert text.count(CODEX_APPROVAL_BLOCK_START) == 1
    assert text.count(CODEX_APPROVAL_BLOCK_END) == 1
    parsed = tomllib.loads(text)
    assert parsed["model"] == "gpt-5"
    assert parsed["profiles"]["fast"]["model"] == "gpt-5-mini"
    assert parsed["mcp_servers"]["harness-mesh"]["command"] == "/old/python"
    tools = parsed["mcp_servers"]["harness-mesh"]["tools"]
    assert len(tools) == 14
    assert tools["dispatch_task"]["approval_mode"] == "approve"
    assert "apply_task_patch" not in tools
    assert _codex_path(home).stat().st_mode & 0o777 == 0o640
    backup = _codex_path(home).with_name("config.toml.harness-mesh.bak")
    assert backup.read_text(encoding="utf-8") == original


def test_codex_reinstall_is_idempotent(tmp_path):
    installer, home, _ = _installer(tmp_path)
    installer.install(["codex"])
    first = _codex_path(home).read_text(encoding="utf-8")
    installer.install(["codex"])
    assert _codex_path(home).read_text(encoding="utf-8") == first
    assert first.count(CODEX_APPROVAL_BLOCK_START) == 1
    parsed = tomllib.loads(first)
    assert len(parsed["mcp_servers"]["harness-mesh"]["tools"]) == 14


def test_codex_removes_block_before_cli_calls_and_appends_after(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _codex_path(home).parent.mkdir(parents=True)
    _codex_path(home).write_text(
        "[mcp_servers.harness-mesh]\ncommand = \"/old/python\"\nargs = []\n",
        encoding="utf-8",
    )
    seen: list[tuple[str, str]] = []

    def runner(argv, cwd, env):
        if "mcp" in argv and "remove" in argv:
            seen.append(("remove", _codex_path(home).read_text(encoding="utf-8")))
            _codex_path(home).write_text("", encoding="utf-8")
        elif "mcp" in argv and "add" in argv:
            seen.append(("add", _codex_path(home).read_text(encoding="utf-8")))
            _codex_path(home).write_text(
                '[mcp_servers.harness-mesh]\ncommand = "/new/python"\n',
                encoding="utf-8",
            )
        return 0, "", ""

    installer, home, _ = _installer(tmp_path, runner=runner, home=home)
    result = installer.install(["codex"])[0]
    assert result.action == "installed"
    assert [name for name, _ in seen] == ["remove", "add"]
    assert all(CODEX_APPROVAL_BLOCK_START not in text for _, text in seen)
    final = _codex_path(home).read_text(encoding="utf-8")
    assert final.index(CODEX_APPROVAL_BLOCK_START) > final.index("[mcp_servers.harness-mesh]")
    assert tomllib.loads(final)["mcp_servers"]["harness-mesh"]["command"] == "/new/python"


def test_codex_levels_none_and_uninstall_clear_managed_block(tmp_path):
    installer, home, _ = _installer(tmp_path, level="all")
    installer.install(["codex"])
    parsed = tomllib.loads(_codex_path(home).read_text(encoding="utf-8"))
    assert len(parsed["mcp_servers"]["harness-mesh"]["tools"]) == 18

    installer.approval_level = "read"
    installer.install(["codex"])
    parsed = tomllib.loads(_codex_path(home).read_text(encoding="utf-8"))
    assert len(parsed["mcp_servers"]["harness-mesh"]["tools"]) == 11

    installer.approval_level = "none"
    installer.install(["codex"])
    text = _codex_path(home).read_text(encoding="utf-8")
    assert CODEX_APPROVAL_BLOCK_START not in text
    assert CODEX_APPROVAL_BLOCK_END not in text

    installer.approval_level = "dispatch"
    installer.install(["codex"])
    installer.install(["codex"], uninstall=True)
    text = _codex_path(home).read_text(encoding="utf-8")
    assert CODEX_APPROVAL_BLOCK_START not in text


def test_codex_validation_failure_restores_previous_content(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _codex_path(home).parent.mkdir(parents=True)
    original = 'model = "gpt-5"\n\n[mcp_servers.harness-mesh]\ncommand = "/old/python"\nargs = []\n'
    _codex_path(home).write_text(original, encoding="utf-8")
    installer, home, _ = _installer(tmp_path, home=home)

    import harness_mesh.installer as installer_module

    def fail_validation(text, tools):
        raise ValueError("codex validation failed")

    monkeypatch.setattr(installer_module, "_validate_codex_approvals", fail_validation)
    result = installer.install(["codex"])[0]
    assert result.action == "failed"
    assert "codex validation failed" in result.detail
    assert _codex_path(home).read_text(encoding="utf-8") == original


def test_codex_dry_run_does_not_write(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _codex_path(home).parent.mkdir(parents=True)
    original = 'model = "gpt-5"\n'
    _codex_path(home).write_text(original, encoding="utf-8")
    installer, home, _ = _installer(tmp_path, home=home, dry_run=True)

    result = installer.install(["codex"])[0]
    assert result.action == "would_install"
    assert result.approvals["status"] == "planned"
    assert len(result.approvals["entries"]) == 14
    assert _codex_path(home).read_text(encoding="utf-8") == original
    assert not _codex_path(home).with_name("config.toml.harness-mesh.bak").exists()


# ----------------------------------------------------------------------
# Harnesses without approval settings
# ----------------------------------------------------------------------
def test_zcode_and_deepseek_report_no_approval_settings_needed(tmp_path):
    installer, _, _ = _installer(tmp_path)
    zcode = installer.install(["zcode"])[0]
    deepseek = installer.install(["deepseek"])[0]
    assert zcode.action == "installed"
    assert "no approval settings needed" in zcode.detail
    assert zcode.approvals["status"] == "not_required"
    assert deepseek.action == "installed"
    assert "no approval settings needed" in deepseek.detail
    assert deepseek.approvals["status"] == "not_required"


def test_claude_malformed_settings_returns_failed_without_touching_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _claude_path(home).parent.mkdir(parents=True)
    original = '{"permissions": "not-an-object"}\n'
    _claude_path(home).write_text(original, encoding="utf-8")
    installer, home, _ = _installer(tmp_path, home=home)

    result = installer.install(["claude_code"])[0]
    assert result.action == "failed"
    assert "permissions must be an object" in result.detail
    assert _claude_path(home).read_text(encoding="utf-8") == original


def test_claude_unreadable_settings_returns_failed_without_touching_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _claude_path(home).parent.mkdir(parents=True)
    original = b"\xff\xfe\x00broken"
    _claude_path(home).write_bytes(original)
    installer, home, _ = _installer(tmp_path, home=home)

    result = installer.install(["claude_code"])[0]
    assert result.action == "failed"
    assert "cannot read" in result.detail
    assert _claude_path(home).read_bytes() == original


def test_codex_service_wide_approval_counts_as_full_coverage(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _codex_path(home).parent.mkdir(parents=True)
    _codex_path(home).write_text(
        '[mcp_servers.harness-mesh]\ncommand = "/old/python"\ndefault_tools_approval_mode = "approve"\n',
        encoding="utf-8",
    )
    installer, home, _ = _installer(tmp_path, home=home)

    report = installer.approval_report("codex")
    assert report["approved"] == 14
    assert report["missing"] == []
    assert len(report["tools"]) == 18
