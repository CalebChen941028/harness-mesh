"""Worktree/Git isolation, sensitive diffs and apply safety.

Covers B1 (worktree ``.git`` validation + guard), B2 (strict removal and the
reuse marker), B4 (sensitive diff filtering), B5 (quoted paths and symlink
verification) and B6 (clean merge target + rollback).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from harness_mesh.workspace import WorkspaceError, WorkspaceManager, _unquote_git_path
from tests.helpers import init_git_repo


def _git(*args: str, cwd: Path):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


# ---------------------------------------------------------------------------
# worktree .git pointer validation
# ---------------------------------------------------------------------------
def test_fake_gitdir_inside_worktree_is_rejected(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-fake-gitdir", sync_uncommitted=False)

    fake = info.path / "fake-gitdir"
    (fake / "objects").mkdir(parents=True)
    (fake / "refs").mkdir(parents=True)
    (fake / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (fake / "config").write_text(
        "[filter \"evil\"]\n\tclean = /bin/echo\n", encoding="utf-8"
    )
    (info.path / ".git").write_text(f"gitdir: {fake}\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="gitdir"):
        workspace.collect_changes(info.path)
    with pytest.raises(WorkspaceError, match="gitdir"):
        workspace._validated_worktree_gitdir("task-fake-gitdir")


def test_git_pointer_symlink_and_directory_are_rejected(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-gitfile-type", sync_uncommitted=False)
    gitfile = info.path / ".git"
    assert gitfile.is_file()

    gitfile.unlink()
    os.symlink(info.path / "elsewhere", gitfile)
    with pytest.raises(WorkspaceError, match="regular file"):
        workspace._validated_worktree_gitdir("task-gitfile-type")

    gitfile.unlink()
    gitfile.mkdir()
    with pytest.raises(WorkspaceError, match="regular file"):
        workspace._validated_worktree_gitdir("task-gitfile-type")


def test_gitdir_of_another_worktree_is_rejected(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    first = workspace.create_worktree("task-owner-a", sync_uncommitted=False)
    second = workspace.create_worktree("task-owner-b", sync_uncommitted=False)

    stolen = (second.path / ".git").read_text(encoding="utf-8")
    (first.path / ".git").write_text(stolen, encoding="utf-8")

    with pytest.raises(WorkspaceError, match="backlink"):
        workspace._validated_worktree_gitdir("task-owner-a")


def test_git_metadata_guard_covers_worktree_git_file(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-guard-gitfile", sync_uncommitted=False)

    gitdir = workspace._validated_worktree_gitdir("task-guard-gitfile")
    # Same valid target, different .git bytes: the recorded hash must notice.
    (info.path / ".git").write_text(f"gitdir: {gitdir}   \n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="Git metadata changed"):
        workspace.verify_git_guard("task-guard-gitfile")


# ---------------------------------------------------------------------------
# strict removal, reserved ids and the reuse marker
# ---------------------------------------------------------------------------
def test_safe_remove_dir_refuses_root_and_outside(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    workspace.worktrees_root.mkdir(parents=True, exist_ok=True)

    with pytest.raises(WorkspaceError, match="root itself"):
        workspace._safe_remove_dir(workspace.worktrees_root, workspace.worktrees_root)

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(WorkspaceError, match="outside worktrees root"):
        workspace._safe_remove_dir(outside, workspace.worktrees_root)

    junk = workspace.worktrees_root / "junk"
    junk.mkdir()
    (junk / "file.txt").write_text("x", encoding="utf-8")
    workspace._safe_remove_dir(junk, workspace.worktrees_root)
    assert not junk.exists()


@pytest.mark.parametrize("task_id", [".", "..", ".meta", "a..b", "a.", "-dash", "a/b", "a b", ""])
def test_reserved_or_malformed_task_ids_are_refused(tmp_path, task_id):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    with pytest.raises(WorkspaceError):
        workspace.create_worktree(task_id, sync_uncommitted=False)


def test_reuse_rejects_a_tampered_worktree_marker(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-marker", sync_uncommitted=False)
    marker_path = info.path / ".harness-mesh-worktree.json"
    assert marker_path.is_file()
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker == {"task_id": "task-marker", "branch": "harness-mesh/task-marker"}

    marker_path.chmod(0o644)
    marker_path.write_text(
        json.dumps({"task_id": "another-task", "branch": marker["branch"]}),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceError, match="marker mismatch"):
        workspace.create_worktree("task-marker", sync_uncommitted=False)


def test_markerless_worktree_from_an_older_release_is_adopted_once(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-legacy", sync_uncommitted=False)
    (info.path / ".harness-mesh-worktree.json").unlink()

    reopened = workspace.create_worktree("task-legacy", sync_uncommitted=False)
    assert reopened.reused is True
    assert reopened.path == info.path
    assert (info.path / ".harness-mesh-worktree.json").is_file()


def test_markerless_directory_without_mesh_metadata_is_rejected(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-no-meta", sync_uncommitted=False)
    (info.path / ".harness-mesh-worktree.json").unlink()
    (workspace._meta_dir() / "task-no-meta.json").unlink()

    with pytest.raises(WorkspaceError, match="marker"):
        workspace.create_worktree("task-no-meta", sync_uncommitted=False)


def test_preplanted_worktree_directory_is_refused(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    workspace.ensure_repository()
    planted = workspace.worktrees_root / "planted-task"
    planted.mkdir(parents=True)
    (planted / ".git").write_text("gitdir: /tmp/attacker\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="marker"):
        workspace.create_worktree("planted-task", sync_uncommitted=False)


# ---------------------------------------------------------------------------
# sensitive path filtering in diffs and file lists
# ---------------------------------------------------------------------------
def test_nested_and_uppercase_sensitive_paths_are_filtered(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-sensitive-nested", sync_uncommitted=False)
    secrets = {
        "pkg/nested/.env.production": "DB_PASSWORD=hunter2\n",
        "deep/.ssh/config": "Host *\n  IdentityFile ~/.ssh/id_rsa\n",
        "certs/Server.PEM": "-----BEGIN PRIVATE KEY-----\n",
        "Credentials/CREDENTIALS.json": '{"token": "x"}\n',
        "Normal.py": "SAFE = 1\n",
    }
    for relative, content in secrets.items():
        path = info.path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    changes = workspace.collect_changes(info.path)

    assert changes.modified_files == ["Normal.py"]
    assert "DB_PASSWORD" not in changes.diff
    assert "PRIVATE KEY" not in changes.diff
    assert '{"token": "x"}' not in changes.diff
    for hidden in secrets:
        if hidden != "Normal.py":
            assert hidden in changes.skipped_sensitive, hidden

    applied = workspace.apply_worktree_changes("task-sensitive-nested", strategy="patch")
    assert applied.applied_files == ["Normal.py"]
    assert not (root / "pkg" / "nested" / ".env.production").exists()


def test_non_ascii_changed_file_round_trips_through_diff(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-unicode", sync_uncommitted=False)
    target = info.path / "docs"
    target.mkdir()
    (target / "说明.txt").write_text("你好\n", encoding="utf-8")

    changes = workspace.collect_changes(info.path)
    assert "docs/说明.txt" in changes.modified_files
    assert "你好" in changes.diff

    applied = workspace.apply_worktree_changes("task-unicode", strategy="patch")
    assert applied.applied_files == ["docs/说明.txt"]
    assert (root / "docs" / "说明.txt").read_text(encoding="utf-8") == "你好\n"


def test_special_filenames_are_literal_pathspecs(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-special-names", sync_uncommitted=False)
    names = [
        "-leading-dash.txt",
        ":colon-name.txt",
        "space name.txt",
        "中文 文件.txt",
        'quote"name.txt',
    ]
    for name in names:
        (info.path / name).write_text(f"content of {name}\n", encoding="utf-8")

    changes = workspace.collect_changes(info.path)
    assert set(changes.modified_files) == set(names)
    for name in names:
        assert f"content of {name}" in changes.diff

    applied = workspace.apply_worktree_changes("task-special-names", strategy="patch")
    assert set(applied.applied_files) == set(names)
    for name in names:
        assert (root / name).read_text(encoding="utf-8") == f"content of {name}\n"


def test_large_change_uses_chunked_argv_fallback(tmp_path, monkeypatch):
    import harness_mesh.workspace as workspace_module

    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-many-files", sync_uncommitted=False)
    workspace._supports_pathspec_file = False
    monkeypatch.setattr(workspace_module, "_DIFF_ARGV_CHUNK_CHARS", 40)
    names = [f"generated/file-{index:02d}.txt" for index in range(12)]
    for name in names:
        path = info.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"value {name}\n", encoding="utf-8")

    changes = workspace.collect_changes(info.path)
    assert set(changes.modified_files) == set(names)
    for name in names:
        assert f"value {name}" in changes.diff

    applied = workspace.apply_worktree_changes("task-many-files", strategy="patch")
    assert set(applied.applied_files) == set(names)
    for name in names:
        assert (root / name).is_file()


# ---------------------------------------------------------------------------
# quoted paths, symlink checks and post-apply verification
# ---------------------------------------------------------------------------
def test_unquote_git_path_decodes_c_style_octal_escapes():
    assert _unquote_git_path('"b/docs/\\351\\223\\276\\346\\216\\245"') == "b/docs/链接"
    assert _unquote_git_path("b/plain.txt") == "b/plain.txt"
    assert _unquote_git_path('"a/with space.txt"') == "a/with space.txt"


def test_quoted_path_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-quoted-symlink", sync_uncommitted=False)
    (info.path / "docs").mkdir()
    (info.path / "docs" / "链接").symlink_to(outside / "secret.txt")

    with pytest.raises(WorkspaceError, match="symlink"):
        workspace.apply_worktree_changes("task-quoted-symlink", strategy="patch")
    assert not (root / "docs" / "链接").exists()


def test_internal_symlink_is_allowed_and_recreated(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-internal-symlink", sync_uncommitted=False)
    (info.path / "real.txt").write_text("inside\n", encoding="utf-8")
    (info.path / "link.txt").symlink_to("real.txt")

    applied = workspace.apply_worktree_changes("task-internal-symlink", strategy="patch")
    assert "link.txt" in applied.applied_files
    link = root / "link.txt"
    assert link.is_symlink()
    assert link.read_text(encoding="utf-8") == "inside\n"


def test_post_apply_symlink_verification_removes_escape(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = WorkspaceManager(root)
    root.joinpath("escape").symlink_to(outside)

    with pytest.raises(WorkspaceError, match="outside the target workspace"):
        workspace._verify_applied_symlinks(root, ["escape"], set())
    assert not (root / "escape").exists()


# ---------------------------------------------------------------------------
# merge requires a clean target and a failed apply rolls back
# ---------------------------------------------------------------------------
def test_merge_refuses_dirty_target_files(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"app.txt": "line1\nline2\nline3\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-merge-dirty", sync_uncommitted=False)
    (info.path / "app.txt").write_text("line1\nWORKTREE\nline3\n", encoding="utf-8")
    (root / "app.txt").write_text("line1\nLOCAL DIRTY\nline3\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="clean target"):
        workspace.apply_worktree_changes("task-merge-dirty", strategy="merge")
    assert (root / "app.txt").read_text(encoding="utf-8") == "line1\nLOCAL DIRTY\nline3\n"


def test_failed_three_way_merge_is_rolled_back(tmp_path, monkeypatch):
    root = tmp_path / "project"
    init_git_repo(root, {"app.txt": "line1\nline2\nline3\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-merge-rollback", sync_uncommitted=False)
    (info.path / "app.txt").write_text("line1\nWORKTREE\nline3\n", encoding="utf-8")

    # A staged conflicting change in the primary workspace is what makes
    # ``git apply --3way`` write conflict markers before failing.  The clean
    # target pre-check is disabled here on purpose to exercise the rollback.
    (root / "app.txt").write_text("line1\nLOCAL STAGED\nline3\n", encoding="utf-8")
    _git("add", "app.txt", cwd=root)
    monkeypatch.setattr(
        workspace, "_ensure_clean_merge_target", lambda target, files: None
    )

    with pytest.raises(WorkspaceError) as excinfo:
        workspace.apply_worktree_changes("task-merge-rollback", strategy="merge")

    assert "rolled back" in str(excinfo.value)
    content = (root / "app.txt").read_text(encoding="utf-8")
    assert "<<<<<<<" not in content
    assert content == "line1\nline2\nline3\n"
    status = _git("status", "--porcelain", cwd=root).stdout
    assert "UU" not in status


# ---------------------------------------------------------------------------
# inherit_diff filters upstream diffs and validates symlinks
# ---------------------------------------------------------------------------
SAFE_INHERITED_DIFF = """diff --git a/safe.txt b/safe.txt
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/safe.txt
@@ -0,0 +1 @@
+inherited safely
diff --git a/nested/.env b/nested/.env
new file mode 100644
index 0000000..2222222
--- /dev/null
+++ b/nested/.env
@@ -0,0 +1 @@
+SECRET=1
"""


def test_inherit_diff_drops_sensitive_blocks(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-inherit", sync_uncommitted=False)

    assert workspace.inherit_diff("task-inherit", SAFE_INHERITED_DIFF) is True
    assert (info.path / "safe.txt").read_text(encoding="utf-8") == "inherited safely\n"
    assert not (info.path / "nested" / ".env").exists()
    assert (info.path / ".harness-mesh-worktree.json").is_file()


ESCAPING_SYMLINK_DIFF = """diff --git "a/docs/\\351\\223\\276\\346\\216\\245" "b/docs/\\351\\223\\276\\346\\216\\245"
new file mode 120000
index 0000000..3333333
--- /dev/null
+++ "b/docs/\\351\\223\\276\\346\\216\\245"
@@ -0,0 +1 @@
+/tmp/outside-target
"""


def test_inherit_diff_rejects_escaping_symlink(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    workspace.create_worktree("task-inherit-link", sync_uncommitted=False)

    with pytest.raises(WorkspaceError, match="symlink"):
        workspace.inherit_diff("task-inherit-link", ESCAPING_SYMLINK_DIFF)

# ---------------------------------------------------------------------------
# checkpoint and router paths also validate before running git
# ---------------------------------------------------------------------------
def test_capture_checkpoint_validates_worktree_gitdir(tmp_path):
    from harness_mesh.models import TaskRequest

    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-checkpoint-gitdir", sync_uncommitted=False)
    fake = info.path / "fake-gitdir"
    (fake / "objects").mkdir(parents=True)
    (fake / "refs").mkdir(parents=True)
    (fake / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (fake / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    (info.path / ".git").write_text(f"gitdir: {fake}\n", encoding="utf-8")
    request = TaskRequest(task_id="task-checkpoint-gitdir", objective="checkpoint")

    with pytest.raises(WorkspaceError, match="gitdir"):
        workspace.capture_checkpoint(
            request, info.path, "claude_code", "claude-opus-9", "timeout"
        )


@pytest.mark.asyncio
async def test_fake_gitdir_never_executes_a_configured_filter_through_router(tmp_path):
    from harness_mesh.adapters.base import AdapterOutcome
    from harness_mesh.models import TaskRequest, TaskStatus
    from tests.helpers import FakeAdapter, make_mesh, write_claude_settings

    marker = tmp_path / "filter-ran"
    payload = tmp_path / "payload.sh"
    payload.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
    payload.chmod(0o755)

    def handler(adapter, call):
        workdir = Path(call.workdir)
        fake = workdir / "evilgit"
        (fake / "objects").mkdir(parents=True)
        (fake / "refs").mkdir(parents=True)
        (fake / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (fake / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n"
            f'[filter "evil"]\n\tclean = {payload}\n\trequired = true\n',
            encoding="utf-8",
        )
        (workdir / ".git").write_text(f"gitdir: {fake}\n", encoding="utf-8")
        (workdir / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
        (workdir / "app.txt").write_text("tampered\n", encoding="utf-8")
        return AdapterOutcome(exit_code=0, stdout="timed out", timed_out=True)

    mesh = make_mesh(tmp_path, adapters={"claude_code": FakeAdapter("claude_code", handler)})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        result = await mesh.router.dispatch(
            TaskRequest(
                task_id="task-router-gitdir",
                target_harness="claude_code",
                objective="tamper with .git",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status in {TaskStatus.FAILED, TaskStatus.TIMEOUT}
        assert not marker.exists(), "mesh executed a filter configured by worktree content"
    finally:
        mesh.storage.close()

def test_worktree_markers_are_never_part_of_changes(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-markers", sync_uncommitted=False)

    changes = workspace.collect_changes(info.path)
    assert changes.modified_files == []
    assert "harness-mesh-worktree.json" not in changes.diff
    assert ".harness-mesh-lineage.json" not in changes.diff
    tracked = _git("ls-files", cwd=info.path).stdout
    assert "harness-mesh-worktree.json" not in tracked
    assert ".harness-mesh-lineage.json" not in tracked


def test_subdirectory_of_a_tampered_worktree_is_still_validated(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-nested-validate", sync_uncommitted=False)
    (info.path / "src").mkdir()
    (info.path / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    fake = info.path / "fake-gitdir"
    (fake / "objects").mkdir(parents=True)
    (fake / "refs").mkdir(parents=True)
    (fake / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (fake / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    (info.path / ".git").write_text(f"gitdir: {fake}\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="gitdir"):
        workspace.collect_changes(info.path / "src")

def test_legacy_guard_format_is_upgraded_once(tmp_path):
    root = tmp_path / "project"
    init_git_repo(root, {"README.md": "base\n"})
    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-legacy-guard", sync_uncommitted=False)

    guard_path = workspace._git_guard_path("task-legacy-guard")
    payload = json.loads(guard_path.read_text(encoding="utf-8"))
    legacy_snapshot = {
        "config": payload["snapshot"]["config"],
        "attributes": payload["snapshot"]["attributes"],
        "hooks": payload["snapshot"]["hooks"],
    }
    guard_path.write_text(
        json.dumps({"task_id": "task-legacy-guard", "snapshot": legacy_snapshot}),
        encoding="utf-8",
    )

    (info.path / "new.txt").write_text("change\n", encoding="utf-8")
    applied = workspace.apply_worktree_changes("task-legacy-guard", strategy="patch")
    assert applied.applied_files == ["new.txt"]
    upgraded = json.loads(guard_path.read_text(encoding="utf-8"))["snapshot"]
    assert "worktree_git_file" in upgraded
