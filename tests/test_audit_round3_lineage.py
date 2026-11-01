"""Round 3 WP-R9: lineage must survive hosts that drop the mesh environment.

Codex 0.166 starts MCP servers with only the environment declared in the MCP
config, so ``HARNESS_MESH_CALL_DEPTH`` and ``HARNESS_MESH_TRACE_CHAIN`` never
reach the child.  The fallback therefore has to recover the exact values the
router handed to the adapter (request depth + 1, current harness appended) from
the assignment ledger, the process ancestry or the worktree cwd.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from harness_mesh.adapters.base import AdapterCall
from harness_mesh.adapters.claude_code import ClaudeCodeAdapter
from harness_mesh.config import default_config
from harness_mesh.mcp_server import _inherited_lineage, create_server
from harness_mesh.models import AssignmentRecord, TaskRequest, TaskStatus
from tests.helpers import FakeAdapter, make_mesh, success, write_claude_settings, write_codex_config

MAX_DEPTH = 3
REPO_ROOT = Path(__file__).resolve().parents[1]


def _drop_mesh_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("HARNESS_MESH_"):
            monkeypatch.delenv(key, raising=False)


async def _call_dispatch(router, **kwargs) -> dict:
    server = create_server(router)
    result = await server.call_tool("dispatch_task", kwargs)
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def _running_assignment(
    mesh,
    task_id: str,
    *,
    depth: int,
    trace: list[str],
    pid: int | None,
    attempt: int = 1,
) -> AssignmentRecord:
    record = AssignmentRecord(
        task_id=task_id,
        attempt=attempt,
        harness=trace[-1] if trace else "codex",
        model="fixture-model",
        status="running",
        call_depth=depth,
        trace_chain=list(trace),
        pid=pid,
    )
    mesh.storage.add_assignment(record)
    return record


@pytest.mark.asyncio
async def test_codex_cwd_only_recursion_stops_at_the_first_cycle(tmp_path, monkeypatch):
    executions: list[tuple[int, list[str]]] = []
    children: list[dict] = []
    holder: dict = {}
    origin = os.getcwd()

    async def handler(adapter, call):
        executions.append((call.call_depth, list(call.trace_chain)))
        assert len(executions) <= MAX_DEPTH, "cwd fallback let the recursion run away"
        _drop_mesh_env(monkeypatch)
        monkeypatch.chdir(call.workdir)
        try:
            payload = await _call_dispatch(
                holder["mesh"].router, target_harness="codex", objective="probe child"
            )
        finally:
            monkeypatch.chdir(origin)
        children.append(payload)
        return success("done")

    mesh = make_mesh(
        tmp_path,
        adapters={"codex": FakeAdapter("codex", handler)},
        config_updates={"max_call_depth": MAX_DEPTH, "max_active_assignments": 50},
    )
    holder["mesh"] = mesh
    write_codex_config(mesh.home_dir, model="gpt-5-codex")
    try:
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="codex",
                objective="top level task",
                workspace_root=str(mesh.workspace_root),
            )
        )
    finally:
        mesh.storage.close()

    assert result.status == TaskStatus.COMPLETED
    assert executions == [(1, ["codex"])]
    assert len(children) == 1
    assert children[0]["status"] == TaskStatus.REJECTED.value
    assert children[0]["error_detail"] == "recursive harness cycle rejected: codex -> codex"


@pytest.mark.asyncio
async def test_assignment_ledger_records_adapter_lineage_and_process_pid(tmp_path):
    seen: dict = {}

    async def handler(adapter, call):
        seen["depth"] = call.call_depth
        seen["trace"] = list(call.trace_chain)
        if call.on_process_start is not None:
            call.on_process_start(os.getpid())
        return success("ok")

    mesh = make_mesh(tmp_path, adapters={"codex": FakeAdapter("codex", handler)})
    write_codex_config(mesh.home_dir, model="gpt-5-codex")
    try:
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="codex",
                objective="lineage ledger",
                workspace_root=str(mesh.workspace_root),
                call_depth=1,
                trace_chain=["claude_code"],
            )
        )
        record = mesh.storage.list_assignments(result.task_id)[-1]
    finally:
        mesh.storage.close()

    assert result.status == TaskStatus.COMPLETED
    assert seen == {"depth": 2, "trace": ["claude_code", "codex"]}
    assert record.call_depth == 2
    assert record.trace_chain == ["claude_code", "codex"]
    assert record.pid == os.getpid()


@pytest.mark.asyncio
async def test_cli_adapter_reports_the_spawned_process_pid(tmp_path):
    config = default_config()
    adapter = ClaudeCodeAdapter(
        config,
        config.harnesses["claude_code"],
        tmp_path,
        env={"HOME": str(tmp_path)},
        which=lambda name: "/bin/echo",
    )
    pids: list[int] = []
    call = AdapterCall(
        task_id="pid-task",
        assignment_id="pid-assignment",
        prompt="hello",
        workdir=tmp_path,
        model="test-model",
        timeout_seconds=10,
        on_process_start=pids.append,
    )
    outcome = await adapter.execute(call)

    assert outcome.exit_code == 0, outcome.stderr
    assert len(pids) == 1
    assert pids[0] > 0


def test_worktree_path_wins_over_an_agent_writable_lineage_marker(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path, adapters={"codex": FakeAdapter("codex", lambda a, c: success("ok"))})
    try:
        for task_id in ("task-a", "task-b"):
            request = TaskRequest(
                task_id=task_id,
                target_harness="codex",
                objective=task_id,
                workspace_root=str(mesh.workspace_root),
            )
            mesh.storage.create_task(request, status=TaskStatus.RUNNING)
        info = mesh.workspace.create_worktree("task-a", sync_uncommitted=False)
        _running_assignment(mesh, "task-a", depth=1, trace=["codex"], pid=999_999)
        _running_assignment(mesh, "task-b", depth=3, trace=["claude_code"], pid=999_998)

        marker = info.path / ".harness-mesh-lineage.json"
        marker.chmod(0o644)
        marker.write_text(json.dumps({"task_id": "task-b"}), encoding="utf-8")
        monkeypatch.chdir(info.path)

        assert _inherited_lineage(mesh.router) == (1, ["codex"])
    finally:
        mesh.storage.close()


def test_lineage_marker_alone_cannot_define_ancestry(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path, adapters={"codex": FakeAdapter("codex", lambda a, c: success("ok"))})
    try:
        request = TaskRequest(
            task_id="forged-task",
            target_harness="codex",
            objective="forged",
            workspace_root=str(mesh.workspace_root),
            call_depth=3,
            trace_chain=["claude_code"],
        )
        mesh.storage.create_task(request, status=TaskStatus.RUNNING)
        _running_assignment(mesh, "forged-task", depth=3, trace=["claude_code"], pid=999_997)
        marker = mesh.workspace_root / ".harness-mesh-lineage.json"
        marker.write_text(json.dumps({"task_id": "forged-task"}), encoding="utf-8")
        monkeypatch.chdir(mesh.workspace_root)

        assert _inherited_lineage(mesh.router) == (0, [])
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_stdio_server_derives_the_same_lineage_as_the_environment(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path, adapters={"codex": FakeAdapter("codex", lambda a, c: success("ok"))})
    try:
        # The env path runs with no worktree cwd and no assignment: only the
        # inherited environment can supply the lineage.
        monkeypatch.chdir(tmp_path)
        _drop_mesh_env(monkeypatch)
        monkeypatch.setenv("HARNESS_MESH_CALL_DEPTH", "1")
        monkeypatch.setenv("HARNESS_MESH_TRACE_CHAIN", json.dumps(["codex"]))
        env_cycle = await _call_dispatch(
            mesh.router, target_harness="codex", objective="env cycle probe"
        )
        monkeypatch.setenv("HARNESS_MESH_CALL_DEPTH", str(MAX_DEPTH))
        monkeypatch.setenv("HARNESS_MESH_TRACE_CHAIN", json.dumps(["claude_code"]))
        env_depth = await _call_dispatch(
            mesh.router, target_harness="codex", objective="env depth probe"
        )

        parent = TaskRequest(
            task_id="stdio-parent",
            target_harness="codex",
            objective="stdio parent",
            workspace_root=str(mesh.workspace_root),
        )
        mesh.storage.create_task(parent, status=TaskStatus.RUNNING)
        info = mesh.workspace.create_worktree("stdio-parent", sync_uncommitted=False)
        # A live sibling pid keeps the row valid (stale reaping must not close
        # it) while staying outside this MCP server's ancestor chain, so only
        # the worktree cwd can supply the lineage.
        sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            assignment = _running_assignment(
                mesh, "stdio-parent", depth=1, trace=["codex"], pid=sibling.pid
            )
        except Exception:
            sibling.terminate()
            raise

        env = {
            key: value
            for key, value in os.environ.items()
            # Keep the suite's offline switch; the stdio server cannot inherit
            # the in-process socket guard, and lineage variables stay dropped.
            if not key.startswith("HARNESS_MESH_") or key == "HARNESS_MESH_OFFLINE"
        }
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        env["HOME"] = str(mesh.home_dir)
        # Belt and braces: even a regression in lineage resolution must fail
        # with "no_available_model" instead of launching a real harness.
        disabled = tmp_path / "disabled-harnesses.yaml"
        disabled.write_text(
            "max_call_depth: 3\nharnesses:\n"
            + "".join(
                f"  {name}:\n    enabled: false\n"
                for name in ("claude_code", "codex", "deepseek", "antigravity", "zcode")
            ),
            encoding="utf-8",
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "harness_mesh.mcp_server", "--no-monitor", "--config", str(disabled)],
            cwd=str(info.path),
            env=env,
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    first = await session.call_tool(
                        "dispatch_task", {"target_harness": "codex", "objective": "cwd cycle probe"}
                    )
                    payload_cycle = json.loads(first.content[0].text)
                    mesh.storage.update_assignment(
                        assignment.assignment_id,
                        call_depth=MAX_DEPTH,
                        trace_chain=json.dumps(["claude_code"]),
                    )
                    second = await session.call_tool(
                        "dispatch_task", {"target_harness": "codex", "objective": "cwd depth probe"}
                    )
                    payload_depth = json.loads(second.content[0].text)
        finally:
            sibling.terminate()
            sibling.wait(timeout=5)
    finally:
        mesh.storage.close()

    assert payload_cycle["status"] == TaskStatus.REJECTED.value
    assert payload_cycle["error_detail"] == env_cycle["error_detail"]
    assert "codex -> codex" in payload_cycle["error_detail"]
    assert payload_depth["status"] == TaskStatus.REJECTED.value
    assert payload_depth["error_detail"] == env_depth["error_detail"]
    assert f"call_depth={MAX_DEPTH}" in payload_depth["error_detail"]


@pytest.mark.asyncio
async def test_end_to_end_recursion_without_hand_built_lineage(tmp_path, monkeypatch):
    executions: list[tuple[str, int, list[str]]] = []
    rejected: list[dict] = []
    origin = os.getcwd()

    def _enter(call):
        _drop_mesh_env(monkeypatch)
        monkeypatch.chdir(call.workdir)

    async def codex_handler(adapter, call):
        executions.append(("codex", call.call_depth, list(call.trace_chain)))
        _enter(call)
        try:
            payload = await _call_dispatch(
                mesh.router, target_harness="claude_code", objective="hand off to claude"
            )
        finally:
            monkeypatch.chdir(origin)
        assert payload["status"] == TaskStatus.COMPLETED.value, payload["error_detail"]
        return success("codex done")

    async def claude_handler(adapter, call):
        executions.append(("claude_code", call.call_depth, list(call.trace_chain)))
        _enter(call)
        try:
            payload = await _call_dispatch(
                mesh.router, target_harness="codex", objective="cycle back to codex"
            )
        finally:
            monkeypatch.chdir(origin)
        rejected.append(payload)
        return success("claude done")

    mesh = make_mesh(
        tmp_path,
        adapters={
            "codex": FakeAdapter("codex", codex_handler),
            "claude_code": FakeAdapter("claude_code", claude_handler),
        },
        config_updates={"max_call_depth": MAX_DEPTH},
    )
    write_codex_config(mesh.home_dir, model="gpt-5-codex")
    write_claude_settings(mesh.home_dir, model="claude-opus-9")
    try:
        top = await mesh.router.dispatch(
            TaskRequest(
                target_harness="codex",
                objective="root task",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
    finally:
        mesh.storage.close()

    assert top.status == TaskStatus.COMPLETED, top.error_detail
    assert executions == [
        ("codex", 1, ["codex"]),
        ("claude_code", 2, ["codex", "claude_code"]),
    ]
    assert len(rejected) == 1
    assert rejected[0]["status"] == TaskStatus.REJECTED.value
    assert rejected[0]["error_detail"] == "recursive harness cycle rejected: codex -> claude_code -> codex"
