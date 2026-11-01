"""Workspace root allowlist, startup guard and sensitive previews."""

from __future__ import annotations

import pytest

from harness_mesh.config import default_config
from harness_mesh.models import TaskRequest, TaskStatus
from harness_mesh.router import create_mesh_runtime
from harness_mesh.security import (
    SENSITIVE_FILE_PATTERNS,
    WorkspaceRootError,
    is_sensitive_name,
    resolve_allowed_root,
)
from harness_mesh.workspace import WorkspaceError
from tests.helpers import FakeAdapter, init_git_repo, make_mesh, success, write_claude_settings


@pytest.mark.asyncio
async def test_workspace_root_allowlist_rejects_escape_paths_and_symlinks(tmp_path):
    mesh = make_mesh(tmp_path, adapters={"claude_code": FakeAdapter("claude_code", lambda a, c: success("ok"))})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        outside = tmp_path / "outside"
        outside.mkdir()
        symlink = mesh.workspace_root / "escape"
        symlink.symlink_to(outside, target_is_directory=True)

        for unsafe in ("/", "~", str(mesh.workspace_root / ".."), str(symlink)):
            result = await mesh.router.dispatch(
                TaskRequest(
                    target_harness="claude_code",
                    objective="must be rejected",
                    workspace_root=unsafe,
                    timeout_seconds=30,
                )
            )
            assert result.status == TaskStatus.REJECTED, unsafe
            assert "allowed_workspace_roots" in (result.error_detail or "")

        child = mesh.workspace_root / "subproject"
        child.mkdir()
        init_git_repo(child, {"README.md": "child\\n"})
        allowed = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="child workspace is allowed",
                workspace_root=str(child),
                timeout_seconds=30,
            )
        )
        assert allowed.status == TaskStatus.COMPLETED
        assert (child / ".harness-mesh").exists()
    finally:
        mesh.storage.close()


def test_resolve_allowed_root_and_apply_boundary(tmp_path):
    allowed = tmp_path / "project"
    child = allowed / "child"
    outside = tmp_path / "outside"
    for path in (allowed, child, outside):
        path.mkdir()
    assert resolve_allowed_root(child, [allowed]) == child.resolve()
    with pytest.raises(WorkspaceRootError):
        resolve_allowed_root(outside, [allowed])

    symlink = allowed / "to-outside"
    symlink.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceRootError):
        resolve_allowed_root(symlink, [allowed])

    mesh = make_mesh(tmp_path / "mesh", adapters={})
    try:
        mesh.storage.create_task(
            TaskRequest(
                task_id="outside-apply",
                target_harness="claude_code",
                objective="outside",
                workspace_root=str(outside),
            )
        )
        with pytest.raises(WorkspaceError):
            mesh.router.apply_task_patch("outside-apply", strategy="patch")
    finally:
        mesh.storage.close()


def test_sensitive_preview_files_are_skipped(tmp_path):
    mesh = make_mesh(tmp_path, adapters={})
    try:
        (mesh.workspace_root / "public.txt").write_text("public data\n", encoding="utf-8")
        (mesh.workspace_root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (mesh.workspace_root / "auth.key").write_text("key data\n", encoding="utf-8")
        previews = mesh.router.context.render_context_file_previews(
            ["public.txt", ".env", "auth.key"], mesh.workspace_root
        )
        assert "public data" in previews
        assert "SECRET=1" not in previews
        assert "key data" not in previews
        for pattern in (".env", ".env.local", "server.pem", "id_rsa", "credentials.json", ".npmrc", "app.keystore"):
            assert is_sensitive_name(pattern), pattern
    finally:
        mesh.storage.close()
    assert SENSITIVE_FILE_PATTERNS


def test_service_startup_requires_a_git_workspace(tmp_path):
    with pytest.raises(RuntimeError, match="requires a Git workspace"):
        create_mesh_runtime(config=default_config(), workspace_root=tmp_path / "not-a-repo")

    workspace = tmp_path / "repo"
    init_git_repo(workspace, {"README.md": "startup\\n"})
    runtime = create_mesh_runtime(config=default_config(), workspace_root=workspace)
    try:
        assert str(workspace.resolve()) in runtime.config.allowed_workspace_roots
    finally:
        runtime.storage.close()
