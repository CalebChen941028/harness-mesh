"""Round 3 WP-R10: the active-assignment cap is shared through state.db.

Each worker harness starts its own MCP server process, so a per-router counter
always began at zero and the global cap never held across a recursion chain.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from harness_mesh.models import AssignmentRecord, TaskRequest, TaskStatus
from harness_mesh.router import MeshRouter
from harness_mesh.storage import StateStore
from tests.helpers import FakeAdapter, make_mesh, success, write_codex_config


def _second_router(mesh, adapters) -> MeshRouter:
    """A second router over the same database file, as another process would."""

    storage = StateStore(mesh.storage.path)
    return MeshRouter(
        mesh.config,
        storage,
        discovery=mesh.discovery,
        skills=mesh.skills,
        context=mesh.context,
        workspace=mesh.workspace,
        adapters=adapters,
        guardian=mesh.guardian,
        workspace_root=mesh.workspace_root,
        env=mesh.env,
        home_dir=mesh.home_dir,
    )


@pytest.mark.asyncio
async def test_shared_database_cap_rejects_a_second_router(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(adapter, call):
        started.set()
        await release.wait()
        return success("holder done")

    mesh = make_mesh(
        tmp_path,
        adapters={"codex": FakeAdapter("codex", slow_handler)},
        config_updates={"max_active_assignments": 1},
    )
    write_codex_config(mesh.home_dir, model="gpt-5-codex")
    other = _second_router(mesh, {"codex": FakeAdapter("codex", lambda a, c: success("other done"))})
    holder = asyncio.create_task(
        mesh.router.dispatch(
            TaskRequest(
                task_id="holder-task",
                target_harness="codex",
                objective="hold the only slot",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        rejected = await other.dispatch(
            TaskRequest(
                task_id="second-task",
                target_harness="codex",
                objective="must not start",
                workspace_root=str(mesh.workspace_root),
            )
        )
        assert rejected.status == TaskStatus.REJECTED
        assert "max_active_assignments" in (rejected.error_detail or "")
        assert not hasattr(mesh.router, "_active_assignments")
    finally:
        release.set()
        await holder
        other.storage.close()
        mesh.storage.close()


@pytest.mark.asyncio
async def test_dead_process_rows_are_reaped_and_do_not_occupy_the_limit(tmp_path):
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait(timeout=10)
    dead_pid = finished.pid

    mesh = make_mesh(
        tmp_path,
        adapters={"codex": FakeAdapter("codex", lambda a, c: success("ran"))},
        config_updates={"max_active_assignments": 1},
    )
    write_codex_config(mesh.home_dir, model="gpt-5-codex")
    stale = AssignmentRecord(
        task_id="crashed-task",
        attempt=1,
        harness="codex",
        model="gpt-5-codex",
        status="running",
        call_depth=1,
        trace_chain=["codex"],
        pid=dead_pid,
    )
    try:
        mesh.storage.add_assignment(stale)
        result = await mesh.router.dispatch(
            TaskRequest(
                task_id="fresh-task",
                target_harness="codex",
                objective="should get the slot",
                workspace_root=str(mesh.workspace_root),
            )
        )
        reaped = mesh.storage.list_assignments("crashed-task")[0]
    finally:
        mesh.storage.close()

    assert result.status == TaskStatus.COMPLETED, result.error_detail
    assert reaped.status == "ended_error"
    assert reaped.error_reason == "stale"
