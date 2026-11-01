"""Installer reliability, confirmation and doctor checks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from harness_mesh.cli import app as cli_app
from harness_mesh.config import default_config
from harness_mesh.installer import HarnessInstaller
from tests.helpers import init_git_repo


def _fake_binary(directory: Path, name: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _installer(tmp_path: Path, runner=None, *, home: Path | None = None, dry_run=False):
    home = home or tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir(exist_ok=True)
    workspace.mkdir(exist_ok=True)
    desktop = home / ".dsh" / "profiles" / "desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    (desktop / "cordis.patch.yml").write_text("# comment\n[]\n", encoding="utf-8")
    config = default_config()
    for harness in config.harnesses:
        config.harnesses[harness].binary = _fake_binary(tmp_path / "bin", harness)
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
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            which=lambda name: None,
            runner=runner or fake_runner,
            config_path=tmp_path / "missing.yaml",
            dry_run=dry_run,
        ),
        home,
        workspace,
        calls,
    )


def test_default_runner_sets_stdin_devnull(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    code, _, _ = HarnessInstaller._default_runner(["true"], Path.cwd(), {})
    assert code == 0
    assert captured["stdin"] is subprocess.DEVNULL


def test_failed_add_restores_original_registration(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "harness-mesh": {
                        "type": "stdio",
                        "command": "/original/command",
                        "args": ["--old"],
                        "env": {"OLD": "1"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def runner(argv, cwd, env):
        calls.append(list(argv))
        if "add-json" in argv:
            if any("original/command" in item for item in argv):
                return 0, "restored", ""
            return 1, "", "add failed"
        return 0, "", ""

    installer, _, _, _ = _installer(tmp_path, runner=runner, home=home)
    result = installer.install(["claude_code"])[0]
    assert result.action == "failed"
    assert "original registration restored" in result.detail
    restore = next(call for call in calls if "add-json" in call and any("/original/command" in item for item in call))
    payload = json.loads(restore[-1])
    assert payload["command"] == "/original/command"
    assert payload["args"] == ["--old"]
    remove = next(call for call in calls if "remove" in call)
    assert "-s" in remove and "user" in remove


def test_dsh_patch_rolls_back_when_validation_fails(tmp_path, monkeypatch):
    installer, home, _, _ = _installer(tmp_path)
    patch = home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
    original = patch.read_text(encoding="utf-8")

    import harness_mesh.installer as installer_module

    def fail_validation(content):
        raise ValueError("invalid patch")

    monkeypatch.setattr(installer_module, "_validate_dsh_patch", fail_validation)
    result = installer.install(["deepseek"])[0]
    assert result.action == "failed"
    assert patch.read_text(encoding="utf-8") == original
    assert patch.with_suffix(".yml.harness-mesh.bak").exists()


def test_cli_install_refuses_non_interactive_without_yes(tmp_path):
    workspace = tmp_path / "workspace"
    init_git_repo(workspace, {"README.md": "cli\n"})
    config = Path(__file__).resolve().parents[1] / "config" / "harnesses.example.yaml"
    runner = CliRunner()

    refused = runner.invoke(
        cli_app,
        ["--workspace", str(workspace), "--config", str(config), "install"],
        env={"HOME": str(tmp_path / "home"), "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1"},
    )
    assert refused.exit_code == 1
    assert "Refusing" in refused.output
    assert not (workspace / ".harness-mesh").exists()

    dry = runner.invoke(
        cli_app,
        ["--workspace", str(workspace), "--config", str(config), "install", "--yes", "--dry-run"],
        env={"HOME": str(tmp_path / "home"), "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1"},
    )
    assert dry.exit_code == 0, dry.output
    assert "Planned changes:" in dry.output


def test_doctor_reports_missing_command_and_healthy_command(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"harness-mesh": {"command": "/missing/command", "args": []}}}),
        encoding="utf-8",
    )
    installer, _, _, calls = _installer(tmp_path, home=home)
    missing = installer.doctor(["claude_code"])[0]
    assert missing.status == "failed"
    assert "Reinstall with `hmesh install --yes`" in missing.detail

    (home / ".claude.json").write_text(
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
    healthy = installer.doctor(["claude_code"])[0]
    assert healthy.status == "ok"
    assert calls and calls[-1][-1] == "--help"
