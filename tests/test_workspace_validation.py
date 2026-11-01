"""Consistent workspace validation for runtime, install and doctor."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harness_mesh.cli import app as cli_app
from harness_mesh.security import ensure_service_workspace, is_git_workspace
from tests.helpers import init_git_repo


def test_auto_init_repo_allows_non_git_service_workspace(tmp_path):
    workspace = tmp_path / "plain"
    workspace.mkdir()
    assert ensure_service_workspace(workspace, auto_init_repo=True) == workspace.resolve()
    with pytest.raises(RuntimeError, match="requires a Git workspace"):
        ensure_service_workspace(workspace, auto_init_repo=False)


def test_is_git_workspace_reports_missing_git(monkeypatch, tmp_path):
    workspace = tmp_path / "plain"
    workspace.mkdir()

    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="git is not installed"):
        is_git_workspace(workspace)


def test_install_refuses_home_workspace_at_plan_time(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config = Path(__file__).resolve().parents[1] / "config" / "harnesses.example.yaml"
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["--workspace", str(home), "--config", str(config), "install", "--dry-run"],
        env={"HOME": str(home), "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1"},
    )
    assert result.exit_code == 1
    assert "Cannot install from this workspace" in result.output
    assert not (home / ".harness-mesh").exists()


def test_doctor_flags_registered_workspace_outside_git(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    invalid_workspace = tmp_path / "not-git"
    invalid_workspace.mkdir()
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "harness-mesh": {
                        "command": sys.executable,
                        "args": ["-m", "harness_mesh.mcp_server"],
                        "env": {"HARNESS_MESH_WORKSPACE": str(invalid_workspace)},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    init_git_repo(repo, {"README.md": "doctor\n"})
    config = Path(__file__).resolve().parents[1] / "config" / "harnesses.example.yaml"
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["--workspace", str(repo), "--config", str(config), "doctor", "--harness", "claude_code"],
        env={"HOME": str(home), "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1"},
    )
    assert result.exit_code == 1, result.output
    assert "registered workspace is invalid" in result.output
