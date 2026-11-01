"""Lineage recovery for nested dispatches, shared by the MCP server and CLI.

A worker harness reaches the mesh either through the MCP server it was given or
through ``hmesh dispatch`` in its own shell.  Both entry points have to answer
the same question: which running Assignment is my parent, and how deep is the
call?  Neither can trust the inherited environment alone, because hosts differ
in what they forward and the caller controls ``--call-depth``.  The functions
here recover the recorded values from the shared ledger, the process ancestor
chain and a cwd under a task worktree, then merge them with the options and the
environment by maximum/union, so a caller can only raise the effective depth.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _workspace_from_cwd() -> str:
    """Resolve a cwd inside a task worktree back to the primary project root.

    Hosts may launch the MCP server or the CLI from
    ``<project>/.harness-mesh/worktrees/<task_id>`` without forwarding
    ``HARNESS_MESH_WORKSPACE``.  Treating that directory as a project would
    open a second, empty ``state.db`` and lose the ledger that records the
    parent assignment, so the path before ``.harness-mesh`` wins.
    """

    cwd = Path.cwd().resolve()
    parts = cwd.parts
    for index, part in enumerate(parts):
        if part == ".harness-mesh" and index + 1 < len(parts) and parts[index + 1] == "worktrees":
            return str(Path(*parts[:index]))
    return str(cwd)


def _env_int(name: str) -> int | None:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _env_trace_chain(name: str = "HARNESS_MESH_TRACE_CHAIN") -> list[str] | None:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return None


def _effective_call_depth(argument: int | None, inherited: int | None = None) -> int:
    env_depth = _env_int("HARNESS_MESH_CALL_DEPTH") or 0
    inherited = max(env_depth, int(inherited or 0))
    if argument is None:
        return inherited
    try:
        requested = int(argument)
    except (TypeError, ValueError):
        requested = 0
    return max(inherited, requested)


def _merge_trace_chain(argument: list[str] | None, inherited: list[str] | None = None) -> list[str]:
    """Union the option, environment and ledger chains without reordering them.

    The ledger chain keeps its recorded order; environment entries missing
    from it are prepended in their own order, then the option entries are
    appended.  Duplicates are dropped at their first occurrence.
    """

    inherited = list(inherited or [])
    environment = _env_trace_chain() or []
    if not inherited:
        inherited = list(environment)
    else:
        prefix = [item for item in environment if item not in inherited]
        inherited = [*prefix, *inherited]
    if not argument:
        return list(inherited)
    merged = list(inherited)
    for item in argument:
        name = str(item)
        if name not in merged:
            merged.append(name)
    return merged


@dataclass(frozen=True)
class _Lineage:
    """Lineage recovered from the shared ledger, not from the environment."""

    task_id: str
    call_depth: int
    trace_chain: list[str]


def _lineage_from_assignment(record: Any) -> _Lineage:
    return _Lineage(
        task_id=str(record.task_id),
        call_depth=int(record.call_depth or 0),
        trace_chain=[str(item) for item in (record.trace_chain or [])],
    )


def _parent_pids(limit: int = 32) -> list[int]:
    """Ancestor pids, starting at ``os.getppid()``; stops at pid 1 or on error."""

    pids: list[int] = []
    pid = os.getppid()
    while 1 < pid and pid not in pids and len(pids) < limit:
        pids.append(pid)
        try:
            completed = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            break
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        try:
            pid = int(lines[0]) if lines else 0
        except ValueError:
            break
    return pids


def _lineage_from_ancestry(router: Any) -> _Lineage | None:
    """Match a live assignment pid against the process ancestor chain.

    Codex 0.166 does not forward the host environment to MCP children, but
    it still starts them as its own children, so this path survives there and
    in ``shared_write`` mode where the cwd is the primary workspace.
    """

    ancestors = _parent_pids()
    if not ancestors:
        return None
    by_pid: dict[int, Any] = {}
    for record in router.storage.list_running_assignments():
        if record.pid is not None:
            by_pid[int(record.pid)] = record
    for pid in ancestors:
        record = by_pid.get(pid)
        if record is not None:
            return _lineage_from_assignment(record)
    return None


def _worktree_task_id(router: Any) -> str | None:
    cwd = Path.cwd().resolve()
    worktrees = (router.workspace.state_dir / "worktrees").resolve()
    try:
        relative = cwd.relative_to(worktrees)
    except ValueError:
        return None
    return relative.parts[0] if relative.parts else None


def _marker_task_id(router: Any) -> str | None:
    """Read the agent-writable marker without trusting it on its own.

    The marker only counts when the database already says this task is
    running and its recorded worktree really contains the current directory.
    A conflicting worktree path always wins, and markers in the primary
    workspace (``shared_write``) are ignored in favour of ancestry.
    """

    cwd = Path.cwd().resolve()
    current = cwd
    while True:
        marker = current / ".harness-mesh-lineage.json"
        if marker.exists():
            try:
                candidate = str(json.loads(marker.read_text(encoding="utf-8")).get("task_id") or "")
            except (OSError, json.JSONDecodeError):
                return None
            if not candidate:
                return None
            try:
                task = router.storage.get_task(candidate)
            except Exception:
                return None
            worktree_path = task.get("worktree_path")
            if not worktree_path:
                return None
            try:
                worktree = Path(worktree_path).expanduser().resolve()
            except OSError:
                return None
            if not cwd.is_relative_to(worktree):
                return None
            if task.get("status") not in {"running", "pending"}:
                return None
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def _lineage_from_worktree(router: Any) -> _Lineage | None:
    task_id = _worktree_task_id(router)
    if task_id:
        record = router.storage.get_running_assignment(task_id)
        if record is not None:
            return _lineage_from_assignment(record)
        return _lineage_from_task_request(router, task_id)
    marker_task = _marker_task_id(router)
    if marker_task:
        record = router.storage.get_running_assignment(marker_task)
        if record is not None:
            return _lineage_from_assignment(record)
        return _lineage_from_task_request(router, marker_task)
    return None


def _lineage_from_task_request(router: Any, task_id: str) -> _Lineage | None:
    """Legacy databases have no assignment row; fall back to the ticket."""

    try:
        record = router.storage.get_task(task_id)
    except Exception:
        return None
    request = record.get("request")
    depth = getattr(request, "call_depth", None)
    if depth is None and isinstance(request, dict):
        depth = request.get("call_depth")
    trace = getattr(request, "trace_chain", None)
    if trace is None and isinstance(request, dict):
        trace = request.get("trace_chain")
    return _Lineage(
        task_id=task_id,
        call_depth=int(depth or 0),
        trace_chain=[str(item) for item in (trace or [])],
    )


def _resolved_lineage(router: Any) -> _Lineage | None:
    return _lineage_from_ancestry(router) or _lineage_from_worktree(router)


def _task_id_from_cwd(router: Any) -> str | None:
    lineage = _resolved_lineage(router)
    return lineage.task_id if lineage is not None else None


def _inherited_lineage(router: Any) -> tuple[int, list[str]]:
    lineage = _resolved_lineage(router)
    if lineage is None:
        return 0, []
    return lineage.call_depth, list(lineage.trace_chain)
