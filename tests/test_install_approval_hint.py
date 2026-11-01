"""``hmesh install`` explains the approval level that was actually applied."""

from __future__ import annotations

import os
import re
from pathlib import Path

from typer.testing import CliRunner

from harness_mesh.cli import app as cli_app
from tests.helpers import init_git_repo


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _invoke(tmp_path, *args):
    workspace = tmp_path / "workspace"
    if not workspace.exists():
        init_git_repo(workspace, {"README.md": "install hint\n"})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("claude", "codex", "agy"):
        path = bin_dir / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    config = Path(__file__).resolve().parents[1] / "config" / "harnesses.example.yaml"
    return CliRunner().invoke(
        cli_app,
        [
            "--workspace",
            str(workspace),
            "--config",
            str(config),
            "install",
            "--harness",
            "claude_code",
            *args,
        ],
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1",
        },
    )


def test_install_prints_the_approval_summary(tmp_path):
    result = _invoke(tmp_path, "--yes")
    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Approved 10 tools" in output
    assert "apply_task_patch" in output
    assert "--approve all" in output
    assert "default_tools_approval_mode" not in output
    assert "mcp(harness-mesh/list_harnesses)" not in output


def test_install_none_reports_that_nothing_was_approved(tmp_path):
    result = _invoke(tmp_path, "--yes", "--approve", "none")
    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "No MCP tools were pre-approved" in output
    assert "Approved 10 tools" not in output


def test_uninstall_and_dry_run_do_not_print_the_final_summary(tmp_path):
    dry = _plain(_invoke(tmp_path, "--dry-run").output)
    assert "Approved 10 tools" not in dry
    assert "approvals: level=dispatch tools=10 status=planned" in dry
    removed = _plain(_invoke(tmp_path, "--yes", "--uninstall").output)
    assert "Approved 10 tools" not in removed
