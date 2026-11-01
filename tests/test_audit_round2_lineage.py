"""Round 2 WP-R4: worktree lineage fallback and active assignment limit."""

from __future__ import annotations

import json

import pytest

from harness_mesh.mcp_server import create_server
from harness_mesh.models import TaskRequest, TaskStatus
from tests.helpers import FakeAdapter, make_mesh, success


@pytest.mark.asyncio
async def test_worktree_cwd_restores_lineage_without_environment(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path, adapters={"claude_code": FakeAdapter("claude_code", lambda a, c: success("ok"))})
    try:
        max_depth = 3
        parent = TaskRequest(
            task_id="parent-task",
            target_harness="claude_code",
            objective="parent",
            call_depth=max_depth,
            trace_chain=["claude_code"],
            workspace_root=str(mesh.workspace_root),
        )
        mesh.storage.create_task(parent, status=TaskStatus.RUNNING)
        info = mesh.workspace.create_worktree("parent-task", sync_uncommitted=False)
        monkeypatch.chdir(info.path)
        monkeypatch.delenv("HARNESS_MESH_CALL_DEPTH", raising=False)
        monkeypatch.delenv("HARNESS_MESH_TRACE_CHAIN", raising=False)

        server = create_server(mesh.router)
        result = await server.call_tool(
            "dispatch_task",
            {"target_harness": "claude_code", "objective": "child", "call_depth": 0},
        )
        payload = json.loads((result[0] if isinstance(result, tuple) else result)[0].text)
        assert payload["status"] == TaskStatus.REJECTED.value
        assert "call_depth=3" in payload["error_detail"]
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_global_active_assignment_limit_rejects_new_dispatch(tmp_path):
    mesh = make_mesh(tmp_path, adapters={"claude_code": FakeAdapter("claude_code", lambda a, c: success("ok"))})
    try:
        mesh.config.max_active_assignments = 0
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="blocked",
                workspace_root=str(mesh.workspace_root),
            )
        )
        assert result.status == TaskStatus.REJECTED
        assert "max_active_assignments" in (result.error_detail or "")
    finally:
        mesh.storage.close()
