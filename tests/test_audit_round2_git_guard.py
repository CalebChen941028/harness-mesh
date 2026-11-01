"""Round 2 WP-R3: Git metadata tampering survives resume and aborts assignments."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_mesh.models import TaskRequest, TaskStatus
from harness_mesh.workspace import WorkspaceError, WorkspaceManager
from tests.helpers import FakeAdapter, init_git_repo, make_mesh, success, write_claude_settings


def _write_hook(root: Path, marker: Path) -> None:
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)


def test_reused_worktree_does_not_overwrite_guard_and_detects_tamper(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    workspace.create_worktree("task-resume-guard", sync_uncommitted=False)
    _write_hook(root, tmp_path / "marker")

    workspace.create_worktree("task-resume-guard", sync_uncommitted=False)
    with pytest.raises(WorkspaceError, match="Git metadata changed"):
        workspace.apply_worktree_changes("task-resume-guard", strategy="patch")


@pytest.mark.asyncio
async def test_tamper_after_first_assignment_aborts_without_second_assignment(tmp_path):
    calls: list[str] = []
    marker = tmp_path / "hook-ran"

    def handler(adapter, call):
        calls.append(call.assignment_id)
        _write_hook(Path(call.workdir).parents[2], marker)
        return success("assignment one done")

    mesh = make_mesh(tmp_path, adapters={"claude_code": FakeAdapter("claude_code", handler)})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        request = TaskRequest(
            target_harness="claude_code",
            objective="write a result",
            fallback_harnesses=["codex"],
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        result = await mesh.router.dispatch(request)
        assert result.status == TaskStatus.FAILED
        assert result.error_detail == "git_metadata_tampered"
        assert len(calls) == 1
        assert len(mesh.storage.list_assignments(request.task_id)) == 1
        assert result.worktree_path and Path(result.worktree_path).exists()
    finally:
        mesh.storage.close()
