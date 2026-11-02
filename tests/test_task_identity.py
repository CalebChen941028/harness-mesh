"""Ticket identity, cross-process conflicts, model output and redaction.

Covers B3 (``task_id`` validation + ledger conflict rejection), B7
(``DiscoveredModel.public_view`` in discovery payloads), B8 (transcript/diff
redaction and transcript cap), B9 (context provenance default) and the shared
lineage caller helpers.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness_mesh.lineage import caller_is_worker, caller_task_id
from harness_mesh.models import AssignmentRecord, DiscoveredModel, TaskRequest, TaskStatus
from harness_mesh.router import MeshRouter
from tests.helpers import FakeAdapter, make_mesh, success, write_claude_settings

SECRET = "sk-" + "a" * 32


# ---------------------------------------------------------------------------
# TaskRequest.task_id validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("task_id", ["task-1", "Task_1.a", "t", "a" * 64, "x.y-z_9"])
def test_task_request_accepts_wellformed_ids(task_id):
    request = TaskRequest(task_id=task_id, objective="ok")
    assert request.task_id == task_id


@pytest.mark.parametrize(
    "task_id",
    [".", "..", ".meta", "a..b", "a.", "-dash", "a/b", "a b", "", "a" * 65, "worktrees", "tmp"],
)
def test_task_request_rejects_unsafe_ids(task_id):
    with pytest.raises(ValidationError):
        TaskRequest(task_id=task_id, objective="bad")


@pytest.mark.asyncio
async def test_running_task_id_is_rejected_without_mutating_the_ticket(tmp_path):
    calls: list[str] = []

    def handler(adapter, call):
        calls.append(call.assignment_id)
        return success("should not run")

    mesh = make_mesh(tmp_path, adapters={"claude_code": FakeAdapter("claude_code", handler)})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        original = TaskRequest(
            task_id="held-task",
            target_harness="claude_code",
            objective="original objective",
            workspace_root=str(mesh.workspace_root),
        )
        mesh.storage.create_task(original, status=TaskStatus.RUNNING)
        mesh.storage.add_assignment(
            AssignmentRecord(
                task_id="held-task",
                attempt=1,
                harness="claude_code",
                model="claude-opus-9",
                status="running",
                pid=os.getpid(),
            )
        )

        intruder = await mesh.router.dispatch(
            TaskRequest(
                task_id="held-task",
                target_harness="claude_code",
                objective="attacker objective",
                workspace_root=str(mesh.workspace_root),
            )
        )

        assert intruder.status == TaskStatus.REJECTED
        assert (intruder.error_detail or "").startswith("task_conflict:held-task")
        assert calls == []
        record = mesh.storage.get_task("held-task")
        assert record["status"] == TaskStatus.RUNNING.value
        assert record["request"].objective == "original objective"
        assignments = mesh.storage.list_assignments("held-task")
        assert [item.status for item in assignments] == ["running"]
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_second_router_process_cannot_take_over_a_live_ticket(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def victim_handler(adapter, call):
        started.set()
        await release.wait()
        return success("victim completed")

    mesh = make_mesh(tmp_path, adapters={"claude_code": FakeAdapter("claude_code", victim_handler)})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        victim_request = TaskRequest(
            task_id="victim-ticket",
            target_harness="claude_code",
            objective="victim objective",
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        victim = asyncio.create_task(mesh.router.dispatch(victim_request))
        await asyncio.wait_for(started.wait(), timeout=10)
        victim_worktree = mesh.storage.get_task("victim-ticket")["worktree_path"]

        # A second MCP server process shares the SQLite ledger but has its own
        # in-memory duplicate table.
        router_two = MeshRouter(
            mesh.config,
            mesh.storage,
            discovery=mesh.discovery,
            skills=mesh.skills,
            workspace=mesh.workspace,
            adapters={"claude_code": FakeAdapter("claude_code", lambda a, c: success("intruder"))},
            guardian=mesh.guardian,
            workspace_root=mesh.workspace_root,
            env=mesh.env,
            home_dir=mesh.home_dir,
        )
        takeover = await asyncio.wait_for(
            router_two.dispatch(
                TaskRequest(
                    task_id="victim-ticket",
                    target_harness="claude_code",
                    objective="attacker objective",
                    workspace_root=str(mesh.workspace_root),
                    timeout_seconds=30,
                )
            ),
            timeout=30,
        )

        assert takeover.status == TaskStatus.REJECTED
        assert (takeover.error_detail or "").startswith("task_conflict:victim-ticket")
        assert takeover.worktree_path is None
        record = mesh.storage.get_task("victim-ticket")
        assert record["worktree_path"] == victim_worktree
        assert record["request"].objective == "victim objective"
        running = [item for item in mesh.storage.list_assignments("victim-ticket") if item.status == "running"]
        assert len(running) == 1

        release.set()
        result = await asyncio.wait_for(victim, timeout=30)
        assert result.status == TaskStatus.COMPLETED
    finally:
        release.set()
        mesh.storage.close()


# ---------------------------------------------------------------------------
# DiscoveredModel.public_view
# ---------------------------------------------------------------------------
def test_public_view_whitelists_fields_and_sanitises_provider():
    model = DiscoveredModel(
        harness="codex",
        model_id="gpt-5-codex",
        provider='{"api_key": "sk-leaked"}',
        source="config",
        authorized=True,
        capability_score=42.0,
        assigned_tiers=["high"],
        discovered_at=123.0,
    )
    view = model.public_view()

    assert view["provider"] == "codex"
    assert view["model_id"] == "gpt-5-codex"
    assert view["assigned_tiers"] == ["high"]
    assert "discovered_at" not in view
    assert "harness" not in view
    assert set(view) == {
        "model_id",
        "provider",
        "authorized",
        "capability_score",
        "intelligence",
        "intelligence_source",
        "blended_price",
        "assigned_tiers",
        "source",
        "context_window",
        "supports_reasoning",
        "is_active_default",
    }


def test_public_view_keeps_a_short_enum_provider():
    model = DiscoveredModel(
        harness="codex", model_id="m", provider="openai.us:prod/v2", source="config"
    )
    assert model.public_view()["provider"] == "openai.us:prod/v2"


@pytest.mark.asyncio
async def test_discover_payload_uses_public_view(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path, adapters={"claude_code": FakeAdapter("claude_code", lambda a, c: success("x"))})
    try:
        suspicious = DiscoveredModel(
            harness="claude_code",
            model_id="leaky-model",
            provider='{"api_key": "sk-leaked"}',
            source="config",
        )

        async def fake_discover(harness, **kwargs):
            return [suspicious]

        monkeypatch.setattr(mesh.discovery, "discover_for_harness", fake_discover)
        payload = await mesh.router.discover_harness_models("claude_code")
        model = payload["models"][0]
        assert model["provider"] == "claude_code"
        assert "sk-leaked" not in str(payload)
        assert "discovered_at" not in model
    finally:
        mesh.storage.close()


# ---------------------------------------------------------------------------
# transcript/diff redaction and bounded transcript resources
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_transcript_and_diff_are_redacted_before_storage(tmp_path):
    def handler(adapter, call):
        workdir = Path(call.workdir)
        (workdir / "leak.txt").write_text(f"TOKEN={SECRET}\n", encoding="utf-8")
        return success(f"stdout mentions {SECRET}")

    mesh = make_mesh(tmp_path, adapters={"claude_code": FakeAdapter("claude_code", handler)})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        result = await mesh.router.dispatch(
            TaskRequest(
                task_id="task-redact",
                target_harness="claude_code",
                objective="leak a secret",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED

        transcript = mesh.storage.get_resource("task-redact", "transcript") or ""
        diff = mesh.storage.get_resource("task-redact", "diff") or ""
        assert SECRET not in transcript
        assert SECRET not in diff
        assert SECRET not in result.raw_stdout
        assert SECRET not in result.git_diff
        assert "***" in transcript
        assert "***" in diff
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_transcript_resource_is_capped_with_head_and_tail(tmp_path):
    def handler(adapter, call):
        return success("BEGINHEAD" + "x" * 20_000 + "ENDTAIL")

    mesh = make_mesh(tmp_path, adapters={"claude_code": FakeAdapter("claude_code", handler)})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        mesh.config.transcript_max_chars = 4_000
        await mesh.router.dispatch(
            TaskRequest(
                task_id="task-transcript-cap",
                target_harness="claude_code",
                objective="large output",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        transcript = mesh.storage.get_resource("task-transcript-cap", "transcript") or ""
        assert 0 < len(transcript) <= 4_000
        assert "[... transcript truncated ...]" in transcript
        assert "BEGINHEAD" in transcript
        assert "ENDTAIL" in transcript
    finally:
        mesh.storage.close()


# ---------------------------------------------------------------------------
# project context provenance defaults to "auto"
# ---------------------------------------------------------------------------
def test_update_project_context_defaults_to_auto_source(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        mesh.router.update_project_context("wsb.default", "value")
        entries = mesh.router.get_project_context()["entries"]
        entry = next(item for item in entries if item["key"] == "wsb.default")
        assert entry["source"] == "auto"
    finally:
        mesh.storage.close()


# ---------------------------------------------------------------------------
# lineage caller helpers (MCP write gates depend on them)
# ---------------------------------------------------------------------------
def test_caller_identity_is_none_for_a_controller(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    try:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("HARNESS_MESH_TASK_ID", raising=False)
        assert caller_task_id(mesh.router) is None
        assert caller_is_worker(mesh.router) is False
    finally:
        mesh.storage.close()


def test_caller_identity_prefers_the_inherited_task_id(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    try:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HARNESS_MESH_TASK_ID", "worker-task")
        assert caller_task_id(mesh.router) == "worker-task"
        assert caller_is_worker(mesh.router) is True
    finally:
        mesh.storage.close()


def test_caller_identity_from_a_running_worktree(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    try:
        monkeypatch.delenv("HARNESS_MESH_TASK_ID", raising=False)
        info = mesh.workspace.create_worktree("worker-worktree", sync_uncommitted=False)
        mesh.storage.add_assignment(
            AssignmentRecord(
                task_id="worker-worktree",
                attempt=1,
                harness="claude_code",
                model="claude-opus-9",
                status="running",
                pid=os.getpid(),
            )
        )
        monkeypatch.chdir(info.path)
        assert caller_task_id(mesh.router) == "worker-worktree"
        assert caller_is_worker(mesh.router) is True
    finally:
        mesh.storage.close()
