"""Audit WP2/WP3: sensitive files and symlinks in repository/worktree handling."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness_mesh.security import SENSITIVE_FILE_PATTERNS, is_sensitive_name
from harness_mesh.workspace import WorkspaceError, WorkspaceManager
from tests.helpers import init_git_repo


def _git_files(root: Path) -> list[str]:
    return (
        subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.splitlines()
    )


def test_sensitive_pattern_table_matches_expected_names():
    for name in (".env", ".env.production", "server.pem", "server.key", "id_rsa", "id_ed25519.pub",
                 "client.p12", "credentials.json", ".npmrc", ".netrc", "app.keystore"):
        assert is_sensitive_name(name), name
    assert not is_sensitive_name("README.md")
    assert ".env" in SENSITIVE_FILE_PATTERNS


def test_baseline_commit_excludes_sensitive_files(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_text("safe\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (root / "server.pem").write_text("private key\n", encoding="utf-8")
    (root / "credentials.json").write_text("{}\n", encoding="utf-8")

    workspace = WorkspaceManager(root, auto_init_repo=True)
    workspace.ensure_repository()

    tracked = _git_files(root)
    assert "a.txt" in tracked
    assert not any(is_sensitive_name(path) for path in tracked)
    exclude = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".env" in exclude
    assert "*.pem" in exclude


def test_non_git_workspace_is_refused_by_default(tmp_path):
    root = tmp_path / "not-a-repo"
    root.mkdir()
    with pytest.raises(WorkspaceError, match="not a Git repository"):
        WorkspaceManager(root).create_worktree("task-no-repo")


def test_dirty_sync_skips_sensitive_and_preserves_normal_files(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"tracked.txt": "base\n"})
    (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (root / "untracked.txt").write_text("copy me\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")

    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-sensitive")

    assert (info.path / "tracked.txt").read_text(encoding="utf-8") == "dirty\n"
    assert (info.path / "untracked.txt").read_text(encoding="utf-8") == "copy me\n"
    assert not (info.path / ".env").exists()
    tracked = _git_files(info.path)
    assert not any(is_sensitive_name(path) for path in tracked)

