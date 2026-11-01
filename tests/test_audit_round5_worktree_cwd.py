"""Round 5 WP-R15: the CLI must recover the primary workspace from a worktree.

A worker shell normally runs with its cwd set to
``<project>/.harness-mesh/worktrees/<task_id>``.  If the CLI treats that
directory as a project it opens a second, empty ``state.db`` there, so the
assignment ledger has no record of the parent and the recursion guard sees
depth zero.  The CLI must resolve the cwd back to the project root exactly like
the MCP server does, and the service startup check must refuse a worktree path
even when it is passed explicitly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness_mesh.models import AssignmentRecord, TaskRequest
from harness_mesh.security import ensure_service_workspace
from tests.helpers import init_git_repo, make_mesh

REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_DEPTH = 3
HARNESSES = ("claude_code", "codex", "deepseek", "antigravity", "zcode")


def _write_config(tmp_path: Path, *, codex_enabled: bool = False) -> Path:
    path = tmp_path / "round5-harnesses.yaml"
    codex = (
        "  codex:\n"
        "    enabled: true\n"
        "    binary: hmesh-test-missing-codex\n"
        "    binary_paths: []\n"
        if codex_enabled
        else "  codex:\n    enabled: false\n"
    )
    others = "".join(f"  {name}:\n    enabled: false\n" for name in HARNESSES if name != "codex")
    path.write_text(
        f"max_call_depth: {MAX_DEPTH}\nauto_init_repo: true\nharnesses:\n{codex}{others}",
        encoding="utf-8",
    )
    return path


def _mesh_with_git_repo(tmp_path: Path):
    workspace = tmp_path / "workspace"
    init_git_repo(workspace, {"README.md": "round 5 worktree cwd\n"})
    return make_mesh(tmp_path)


def _worktree_path(mesh) -> Path:
    worktree = mesh.workspace_root / ".harness-mesh" / "worktrees" / "parent"
    worktree.mkdir(parents=True, exist_ok=True)
    return worktree


def _add_running_assignment(mesh, task_id: str = "worktree-parent") -> AssignmentRecord:
    record = AssignmentRecord(
        task_id=task_id,
        attempt=1,
        harness="codex",
        model="gpt-5-codex",
        status="running",
        call_depth=MAX_DEPTH,
        trace_chain=["codex"],
        pid=os.getpid(),
    )
    return mesh.storage.add_assignment(record)


def _env(mesh) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        # Keep the suite's offline switch; children cannot inherit the socket
        # guard, and every other HARNESS_MESH_* variable is dropped on purpose.
        if not key.startswith("HARNESS_MESH_") or key == "HARNESS_MESH_OFFLINE"
    }
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["HOME"] = str(mesh.home_dir)
    env["HARNESS_MESH_DISABLE_SYSTEM_BINARIES"] = "1"
    return env


def _run_cli(mesh, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "harness_mesh.cli", *args],
        cwd=str(cwd),
        env=_env(mesh),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _run_dispatch(
    mesh, cwd: Path, config_path: Path, *extra: str
) -> tuple[subprocess.CompletedProcess[str], dict]:
    completed = _run_cli(
        mesh,
        cwd,
        "--config",
        str(config_path),
        "dispatch",
        "--objective",
        "child dispatch from a worker worktree",
        "--harness",
        "codex",
        *extra,
    )
    assert completed.returncode == 0, completed.stderr
    return completed, json.loads(completed.stdout)


def test_cli_dispatch_from_a_worktree_uses_the_primary_ledger(tmp_path):
    mesh = _mesh_with_git_repo(tmp_path)
    worktree = _worktree_path(mesh)
    config_path = _write_config(tmp_path)
    try:
        _add_running_assignment(mesh)
        completed, payload = _run_dispatch(mesh, worktree, config_path)
    finally:
        mesh.storage.close()

    assert "Traceback" not in completed.stderr
    assert payload["status"] == "rejected", completed.stdout
    assert f"call_depth={MAX_DEPTH}" in payload["error_detail"]
    assert "max_call_depth" in payload["error_detail"]
    assert not (worktree / ".harness-mesh" / "state.db").exists()


def test_cli_dispatch_from_a_worktree_rejects_a_lowered_call_depth(tmp_path):
    mesh = _mesh_with_git_repo(tmp_path)
    worktree = _worktree_path(mesh)
    config_path = _write_config(tmp_path)
    try:
        _add_running_assignment(mesh)
        completed, payload = _run_dispatch(
            mesh, worktree, config_path, "--call-depth", "0", "--trace-chain", '["zcode"]'
        )
        request = mesh.storage.get_task(payload["task_id"])["request"]
    finally:
        mesh.storage.close()

    assert "Traceback" not in completed.stderr
    assert payload["status"] == "rejected", completed.stdout
    assert "max_call_depth" in payload["error_detail"]
    assert request is not None
    assert request.call_depth == MAX_DEPTH
    assert request.trace_chain == ["codex", "zcode"]
    assert not (worktree / ".harness-mesh" / "state.db").exists()


def test_cli_dispatch_rejects_an_explicit_worktree_workspace(tmp_path):
    mesh = _mesh_with_git_repo(tmp_path)
    worktree = _worktree_path(mesh)
    config_path = _write_config(tmp_path)
    try:
        completed = _run_cli(
            mesh,
            mesh.workspace_root,
            "--workspace",
            str(worktree),
            "--config",
            str(config_path),
            "dispatch",
            "--objective",
            "explicit worktree workspace",
            "--harness",
            "codex",
        )
    finally:
        mesh.storage.close()

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0, combined
    assert "worktree" in combined.lower()
    assert "HARNESS_MESH_WORKSPACE" in combined
    assert "project root" in combined
    assert not (worktree / ".harness-mesh" / "state.db").exists()


def test_cli_dispatch_from_the_primary_root_still_rejects_deep_calls(tmp_path):
    mesh = _mesh_with_git_repo(tmp_path)
    config_path = _write_config(tmp_path)
    try:
        _add_running_assignment(mesh)
        completed, payload = _run_dispatch(mesh, mesh.workspace_root, config_path)
        request = mesh.storage.get_task(payload["task_id"])["request"]
    finally:
        mesh.storage.close()

    assert "Traceback" not in completed.stderr
    assert payload["status"] == "rejected", completed.stdout
    assert "max_call_depth" in payload["error_detail"]
    assert request is not None
    assert request.call_depth == MAX_DEPTH


def test_workspace_from_cwd_resolves_the_project_root(monkeypatch, tmp_path):
    from harness_mesh.lineage import _workspace_from_cwd

    primary = tmp_path / "project"
    worktree = primary / ".harness-mesh" / "worktrees" / "task-1" / "src"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)
    assert _workspace_from_cwd() == str(primary.resolve())
    monkeypatch.chdir(primary)
    assert _workspace_from_cwd() == str(primary.resolve())

    from harness_mesh.mcp_server import _workspace_from_cwd as mcp_workspace_from_cwd

    assert mcp_workspace_from_cwd is _workspace_from_cwd


def test_cli_init_mcp_in_a_worktree_writes_the_primary_workspace(tmp_path):
    mesh = _mesh_with_git_repo(tmp_path)
    worktree = _worktree_path(mesh)
    config_path = _write_config(tmp_path)
    try:
        completed = _run_cli(mesh, worktree, "--config", str(config_path), "init-mcp", "--print")
    finally:
        mesh.storage.close()

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    env = payload["mcpServers"]["harness-mesh"]["env"]
    assert env["HARNESS_MESH_WORKSPACE"] == str(mesh.workspace_root.resolve())
    assert not (worktree / ".harness-mesh" / "state.db").exists()


def test_ensure_service_workspace_rejects_a_worktree_root(tmp_path):
    primary = tmp_path / "repo"
    init_git_repo(primary, {"README.md": "worktree guard\n"})
    worktree = primary / ".harness-mesh" / "worktrees" / "parent"
    worktree.mkdir(parents=True)

    for auto_init in (False, True):
        with pytest.raises(RuntimeError, match="project root") as caught:
            ensure_service_workspace(worktree, auto_init_repo=auto_init)
        assert "HARNESS_MESH_WORKSPACE" in str(caught.value)
    with pytest.raises(RuntimeError, match="project root"):
        ensure_service_workspace(worktree / "nested", auto_init_repo=True)


def test_cli_tasks_in_a_worktree_reads_the_primary_ledger(tmp_path):
    mesh = _mesh_with_git_repo(tmp_path)
    worktree = _worktree_path(mesh)
    config_path = _write_config(tmp_path, codex_enabled=True)
    try:
        mesh.storage.create_task(
            TaskRequest(
                task_id="primary-ledger-task",
                target_harness="codex",
                objective="visible only in the primary ledger",
            )
        )
        completed = _run_cli(mesh, worktree, "--config", str(config_path), "tasks", "--json")
    finally:
        mesh.storage.close()

    assert completed.returncode == 0, completed.stderr
    rows = json.loads(completed.stdout)
    assert [row["task_id"] for row in rows] == ["primary-ledger-task"]
    assert not (worktree / ".harness-mesh" / "state.db").exists()


def test_cli_status_in_a_worktree_reads_primary_quota_state(tmp_path):
    mesh = _mesh_with_git_repo(tmp_path)
    worktree = _worktree_path(mesh)
    config_path = _write_config(tmp_path, codex_enabled=True)
    try:
        mesh.guardian.record_exhaustion(
            "codex",
            "*",
            "round5_status_probe",
            is_account_wide=True,
            cooldown_seconds=3600,
        )
        completed = _run_cli(mesh, worktree, "--config", str(config_path), "status", "--json")
    finally:
        mesh.storage.close()

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    by_name = {item["harness"]: item for item in payload}
    assert "codex" in by_name, payload
    assert by_name["codex"]["quota_state"] == "open", payload
    assert not (worktree / ".harness-mesh" / "state.db").exists()
