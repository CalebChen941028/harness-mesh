"""``hmesh install`` tells headless users how to approve the MCP server."""

from __future__ import annotations

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
    config = Path(__file__).resolve().parents[1] / "config" / "harnesses.example.yaml"
    return CliRunner().invoke(
        cli_app,
        ["--workspace", str(workspace), "--config", str(config), "install", "--harness", "zcode", *args],
        env={"HOME": str(tmp_path / "home"), "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1"},
    )


def test_install_prints_the_headless_approval_hint(tmp_path):
    result = _invoke(tmp_path, "--yes")
    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "default_tools_approval_mode" in output
    assert "mcp(harness-mesh/list_harnesses)" in output


def test_uninstall_and_dry_run_do_not_print_the_hint(tmp_path):
    dry = _plain(_invoke(tmp_path, "--dry-run").output)
    assert "default_tools_approval_mode" not in dry
    removed = _plain(_invoke(tmp_path, "--yes", "--uninstall").output)
    assert "default_tools_approval_mode" not in removed
