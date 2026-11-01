"""Round 4 WP-R13: the CLI must apply the same recursion guard as MCP.

A worker can run ``hmesh dispatch`` from its own shell.  Codex does not forward
``HARNESS_MESH_*`` to children, and every harness can pass ``--call-depth 0``,
so the command line has to recover lineage from the shared ledger and the
process ancestor chain exactly like the MCP server does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from harness_mesh.models import AssignmentRecord
from tests.helpers import FakeAdapter, make_mesh, success

REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_DEPTH = 3
HARNESSES = ("claude_code", "codex", "deepseek", "antigravity", "zcode")


def _disabled_config(tmp_path: Path) -> Path:
    path = tmp_path / "disabled-harnesses.yaml"
    path.write_text(
        f"max_call_depth: {MAX_DEPTH}\nauto_init_repo: true\nharnesses:\n"
        + "".join(f"  {name}:\n    enabled: false\n" for name in HARNESSES),
        encoding="utf-8",
    )
    return path


def _add_running_assignment(mesh, task_id: str = "cli-parent") -> AssignmentRecord:
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


def _run_cli_dispatch(
    mesh, config_path: Path, *extra: str
) -> tuple[subprocess.CompletedProcess[str], dict]:
    env = {
        key: value
        for key, value in os.environ.items()
        # Keep the suite's offline switch (children cannot inherit the socket
        # guard); every lineage-related variable is still dropped on purpose.
        if not key.startswith("HARNESS_MESH_") or key == "HARNESS_MESH_OFFLINE"
    }
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["HOME"] = str(mesh.home_dir)
    command = [
        sys.executable,
        "-m",
        "harness_mesh.cli",
        "--workspace",
        str(mesh.workspace_root),
        "--config",
        str(config_path),
        "dispatch",
        "--objective",
        "child dispatch from a worker shell",
        "--harness",
        "codex",
        *extra,
    ]
    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed, json.loads(completed.stdout)


def _stored_request(mesh, task_id: str):
    return mesh.storage.get_task(task_id)["request"]


def test_cli_dispatch_inherits_ledger_depth_without_any_mesh_environment(tmp_path):
    mesh = make_mesh(tmp_path, adapters={"codex": FakeAdapter("codex", lambda a, c: success("child"))})
    config_path = _disabled_config(tmp_path)
    try:
        _add_running_assignment(mesh)
        started = time.monotonic()
        _, payload = _run_cli_dispatch(mesh, config_path)
        elapsed = time.monotonic() - started
        request = _stored_request(mesh, payload["task_id"])
    finally:
        mesh.storage.close()

    assert elapsed < 3.0, f"offline CLI dispatch took {elapsed:.2f}s"
    assert payload["status"] == "rejected", payload
    assert f"call_depth={MAX_DEPTH}" in payload["error_detail"]
    assert "max_call_depth" in payload["error_detail"]
    assert request is not None
    assert request.call_depth == MAX_DEPTH


def test_cli_dispatch_call_depth_option_cannot_lower_inherited_depth(tmp_path):
    mesh = make_mesh(tmp_path, adapters={"codex": FakeAdapter("codex", lambda a, c: success("child"))})
    config_path = _disabled_config(tmp_path)
    try:
        _add_running_assignment(mesh)
        started = time.monotonic()
        completed, payload = _run_cli_dispatch(
            mesh, config_path, "--call-depth", "0", "--trace-chain", '["zcode"]'
        )
        elapsed = time.monotonic() - started
        request = _stored_request(mesh, payload["task_id"])
    finally:
        mesh.storage.close()

    assert elapsed < 3.0, f"offline CLI dispatch took {elapsed:.2f}s"
    assert "Traceback" not in completed.stderr
    assert payload["status"] == "rejected", completed.stdout
    assert "max_call_depth" in payload["error_detail"]
    assert request is not None
    assert request.call_depth == MAX_DEPTH
    # The option and the ledger chain are unioned; neither source is dropped.
    assert request.trace_chain == ["codex", "zcode"]


def test_trace_chain_union_keeps_environment_ledger_and_option_order(monkeypatch):
    from harness_mesh.lineage import _merge_trace_chain

    monkeypatch.setenv("HARNESS_MESH_TRACE_CHAIN", json.dumps(["env-a", "env-b"]))
    # Environment entries missing from the ledger come first in their own
    # order, the ledger chain keeps its order, and the option is appended.
    assert _merge_trace_chain(["option"], ["ledger"]) == ["env-a", "env-b", "ledger", "option"]
    # A duplicate is dropped at its first occurrence.
    assert _merge_trace_chain(["ledger", "option"], ["env-b", "ledger"]) == [
        "env-a",
        "env-b",
        "ledger",
        "option",
    ]


def test_cli_dispatch_without_a_running_assignment_starts_at_depth_zero(tmp_path):
    mesh = make_mesh(tmp_path, adapters={"codex": FakeAdapter("codex", lambda a, c: success("child"))})
    config_path = _disabled_config(tmp_path)
    try:
        completed, payload = _run_cli_dispatch(mesh, config_path)
        request = _stored_request(mesh, payload["task_id"])
    finally:
        mesh.storage.close()

    assert "Traceback" not in completed.stderr
    # No harness is enabled in this config, so the dispatch stops at model
    # selection; what matters is that it is not a depth rejection.
    assert payload["status"] != "rejected", completed.stdout
    assert "max_call_depth" not in (payload.get("error_detail") or "")
    assert request is not None
    assert request.call_depth == 0
