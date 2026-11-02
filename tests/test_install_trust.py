"""Installer trust boundary, doctor command validation and DSH atomic writes.

Install trust boundary:
* D5 -- ``hmesh install`` refuses worker/read-only callers and never embeds a
  workspace checkout path into the launched MCP server configuration.
* D6 -- the doctor executes only the known
  ``<current python> -m harness_mesh.mcp_server`` entry point.
* D7 -- DSH backups and uninstall use atomic writes and the backup is private.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from typer.testing import CliRunner

import harness_mesh.installer as installer_module
from harness_mesh.cli import app as cli_app
from harness_mesh.config import default_config
from harness_mesh.installer import DSH_BLOCK_START, HarnessInstaller
from tests.helpers import init_git_repo

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "harnesses.example.yaml"


def _fake_binary(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _installer(
    tmp_path: Path,
    *,
    home: Path | None = None,
    workspace: Path | None = None,
    config_path: Path | None = None,
    runner=None,
    dry_run: bool = False,
):
    home = home or tmp_path / "home"
    workspace = workspace or tmp_path / "workspace"
    home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    config = default_config()
    binaries = {
        "claude_code": _fake_binary(tmp_path / "bin", "claude"),
        "codex": _fake_binary(tmp_path / "bin", "codex"),
        "deepseek": _fake_binary(tmp_path / "bin", "dsh"),
        "antigravity": _fake_binary(tmp_path / "bin", "agy"),
    }
    for harness, binary in binaries.items():
        config.harnesses[harness].binary = str(binary)
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
            config_path=config_path,
            dry_run=dry_run,
        ),
        calls,
    )


# ----------------------------------------------------------------------
# D5: MCP environment never points at a workspace config
# ----------------------------------------------------------------------


def test_mcp_env_omits_a_workspace_config_path(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_config = workspace / "config" / "harnesses.yaml"
    workspace_config.parent.mkdir(parents=True)
    workspace_config.write_text("market: {}\n", encoding="utf-8")
    installer, _ = _installer(tmp_path, workspace=workspace, config_path=workspace_config)

    env = installer._mcp_env()
    assert env == {"HARNESS_MESH_WORKSPACE": str(workspace.resolve())}
    assert "HARNESS_MESH_CONFIG" not in env
    command_env = installer._command_env()
    assert "HARNESS_MESH_CONFIG" not in command_env


def test_mcp_env_embeds_a_user_level_config_inside_home(tmp_path):
    home = tmp_path / "home"
    user_config = home / ".config" / "harness-mesh" / "config.yaml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text("market: {}\n", encoding="utf-8")
    installer, _ = _installer(tmp_path, home=home, config_path=user_config)

    env = installer._mcp_env()
    assert env["HARNESS_MESH_CONFIG"] == str(user_config.resolve())
    assert env["HARNESS_MESH_WORKSPACE"] == str((tmp_path / "workspace").resolve())


def test_installer_does_not_pick_up_a_workspace_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_config = workspace / "config" / "harnesses.yaml"
    workspace_config.parent.mkdir(parents=True)
    workspace_config.write_text("harnesses: {}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("HARNESS_MESH_CONFIG", raising=False)

    installer, _ = _installer(tmp_path, home=home, workspace=workspace, config_path=None)
    assert installer.config_path is None


def _invoke_install(tmp_path: Path, *args: str, extra_env: dict[str, str] | None = None):
    workspace = tmp_path / "workspace"
    if not workspace.exists():
        init_git_repo(workspace, {"README.md": "install trust\n"})
    bin_dir = tmp_path / "bin"
    for name in ("claude", "codex", "agy"):
        _fake_binary(bin_dir, name)
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1",
        "HARNESS_MESH_OFFLINE": "1",
    }
    if extra_env:
        env.update(extra_env)
    return CliRunner().invoke(
        cli_app,
        [
            "--workspace",
            str(workspace),
            "--config",
            str(EXAMPLE_CONFIG),
            "install",
            "--harness",
            "claude_code",
            "--dry-run",
            *args,
        ],
        env=env,
    )


def test_cli_install_refuses_a_worker_context(tmp_path):
    result = _invoke_install(tmp_path, extra_env={"HARNESS_MESH_TASK_ID": "task_worker01"})
    assert result.exit_code == 1, result.output
    assert "Workers cannot change harness integrations" in result.output
    assert "task_worker01" not in result.output
    assert not (tmp_path / "home" / ".claude" / "settings.json").exists()


def test_cli_install_refuses_a_worktree_cwd(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    init_git_repo(workspace, {"README.md": "install trust\n"})
    worktree = workspace / ".harness-mesh" / "worktrees" / "task_worker01"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    result = _invoke_install(tmp_path)
    assert result.exit_code == 1, result.output
    assert "Workers cannot change harness integrations" in result.output
    assert not (tmp_path / "home" / ".claude" / "settings.json").exists()


def test_cli_install_refuses_read_only_mode(tmp_path):
    result = _invoke_install(tmp_path, extra_env={"HARNESS_MESH_READ_ONLY": "1"})
    assert result.exit_code == 1, result.output
    assert "read-only" in result.output
    assert not (tmp_path / "home" / ".claude" / "settings.json").exists()


def test_cli_install_still_works_for_a_controller(tmp_path):
    result = _invoke_install(tmp_path)
    assert result.exit_code == 0, result.output
    assert "Cannot install" not in result.output


# ----------------------------------------------------------------------
# D4: ``hmesh context set`` records the real caller provenance
# ----------------------------------------------------------------------


def _invoke_context_set(tmp_path: Path, *, extra_env: dict[str, str] | None = None):
    workspace = tmp_path / "workspace"
    if not workspace.exists():
        init_git_repo(workspace, {"README.md": "context source\n"})
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1",
        "HARNESS_MESH_OFFLINE": "1",
    }
    if extra_env:
        env.update(extra_env)
    return CliRunner().invoke(
        cli_app,
        ["--workspace", str(workspace), "context", "set", "wsd.source", "a fact"],
        env=env,
    )


def _stored_context_sources(workspace: Path) -> list[str]:
    from harness_mesh.storage import StateStore

    store = StateStore(workspace / ".harness-mesh" / "state.db")
    try:
        return [entry["source"] for entry in store.list_project_context()]
    finally:
        store.close()


def test_cli_context_set_from_a_worker_records_worker_provenance(tmp_path):
    result = _invoke_context_set(tmp_path, extra_env={"HARNESS_MESH_TASK_ID": "task_worker01"})
    assert result.exit_code == 0, result.output
    assert _stored_context_sources(tmp_path / "workspace") == ["worker"]


def test_cli_context_set_from_a_controller_records_controller_provenance(tmp_path):
    result = _invoke_context_set(tmp_path)
    assert result.exit_code == 0, result.output
    assert _stored_context_sources(tmp_path / "workspace") == ["controller"]


# ----------------------------------------------------------------------
# D6: doctor executes only the known mesh entry point
# ----------------------------------------------------------------------


def _write_claude_registration(home: Path, command: str, args: list[str]) -> None:
    (home / ".claude.json").parent.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"harness-mesh": {"command": command, "args": args}}}),
        encoding="utf-8",
    )


def test_doctor_refuses_a_foreign_registered_command(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "should-not-exist"
    _write_claude_registration(home, "/bin/sh", ["-c", f"touch {marker}"])
    installer, calls = _installer(tmp_path, home=home)

    result = installer.doctor(["claude_code"])[0]
    assert result.status == "failed"
    assert "refusing to execute" in result.detail
    assert calls == []
    assert not marker.exists()


def test_doctor_refuses_foreign_arguments_for_the_python_interpreter(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "should-not-exist"
    _write_claude_registration(home, sys.executable, ["-c", f"touch {marker}"])
    installer, calls = _installer(tmp_path, home=home)

    result = installer.doctor(["claude_code"])[0]
    assert result.status == "failed"
    assert "refusing to execute" in result.detail
    assert calls == []
    assert not marker.exists()


def test_doctor_runs_the_known_mesh_entry_point(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _write_claude_registration(home, sys.executable, ["-m", "harness_mesh.mcp_server"])
    installer, calls = _installer(tmp_path, home=home)

    result = installer.doctor(["claude_code"])[0]
    assert result.status == "ok"
    assert calls and calls[-1][-1] == "--help"
    assert calls[-1][0] == sys.executable


# ----------------------------------------------------------------------
# D7: DSH backup/uninstall are atomic and private
# ----------------------------------------------------------------------


def test_dsh_install_backup_is_private_and_atomic(tmp_path):
    home = tmp_path / "home"
    profile = home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
    profile.parent.mkdir(parents=True)
    original = "# user cordis patch\n[]\n"
    profile.write_text(original, encoding="utf-8")

    installer, _ = _installer(tmp_path, home=home)
    result = installer._install_dsh()

    assert result.action == "installed", result.detail
    backup = profile.with_name("cordis.patch.yml.harness-mesh.bak")
    assert backup.read_text(encoding="utf-8") == original
    assert backup.stat().st_mode & 0o777 == 0o600
    text = profile.read_text(encoding="utf-8")
    assert DSH_BLOCK_START in text


def test_dsh_install_tightens_a_legacy_backup_mode(tmp_path):
    home = tmp_path / "home"
    profile = home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
    profile.parent.mkdir(parents=True)
    profile.write_text("# user cordis patch\n[]\n", encoding="utf-8")
    backup = profile.with_name("cordis.patch.yml.harness-mesh.bak")
    backup.write_text("legacy backup\n", encoding="utf-8")
    backup.chmod(0o644)

    installer, _ = _installer(tmp_path, home=home)
    installer._install_dsh()

    assert backup.read_text(encoding="utf-8") == "legacy backup\n"
    assert backup.stat().st_mode & 0o777 == 0o600


def test_dsh_uninstall_uses_the_atomic_writer(tmp_path, monkeypatch):
    home = tmp_path / "home"
    profile = home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
    profile.parent.mkdir(parents=True)
    profile.write_text("# user cordis patch\n[]\n", encoding="utf-8")
    installer, _ = _installer(tmp_path, home=home)
    assert installer._install_dsh().action == "installed"

    writes: list[tuple[Path, str]] = []
    real_write = installer_module._atomic_write_text

    def spy(path, content):
        writes.append((Path(path), content))
        real_write(path, content)

    monkeypatch.setattr(installer_module, "_atomic_write_text", spy)
    result = installer._uninstall_dsh()

    assert result.action == "uninstalled"
    assert any(path == profile for path, _ in writes)
    text = profile.read_text(encoding="utf-8")
    assert DSH_BLOCK_START not in text
    assert installer_module.yaml.safe_load(text) == []
