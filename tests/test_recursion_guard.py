"""Recursion guard: max depth and A -> B -> A cycle rejection."""

from __future__ import annotations

import pytest

from harness_mesh.models import TaskRequest, TaskStatus
from harness_mesh.router import NO_HARNESS_MESSAGE
from tests.helpers import FakeAdapter, make_mesh, success, write_claude_settings, write_codex_config


@pytest.mark.asyncio
async def test_cycle_and_depth_are_rejected_before_any_worktree_is_created(tmp_path):
    mesh = make_mesh(tmp_path, config_updates={"max_call_depth": 2})
    try:
        cycle = await mesh.router.dispatch(
            TaskRequest(
                target_harness="codex",
                objective="this must never run",
                workspace_root=str(mesh.workspace_root),
                trace_chain=["claude_code", "codex"],
            )
        )
        assert cycle.status == TaskStatus.REJECTED
        assert "cycle" in (cycle.error_detail or "")
        assert not (mesh.workspace.worktrees_root / cycle.task_id).exists()

        too_deep = await mesh.router.dispatch(
            TaskRequest(
                target_harness="codex",
                objective="this must never run either",
                workspace_root=str(mesh.workspace_root),
                call_depth=2,
                trace_chain=["claude_code"],
            )
        )
        assert too_deep.status == TaskStatus.REJECTED
        assert "depth" in (too_deep.error_detail or "")

        unknown = await mesh.router.dispatch(
            TaskRequest(
                target_harness="not-a-harness",
                objective="unknown harness",
                workspace_root=str(mesh.workspace_root),
            )
        )
        assert unknown.status == TaskStatus.REJECTED
        assert "unknown target_harness" in (unknown.error_detail or "")

        not_rejected = await mesh.router.dispatch(
            TaskRequest(
                target_harness="codex",
                objective="allowed depth but no model is available",
                workspace_root=str(mesh.workspace_root),
                call_depth=1,
                trace_chain=["claude_code"],
            )
        )
        assert not_rejected.status == TaskStatus.FAILED
        # No adapter is registered in this fixture, so the new "nothing is
        # installed" message replaces the generic no_available_model detail.
        assert (not_rejected.error_detail or "") in {
            "no_available_model",
            NO_HARNESS_MESSAGE,
        }
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_auto_target_never_reenters_an_ancestor_harness(tmp_path):
    holder = {}
    call_counts = {"claude_code": 0, "codex": 0}

    async def claude_handler(adapter, call):
        call_counts["claude_code"] += 1
        assert call.call_depth == 1
        assert call.trace_chain == ["claude_code"]
        child = TaskRequest(
            task_id="auto-child",
            target_harness="auto",
            objective="nested auto dispatch",
            call_depth=call.call_depth,
            trace_chain=list(call.trace_chain),
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        holder["child"] = await mesh.router.dispatch(child)
        return success("top auto completed")

    async def codex_handler(adapter, call):
        call_counts["codex"] += 1
        holder["codex_depth"] = call.call_depth
        holder["codex_trace"] = list(call.trace_chain)
        return success("codex nested completed")

    mesh = make_mesh(
        tmp_path,
        adapters={
            "claude_code": FakeAdapter("claude_code", claude_handler),
            "codex": FakeAdapter("codex", codex_handler),
        },
        config_updates={"max_call_depth": 4},
    )
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        write_codex_config(mesh.home_dir, model="gpt-5-codex")
        top = await mesh.router.dispatch(
            TaskRequest(
                task_id="auto-top",
                target_harness="auto",
                objective="Dispatch a nested auto ticket.",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )

        assert top.status == TaskStatus.COMPLETED
        assert top.initial_harness == "claude_code"
        child = holder["child"]
        assert child.status == TaskStatus.COMPLETED
        assert child.target_harness == "codex"
        assert call_counts == {"claude_code": 1, "codex": 1}
        assert holder["codex_depth"] == 2
        assert holder["codex_trace"] == ["claude_code", "codex"]
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_nested_dispatch_cannot_loop_back_to_an_ancestor_harness(tmp_path):
    holder = {}
    call_counts = {"claude_code": 0, "codex": 0}

    async def claude_handler(adapter, call):
        call_counts["claude_code"] += 1
        nested = TaskRequest(
            task_id="nested-codex",
            target_harness="codex",
            objective="nested work delegated from claude",
            call_depth=1,
            trace_chain=list(call.trace_chain),
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        holder["nested"] = await mesh.router.dispatch(nested)
        return success("claude top-level completed")

    async def codex_handler(adapter, call):
        call_counts["codex"] += 1
        cycle = TaskRequest(
            task_id="nested-claude-again",
            target_harness="claude_code",
            objective="this loops back to the ancestor harness",
            call_depth=2,
            trace_chain=list(call.trace_chain),
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        holder["cycle"] = await mesh.router.dispatch(cycle)
        return success("codex nested completed")

    mesh = make_mesh(
        tmp_path,
        adapters={
            "claude_code": FakeAdapter("claude_code", claude_handler),
            "codex": FakeAdapter("codex", codex_handler),
        },
        config_updates={"max_call_depth": 3},
    )
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        write_codex_config(mesh.home_dir, model="gpt-5-codex")

        top = await mesh.router.dispatch(
            TaskRequest(
                task_id="top-task",
                target_harness="claude_code",
                objective="Delegate a nested task.",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )

        assert top.status == TaskStatus.COMPLETED
        assert holder["nested"].status == TaskStatus.COMPLETED
        assert holder["cycle"].status == TaskStatus.REJECTED
        assert "claude_code" in (holder["cycle"].error_detail or "")
        assert call_counts == {"claude_code": 1, "codex": 1}
        assert not (mesh.workspace.worktrees_root / "nested-claude-again").exists()
    finally:
        mesh.storage.close()
