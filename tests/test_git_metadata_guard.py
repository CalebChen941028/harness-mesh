"""Mesh Git commands disable hooks and detect metadata tampering."""

from __future__ import annotations

import pytest

from harness_mesh.workspace import WorkspaceError, WorkspaceManager
from tests.helpers import init_git_repo


def test_mesh_commits_ignore_repository_hooks_and_reject_tampered_metadata(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-hook", sync_uncommitted=False)

    marker = tmp_path / "hook-ran"
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)

    workspace._commit_worktree_baseline("task-hook", info.path, "test commit")
    assert not marker.exists()

    with pytest.raises(WorkspaceError, match="Git metadata changed"):
        workspace.apply_worktree_changes("task-hook", strategy="patch")
