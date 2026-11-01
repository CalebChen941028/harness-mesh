"""Round 4 WP-R14: cancelled or crashed dispatches must close their Assignment.

``_dispatch`` marks an assignment finished on every normal outcome.  Before
round four, a cancellation or an unhandled exception escaped before that
update, leaving a ``running`` row whose pid is the long-lived dispatching
process.  ``count_live_assignments`` then treats the dead attempt as live and
the row keeps a ``max_active_assignments`` slot forever.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from harness_mesh.models import TaskRequest, TaskStatus
from tests.helpers import FakeAdapter, make_mesh, success, write_codex_config


def _request(mesh, objective: str) -> TaskRequest:
    return TaskRequest(
        target_harness="codex",
        objective=objective,
        workspace_root=str(mesh.workspace_root),
    )


def _assignments(mesh, task_id: str):
    return mesh.storage.list_assignments(task_id)


@pytest.mark.asyncio
async def test_cancel_task_closes_the_running_assignment(tmp_path):
    started = asyncio.Event()

    async def handler(adapter, call):
        started.set()
        await asyncio.sleep(60)
        return success("too late")

    mesh = make_mesh(tmp_path, adapters={"codex": FakeAdapter("codex", handler)})
    write_codex_config(mesh.home_dir, model="gpt-5-codex")
    request = _request(mesh, "cancel a sleeping assignment")
    try:
        task = asyncio.create_task(mesh.router.dispatch(request))
        await asyncio.wait_for(started.wait(), timeout=10)
        await mesh.router.cancel_task(request.task_id)
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assignments = _assignments(mesh, request.task_id)
        assert assignments, "the adapter ran, so one assignment row must exist"
        assert [item.status for item in assignments if item.status == "running"] == []
        assert assignments[-1].error_reason == "cancelled"
        assert mesh.storage.count_live_assignments() == 0
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_direct_dispatch_cancellation_closes_the_row_inside_dispatch(tmp_path):
    """The cleanup must live in ``_dispatch``, not only in ``dispatch``.

    ``cancel_task`` already reaches ``dispatch``'s ``CancelledError`` handler,
    which closes rows as a side effect.  Cancelling ``_dispatch`` itself
    isolates the new cleanup path the work package asks for.
    """

    started = asyncio.Event()

    async def handler(adapter, call):
        started.set()
        await asyncio.sleep(60)
        return success("too late")

    mesh = make_mesh(tmp_path, adapters={"codex": FakeAdapter("codex", handler)})
    write_codex_config(mesh.home_dir, model="gpt-5-codex")
    request = _request(mesh, "cancel the inner dispatch coroutine")
    try:
        task = asyncio.create_task(mesh.router._dispatch(request))
        await asyncio.wait_for(started.wait(), timeout=10)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assignments = _assignments(mesh, request.task_id)
        assert assignments, "the adapter ran, so one assignment row must exist"
        assert [item.status for item in assignments if item.status == "running"] == []
        assert assignments[-1].error_reason == "cancelled"
        assert mesh.storage.count_live_assignments() == 0
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_unhandled_dispatch_exception_closes_the_running_assignment(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("prompt rendering exploded")

    mesh = make_mesh(
        tmp_path, adapters={"codex": FakeAdapter("codex", lambda a, c: success("never"))}
    )
    write_codex_config(mesh.home_dir, model="gpt-5-codex")
    monkeypatch.setattr("harness_mesh.router.build_handoff_prompt", boom)
    request = _request(mesh, "crash after the assignment row exists")
    try:
        result = await mesh.router.dispatch(request)

        assignments = _assignments(mesh, request.task_id)
        assert result.status == TaskStatus.FAILED
        assert "prompt rendering exploded" in (result.error_detail or "")
        assert assignments, "the assignment row is created before the prompt"
        assert [item.status for item in assignments if item.status == "running"] == []
        assert assignments[-1].error_reason == "dispatch_exception"
        assert mesh.storage.count_live_assignments() == 0
    finally:
        mesh.storage.close()
