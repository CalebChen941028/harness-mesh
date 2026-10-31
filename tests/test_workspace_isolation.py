"""Real Git worktree isolation, checkpoint capture and patch application."""

from __future__ import annotations

import subprocess

import pytest

from harness_mesh.models import TaskRequest
from harness_mesh.workspace import WorkspaceError, WorkspaceManager
from tests.helpers import init_git_repo


def test_worktree_isolation_capture_and_patch_application(tmp_path):
    root = tmp_path / "repo"
    init_git_repo(root, {"main.txt": "base\n", "keep.txt": "keep\n"})
    workspace = WorkspaceManager(root)

    info = workspace.create_worktree("task-iso")
    assert info.path.exists()
    assert info.path != root
    assert info.branch == "harness-mesh/task-iso"
    assert not info.reused
    assert (root / "main.txt").read_text(encoding="utf-8") == "base\n"
    assert not (root / "new.txt").exists()

    (info.path / "main.txt").write_text("worktree change\n", encoding="utf-8")
    (info.path / "new.txt").write_text("new file from assignment\n", encoding="utf-8")
    (info.path / "keep.txt").unlink()

    changes = workspace.collect_changes(info.path)
    assert set(changes.modified_files) == {"main.txt", "new.txt", "keep.txt"}
    assert "worktree change" in changes.diff
    assert "new file from assignment" in changes.diff
    assert "deleted file mode" in changes.diff or "keep.txt" in changes.diff

    request = TaskRequest(
        task_id="task-iso",
        target_harness="claude_code",
        objective="demonstrate checkpoint",
        workspace_root=str(root),
    )
    checkpoint = workspace.capture_checkpoint(
        request,
        info.path,
        "claude_code",
        "claude-opus-9",
        "quota_exhausted:budget_exceeded",
        stdout_tail="the tail of the interrupted output",
        assignment_id="asg_iso_1",
    )
    assert set(checkpoint.partial_modified_files) == {"main.txt", "new.txt", "keep.txt"}
    assert "worktree change" in checkpoint.partial_git_diff
    assert checkpoint.partial_stdout_tail == "the tail of the interrupted output"
    assert checkpoint.last_assignment_id == "asg_iso_1"
    assert checkpoint.worktree_path == str(info.path)

    applied = workspace.apply_worktree_changes("task-iso", workspace_root=root, strategy="patch")
    assert applied.used_fallback is False
    assert set(applied.applied_files) == {"main.txt", "new.txt", "keep.txt"}
    assert (root / "main.txt").read_text(encoding="utf-8") == "worktree change\n"
    assert (root / "new.txt").read_text(encoding="utf-8") == "new file from assignment\n"
    assert not (root / "keep.txt").exists()

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    assert ".harness-mesh" not in status
    assert "main.txt" in status


def test_merge_conflict_raises_and_never_overwrites_primary_workspace(tmp_path):
    root = tmp_path / "repo"
    init_git_repo(root, {"main.txt": "base\n"})
    workspace = WorkspaceManager(root)

    info = workspace.create_worktree("task-merge-conflict")
    (info.path / "main.txt").write_text("worktree change\n", encoding="utf-8")
    (root / "main.txt").write_text("primary local change\n", encoding="utf-8")

    with pytest.raises(WorkspaceError):
        workspace.apply_worktree_changes("task-merge-conflict", workspace_root=root, strategy="merge")

    assert (root / "main.txt").read_text(encoding="utf-8") == "primary local change\n"


def test_worktree_reuse_and_copy_strategy(tmp_path):
    root = tmp_path / "repo"
    init_git_repo(root, {"main.txt": "base\n"})
    workspace = WorkspaceManager(root)

    first = workspace.create_worktree("task-resume")
    (first.path / "first.txt").write_text("first assignment\n", encoding="utf-8")

    second = workspace.create_worktree("task-resume")
    assert second.reused is True
    assert second.path == first.path
    assert (second.path / "first.txt").exists()

    (second.path / "second.txt").write_text("second assignment\n", encoding="utf-8")

    copied = workspace.apply_worktree_changes("task-resume", workspace_root=root, strategy="copy")
    assert copied.strategy == "copy"
    assert set(copied.applied_files) == {"first.txt", "second.txt"}
    assert (root / "first.txt").read_text(encoding="utf-8") == "first assignment\n"
    assert (root / "second.txt").read_text(encoding="utf-8") == "second assignment\n"

    workspace.remove_worktree("task-resume")
    assert not first.path.exists()
    assert workspace.list_worktrees()
