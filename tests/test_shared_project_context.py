"""Shared project context, dirty-worktree sync and dependency pipelines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harness_mesh.cli import app as cli_app
from harness_mesh.mcp_server import create_server
from harness_mesh.models import TaskRequest, TaskStatus
from harness_mesh.workspace import WorkspaceManager
from tests.helpers import (
    FakeAdapter,
    init_git_repo,
    make_mesh,
    success,
    write_claude_settings,
)


def test_uncommitted_workspace_changes_are_synced_to_worktree(tmp_path):
    root = tmp_path / "repo"
    init_git_repo(root, {"main.py": "base\n"})
    (root / "main.py").write_text("dirty primary edit\n", encoding="utf-8")
    (root / "dirty.txt").write_text("untracked primary file\n", encoding="utf-8")

    workspace = WorkspaceManager(root)
    info = workspace.create_worktree("task-dirty", sync_uncommitted=True)

    assert (info.path / "main.py").read_text(encoding="utf-8") == "dirty primary edit\n"
    assert (info.path / "dirty.txt").read_text(encoding="utf-8") == "untracked primary file\n"
    assert info.baseline_commit
    assert workspace.baseline_commit("task-dirty") == info.baseline_commit

    (info.path / "child.py").write_text("child work\n", encoding="utf-8")
    changes = workspace.collect_changes(info.path)
    assert changes.modified_files == ["child.py"]
    assert "child.py" in changes.diff
    assert "dirty primary edit" not in changes.diff
    assert "untracked primary file" not in changes.diff

    applied = workspace.apply_worktree_changes("task-dirty", strategy="patch")
    assert applied.applied_files == ["child.py"]
    assert (root / "child.py").read_text(encoding="utf-8") == "child work\n"
    # The primary's own dirty state is preserved, not double-applied.
    assert (root / "main.py").read_text(encoding="utf-8") == "dirty primary edit\n"
    assert (root / "dirty.txt").exists()

    clean_info = workspace.create_worktree("task-clean", sync_uncommitted=False)
    assert (clean_info.path / "main.py").read_text(encoding="utf-8") == "base\n"
    assert not (clean_info.path / "dirty.txt").exists()


@pytest.mark.asyncio
async def test_project_instructions_blackboard_and_previews_are_injected(tmp_path):
    captured = {}

    def handler(adapter, call):
        captured["prompt"] = call.prompt
        captured["session_id"] = call.session_id
        return success("context-aware work completed")

    mesh = make_mesh(tmp_path, adapters={"claude_code": FakeAdapter("claude_code", handler)})
    try:
        write_claude_settings(mesh.home_dir, model="demo-opus-5-5")
        (mesh.workspace_root / "AGENTS.md").write_text("AGENTS: keep tools deterministic.\n", encoding="utf-8")
        (mesh.workspace_root / "CLAUDE.md").write_text("CLAUDE: prefer small patches.\n", encoding="utf-8")
        (mesh.workspace_root / "src").mkdir()
        (mesh.workspace_root / "src" / "sample.py").write_text(
            "def sample() -> int:\n    return 42\n", encoding="utf-8"
        )
        mesh.router.update_project_context(
            "api.contract",
            "TaskResult.status is the public contract.",
            category="api_contract",
            author_harness="codex",
            source="controller",
        )
        request = TaskRequest(
            task_id="task-context",
            target_harness="claude_code",
            objective="Implement a small change using the shared context.",
            context_files=["src/sample.py"],
            include_project_context=True,
            auto_record_context=True,
            session_id="session-ctx-1",
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        result = await mesh.router.dispatch(request)

        prompt = captured["prompt"]
        assert result.status == TaskStatus.COMPLETED
        assert captured["session_id"] == "session-ctx-1"
        assert "<project_shared_instructions>" in prompt
        assert "AGENTS: keep tools deterministic." in prompt
        assert "CLAUDE: prefer small patches." in prompt
        assert "<project_shared_memory>" in prompt
        assert "api.contract" in prompt
        assert "TaskResult.status is the public contract." in prompt
        assert "<context_file_previews>" in prompt
        assert "def sample() -> int:" in prompt

        entries = mesh.router.get_project_context()["entries"]
        assert any(entry["key"] == "task.task-context.summary" for entry in entries)
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_depends_on_preapplies_upstream_diff_and_injects_outputs(tmp_path):
    captured = {}

    def handler(adapter, call):
        objective = call.task.objective if call.task else ""
        if objective.startswith("UPSTREAM"):
            (call.workdir / "upstream.py").write_text("UPSTREAM = True\n", encoding="utf-8")
            return success("upstream task completed")
        assert (call.workdir / "upstream.py").exists(), "downstream worktree must inherit upstream diff"
        captured["prompt"] = call.prompt
        (call.workdir / "downstream.py").write_text("DOWNSTREAM = True\n", encoding="utf-8")
        return success("downstream task completed")

    mesh = make_mesh(tmp_path, adapters={"claude_code": FakeAdapter("claude_code", handler)})
    try:
        write_claude_settings(mesh.home_dir, model="demo-opus-5-5")
        upstream = await mesh.router.dispatch(
            TaskRequest(
                task_id="up-task",
                target_harness="claude_code",
                objective="UPSTREAM: create upstream.py",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert upstream.status == TaskStatus.COMPLETED
        assert not (mesh.workspace_root / "upstream.py").exists()

        downstream = await mesh.router.dispatch(
            TaskRequest(
                task_id="down-task",
                target_harness="claude_code",
                objective="DOWNSTREAM: build on the upstream output",
                depends_on=["up-task"],
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )

        assert downstream.status == TaskStatus.COMPLETED
        assert "<upstream_task_outputs>" in captured["prompt"]
        assert "up-task" in captured["prompt"]
        assert "preapplied_to_worktree: True" in captured["prompt"]
        assert "UPSTREAM = True" in captured["prompt"]
        assert downstream.modified_files == ["downstream.py"]
        assert "upstream.py" not in downstream.git_diff
        assert (mesh.workspace_root / "downstream.py").exists() is False

        record = mesh.storage.get_task("down-task")
        assert record["checkpoint"] is None
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_mcp_project_context_tools(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        server = create_server(mesh.router)
        update_result = await server.call_tool(
            "update_project_context",
            {
                "key": "mesh.routing",
                "content": "Failover prefers same harness before crossing.",
                "category": "decision",
                "author_harness": "codex",
            },
        )
        update_blocks = update_result[0] if isinstance(update_result, tuple) else update_result
        update_payload = json.loads(update_blocks[0].text)
        assert update_payload["key"] == "mesh.routing"

        read_result = await server.call_tool(
            "get_project_context", {"category": "decision", "include_instructions": False}
        )
        read_blocks = read_result[0] if isinstance(read_result, tuple) else read_result
        read_payload = json.loads(read_blocks[0].text)
        assert any(entry["key"] == "mesh.routing" for entry in read_payload["entries"])
    finally:
        mesh.storage.close()


def test_cli_context_set_and_list(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    init_git_repo(workspace, {"README.md": "context CLI fixture\n"})
    config_path = Path(__file__).resolve().parents[1] / "config" / "harnesses.example.yaml"
    env = {
        "HOME": str(home),
        "DSH_HOME": str(home / ".dsh"),
        "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1",
    }
    runner = CliRunner()
    base = ["--workspace", str(workspace), "--config", str(config_path)]
    set_result = runner.invoke(
        cli_app,
        [*base, "context", "set", "arch.overview", "Federated Harness Mesh", "--category", "architecture"],
        env=env,
    )
    assert set_result.exit_code == 0, set_result.output
    list_result = runner.invoke(cli_app, [*base, "context", "list"], env=env)
    assert list_result.exit_code == 0, list_result.output
    assert "arch.overview" in list_result.output
    assert "Federated Harness Mesh" in list_result.output
