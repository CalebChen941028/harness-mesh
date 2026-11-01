"""Audit WP9: apply status, patch preflight and interface consistency."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

import harness_mesh
from harness_mesh.cli import app as cli_app
from harness_mesh.mcp_server import create_server
from harness_mesh.models import TaskRequest, TaskStatus
from harness_mesh.workspace import WorkspaceError, WorkspaceManager
from tests.helpers import init_git_repo, make_mesh


def _payload(result) -> dict:
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


@pytest.mark.asyncio
async def test_mcp_rejects_copy_strategy_and_requires_real_task(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        server = create_server(mesh.router)
        copied = _payload(
            await server.call_tool("apply_task_patch", {"task_id": "missing", "strategy": "copy"})
        )
        assert "copy" in copied["error"].lower()
        missing = _payload(
            await server.call_tool("apply_task_patch", {"task_id": "missing", "strategy": "patch"})
        )
        assert "error" in missing
    finally:
        mesh.storage.close()


def test_router_apply_requires_completed_status(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        request = TaskRequest(
            task_id="pending-apply",
            target_harness="claude_code",
            objective="pending",
            workspace_root=str(mesh.workspace_root),
        )
        mesh.storage.create_task(request, status=TaskStatus.PENDING)
        with pytest.raises(WorkspaceError, match="completed"):
            mesh.router.apply_task_patch("pending-apply")
    finally:
        mesh.storage.close()


def test_patch_preflight_reports_conflict_files_without_touching_primary(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-conflict", sync_uncommitted=False)
    (info.path / "README.md").write_text("worktree version\n", encoding="utf-8")
    (root / "README.md").write_text("primary version\n", encoding="utf-8")

    with pytest.raises(WorkspaceError) as excinfo:
        workspace.apply_worktree_changes("task-conflict", strategy="patch")
    message = str(excinfo.value)
    assert "README.md" in message
    assert (root / "README.md").read_text(encoding="utf-8") == "primary version\n"


def test_cli_copy_requires_force_copy(tmp_path):
    workspace = tmp_path / "workspace"
    init_git_repo(workspace, {"README.md": "cli\n"})
    config = Path(__file__).resolve().parents[1] / "config" / "harnesses.example.yaml"
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["--workspace", str(workspace), "--config", str(config), "apply", "task-x", "--strategy", "copy"],
        env={"HOME": str(tmp_path / "home"), "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1"},
    )
    assert result.exit_code != 0
    assert "force-copy" in result.output
    help_result = runner.invoke(cli_app, ["apply", "--help"])
    # Rich styles option names with ANSI codes when CI forces colour output.
    assert "--force-copy" in re.sub(r"\x1b\[[0-9;]*m", "", help_result.output)


def test_docstring_and_dependency_bounds_and_ci_units():
    import tomllib

    import harness_mesh.mcp_server as server_module

    assert "eighteen" in (server_module.__doc__ or "").lower()
    project = tomllib.loads((Path("pyproject.toml")).read_text(encoding="utf-8"))["project"]
    bounds = {
        "pydantic": "<3",
        "pyyaml": "<7",
        "httpx": "<1",
        "typer": "<1",
    }
    for dependency in project["dependencies"]:
        name = dependency.split(">=", 1)[0].split("<", 1)[0].split("=", 1)[0]
        if name in bounds:
            assert bounds[name] in dependency, dependency
    assert Path(".github/dependabot.yml").exists()
    assert "pip-audit" in Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert harness_mesh.__version__ == "0.4.0"
