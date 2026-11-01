"""Round 2 WP-R2: symlinks must stay inside the worktree after rewriting."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from harness_mesh.workspace import WorkspaceError, WorkspaceManager
from tests.helpers import init_git_repo


def _git(*args, cwd: Path):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def test_untracked_absolute_link_is_rewritten_and_cannot_write_primary(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"real.txt": "primary base\n"})
    (root / "abs_untracked.txt").symlink_to(root / "real.txt")
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-abs-untracked")

    link = info.path / "abs_untracked.txt"
    assert link.is_symlink()
    assert not Path(os.readlink(link)).is_absolute()
    link.write_text("changed through worktree\n", encoding="utf-8")
    assert (root / "real.txt").read_text(encoding="utf-8") == "primary base\n"
    assert (info.path / "real.txt").read_text(encoding="utf-8") == "changed through worktree\n"


def test_tracked_absolute_link_is_rewritten_on_worktree_creation(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"real.txt": "base\n"})
    (root / "abs_tracked.txt").symlink_to(root / "real.txt")
    _git("add", "abs_tracked.txt", cwd=root)
    _git("commit", "-qm", "tracked absolute symlink", cwd=root)

    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-abs-tracked")
    link = info.path / "abs_tracked.txt"
    assert link.is_symlink()
    assert not Path(os.readlink(link)).is_absolute()
    link.write_text("worktree only\n", encoding="utf-8")
    assert (root / "real.txt").read_text(encoding="utf-8") == "base\n"


def test_external_and_dangling_links_are_reported_not_deleted(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    (root / "external.txt").symlink_to(outside / "secret.txt")
    (root / "dangling.txt").symlink_to(root / "missing.txt")
    _git("add", "external.txt", "dangling.txt", cwd=root)
    _git("commit", "-qm", "tracked escaping symlinks", cwd=root)

    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-escaping")
    assert "external.txt" in info.escaping_symlinks
    assert "dangling.txt" in info.escaping_symlinks
    assert (info.path / "external.txt").is_symlink()
    assert (info.path / "dangling.txt").is_symlink()


def test_apply_rejects_symlink_diff_escaping_target_root(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-escape-apply", sync_uncommitted=False)
    (info.path / "bad-link").symlink_to(outside / "secret.txt")

    with pytest.raises(WorkspaceError, match="symlink"):
        workspace.apply_worktree_changes("task-escape-apply", strategy="patch")
