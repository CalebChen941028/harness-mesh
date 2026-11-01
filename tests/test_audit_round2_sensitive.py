"""Round 2 WP-R1: sensitive filtering must not hide ordinary source files."""

from __future__ import annotations

from harness_mesh.security import SENSITIVE_FILE_PATTERNS, is_sensitive_name, is_sensitive_path
from harness_mesh.workspace import WorkspaceManager
from tests.helpers import init_git_repo


def test_credentials_prone_source_names_are_visible_and_reported(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-sensitive-source", sync_uncommitted=False)
    files = {
        "credentials_service.py": "def load(): pass\n",
        "credentials.py": "def load(): pass\n",
        ".env.example": "TOKEN=\n",
        "token_utils.py": "VALUE = 1\n",
        "server.key.md": "documentation\n",
        ".env": "SECRET=1\n",
        "id_rsa": "PRIVATE\n",
        "id_rsa.pub": "PUBLIC\n",
        ".ssh/config": "Host *\n",
    }
    for relative, content in files.items():
        path = info.path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    changes = workspace.collect_changes(info.path)
    for expected in (".env.example", "credentials.py", "credentials_service.py", "server.key.md", "token_utils.py"):
        assert expected in changes.modified_files, expected
    for hidden in (".env", "id_rsa", ".ssh/config"):
        assert hidden not in changes.modified_files, hidden
        assert hidden in changes.skipped_sensitive, hidden
    assert "id_rsa.pub" in changes.modified_files
    assert "SECRET=1" not in changes.diff


def test_git_info_exclude_contains_only_mesh_state(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    workspace.ensure_repository()
    exclude = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".harness-mesh" in exclude
    assert ".env" not in exclude
    assert "credentials" not in exclude
    assert "*.pem" not in exclude


def test_path_level_sensitive_rules_and_public_keys(tmp_path):
    assert is_sensitive_path(".ssh/config")
    assert is_sensitive_path("nested/.aws/credentials")
    assert is_sensitive_path("repo/.git/config")
    assert is_sensitive_name("id_rsa")
    assert not is_sensitive_name("id_rsa.pub")
    assert not is_sensitive_name("credentials_service.py")
    assert not is_sensitive_name(".env.example")
    assert not is_sensitive_name("server.key.md")
    assert ".env" in SENSITIVE_FILE_PATTERNS


def test_apply_result_reports_skipped_sensitive(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-sensitive-apply", sync_uncommitted=False)
    (info.path / "safe.py").write_text("SAFE=1\n", encoding="utf-8")
    (info.path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    applied = workspace.apply_worktree_changes("task-sensitive-apply", strategy="copy")
    assert "safe.py" in applied.applied_files
    assert ".env" in applied.warnings
