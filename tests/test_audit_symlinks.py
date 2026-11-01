"""Audit WP3: safe symlink handling during dirty sync and patch copying."""

from __future__ import annotations

import pytest

from harness_mesh.workspace import WorkspaceError, WorkspaceManager
from tests.helpers import init_git_repo


def test_dirty_sync_recreates_internal_symlink_and_skips_external_and_dangling(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"inside.txt": "inside\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside secret\n", encoding="utf-8")
    (root / "inside-link").symlink_to(root / "inside.txt")
    (root / "outside-link").symlink_to(outside / "secret.txt")
    (root / "dangling-link").symlink_to(root / "missing.txt")
    (root / "inside.txt").write_text("inside modified\n", encoding="utf-8")

    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-symlink")

    inside_link = info.path / "inside-link"
    assert inside_link.is_symlink()
    assert inside_link.read_text(encoding="utf-8") == "inside modified\n"
    assert not (info.path / "outside-link").exists()
    assert not (info.path / "outside-link").is_symlink()
    assert not (info.path / "dangling-link").exists()
    assert not (info.path / "dangling-link").is_symlink()


def test_copy_changes_recreates_internal_symlink_and_skips_external(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside secret\n", encoding="utf-8")

    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-symlink-copy")
    (info.path / "new-file.txt").write_text("new\n", encoding="utf-8")
    (info.path / "agent-link").symlink_to(info.path / "new-file.txt")

    applied = workspace.apply_worktree_changes("task-symlink-copy", strategy="copy")
    assert "agent-link" in applied.applied_files
    assert (root / "agent-link").is_symlink()
    assert (root / "agent-link").read_text(encoding="utf-8") == "new\n"
    assert not (root / "bad-link").exists()
    assert not (root / "bad-link").is_symlink()


def test_copy_changes_rejects_external_symlink(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside secret\n", encoding="utf-8")

    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-symlink-external-copy", sync_uncommitted=False)
    (info.path / "bad-link").symlink_to(outside / "secret.txt")

    changes = workspace.collect_changes(info.path)
    assert "bad-link" in changes.modified_files
    with pytest.raises(WorkspaceError, match="symlink"):
        workspace.apply_worktree_changes("task-symlink-external-copy", strategy="copy")
    assert not (root / "bad-link").exists()
