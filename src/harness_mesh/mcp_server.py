"""FastMCP server exposing the thirteen cross-harness orchestration tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

try:  # MCP v1 (the pinned API)
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - MCP v2 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore

from .config import load_config
from .models import (
    ContinuationCheckpoint,
    QuotaExhaustedPolicy,
    TaskDifficulty,
    TaskRequest,
    WorkspaceMode,
)
from .router import MeshRouter, MeshRuntime, create_mesh_runtime
from .storage import ResourceNotFoundError, TaskNotFoundError
from .workspace import WorkspaceError

TOOL_NAMES = [
    "list_harnesses",
    "discover_harness_models",
    "list_skills_and_plugins",
    "dispatch_task",
    "parallel_dispatch",
    "get_task_status",
    "read_task_resource",
    "get_quota_status",
    "report_quota_status",
    "apply_task_patch",
    "cancel_task",
    "get_project_context",
    "update_project_context",
]

_runtime: MeshRuntime | None = None


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
    inherited = list(inherited or []) or (_env_trace_chain() or [])
    for item in _env_trace_chain() or []:
        if item not in inherited:
            inherited.insert(0, item)
    if not argument:
        return list(inherited)
    merged = list(inherited)
    for item in argument:
        name = str(item)
        if name not in merged:
            merged.append(name)
    return merged


def _workspace_from_cwd() -> str:
    cwd = Path.cwd().resolve()
    parts = cwd.parts
    for index, part in enumerate(parts):
        if part == ".harness-mesh" and index + 1 < len(parts) and parts[index + 1] == "worktrees":
            return str(Path(*parts[:index]))
    return str(cwd)


def _task_id_from_cwd(router: Any) -> str | None:
    cwd = Path.cwd().resolve()
    worktrees = (router.workspace.state_dir / "worktrees").resolve()
    try:
        relative = cwd.relative_to(worktrees)
    except ValueError:
        relative = None
    if relative and relative.parts:
        return relative.parts[0]
    current = cwd
    while True:
        marker = current / ".harness-mesh-lineage.json"
        if marker.exists():
            try:
                return str(json.loads(marker.read_text(encoding="utf-8")).get("task_id") or "") or None
            except (OSError, json.JSONDecodeError):
                return None
        if current.parent == current:
            return None
        current = current.parent


def _inherited_lineage(router: Any) -> tuple[int, list[str]]:
    task_id = _task_id_from_cwd(router)
    if not task_id:
        return 0, []
    try:
        record = router.storage.get_task(task_id)
    except Exception:
        return 0, []
    request = record.get("request")
    depth = getattr(request, "call_depth", None)
    if depth is None and isinstance(request, dict):
        depth = request.get("call_depth")
    trace = getattr(request, "trace_chain", None)
    if trace is None and isinstance(request, dict):
        trace = request.get("trace_chain")
    return int(depth or 0), [str(item) for item in (trace or [])]


def get_runtime() -> MeshRuntime:
    global _runtime
    if _runtime is None:
        workspace = os.environ.get("HARNESS_MESH_WORKSPACE") or _workspace_from_cwd()
        _runtime = create_mesh_runtime(
            config_path=os.environ.get("HARNESS_MESH_CONFIG"),
            workspace_root=workspace,
        )
    return _runtime


def set_runtime(runtime: MeshRuntime | None) -> None:
    """Test hook: inject or reset the module-level runtime."""

    global _runtime
    _runtime = runtime


def _error_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def create_server(runtime: MeshRuntime | MeshRouter | None = None) -> Any:
    server = FastMCP("harness-mesh")
    router: MeshRouter = runtime.router if isinstance(runtime, MeshRuntime) else (runtime or get_runtime().router)

    @server.tool()
    async def list_harnesses(refresh_models: bool = False) -> dict[str, Any]:
        """List locally available harnesses, discovered authorised models, tier bindings and quota state."""

        statuses = await router.list_harnesses(refresh_models=refresh_models)
        return {"harnesses": [status.model_dump(mode="json") for status in statuses]}

    @server.tool()
    async def discover_harness_models(
        harness: str | None = None, force_refresh: bool = True
    ) -> dict[str, Any]:
        """Run the three-layer model discovery waterfall for one or all harnesses."""

        payload = await router.discover_harness_models(harness=harness, force_refresh=force_refresh)
        if harness is None:
            return {"harnesses": payload}
        return payload

    @server.tool()
    def list_skills_and_plugins(query: str = "") -> dict[str, Any]:
        """Search indexed SKILL.md files and MCP/plugin manifests."""

        return router.list_skills_and_plugins(query=query)

    @server.tool()
    async def dispatch_task(
        objective: str,
        target_harness: str,
        difficulty: str = "auto",
        model: str | None = None,
        reasoning_effort: str | None = None,
        skills: list[str] | None = None,
        plugins: list[str] | None = None,
        auto_match_skills: bool = False,
        on_quota_exhausted: str = "failover",
        fallback_harnesses: list[str] | None = None,
        max_failover_attempts: int | None = None,
        context_files: list[str] | None = None,
        context_notes: str = "",
        depends_on: list[str] | None = None,
        sync_uncommitted: bool = True,
        include_project_context: bool = True,
        auto_record_context: bool = False,
        workspace_mode: str = "isolated_worktree",
        workspace_root: str | None = None,
        timeout_seconds: int | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        call_depth: int | None = None,
        trace_chain: list[str] | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a ticket.

        Quota exhaustion creates a new Assignment on another harness and resumes
        in the same worktree.
        """

        inherited_depth, inherited_trace = _inherited_lineage(router)
        request_kwargs: dict[str, Any] = {
            "target_harness": target_harness,
            "objective": objective,
            "difficulty": TaskDifficulty(difficulty),
            "model": model,
            "reasoning_effort": reasoning_effort,
            "skills": list(skills or []),
            "plugins": list(plugins or []),
            "auto_match_skills": auto_match_skills,
            "on_quota_exhausted": QuotaExhaustedPolicy(on_quota_exhausted),
            "fallback_harnesses": list(fallback_harnesses or []),
            "context_files": list(context_files or []),
            "context_notes": context_notes,
            "depends_on": list(depends_on or []),
            "sync_uncommitted": sync_uncommitted,
            "include_project_context": include_project_context,
            "auto_record_context": auto_record_context,
            "workspace_mode": WorkspaceMode(workspace_mode),
            "workspace_root": workspace_root or str(router.workspace_root),
            "timeout_seconds": timeout_seconds,
            "session_id": session_id,
            "call_depth": _effective_call_depth(call_depth, inherited_depth),
            "trace_chain": _merge_trace_chain(trace_chain, inherited_trace),
            "checkpoint": ContinuationCheckpoint.model_validate(checkpoint) if checkpoint else None,
        }
        if max_failover_attempts is not None:
            request_kwargs["max_failover_attempts"] = max_failover_attempts
        parent_task_id = os.environ.get("HARNESS_MESH_TASK_ID")
        if parent_task_id:
            request_kwargs["parent_task_id"] = parent_task_id
        if task_id:
            request_kwargs["task_id"] = task_id
        request = TaskRequest(**request_kwargs)
        result = await router.dispatch(request)
        return result.model_dump(mode="json")

    @server.tool()
    async def parallel_dispatch(
        tasks: list[dict[str, Any]], max_concurrency: int = 8
    ) -> dict[str, Any]:
        """Dispatch multiple tickets concurrently across different harnesses."""

        parent_task_id = os.environ.get("HARNESS_MESH_TASK_ID")
        inherited_depth, inherited_trace = _inherited_lineage(router)
        normalized: list[dict[str, Any]] = []
        for task in tasks:
            item = dict(task)
            item.setdefault("workspace_root", str(router.workspace_root))
            item["call_depth"] = _effective_call_depth(item.get("call_depth"), inherited_depth)
            item["trace_chain"] = _merge_trace_chain(item.get("trace_chain"), inherited_trace)
            if "parent_task_id" not in item and parent_task_id:
                item["parent_task_id"] = parent_task_id
            normalized.append(item)
        requests = [TaskRequest.model_validate(task) for task in normalized]
        results = await router.parallel_dispatch(requests, max_concurrency=max_concurrency)
        return {"results": [result.model_dump(mode="json") for result in results]}

    @server.tool()
    def get_task_status(task_id: str) -> dict[str, Any]:
        """Return ticket status plus every assignment and failover hop."""

        try:
            return router.get_task_status(task_id)
        except TaskNotFoundError as exc:
            return {"error": _error_detail(exc), "task_id": task_id}

    @server.tool()
    def read_task_resource(
        task_id: str, resource: str = "diff", offset: int = 0, limit: int = 4000
    ) -> dict[str, Any]:
        """Page through a large diff/transcript/assignments payload to protect the controller context window."""

        try:
            return router.read_task_resource(task_id, resource=resource, offset=offset, limit=limit)
        except (TaskNotFoundError, ResourceNotFoundError) as exc:
            return {"error": _error_detail(exc), "task_id": task_id, "resource": resource}

    @server.tool()
    async def get_quota_status(probe_now: bool = False) -> dict[str, Any]:
        """Show quota circuits and the waiting_for_quota queue.

        ``probe_now`` triggers recovery probes and resumes waiting tickets.
        """

        return await router.get_quota_status(probe_now=probe_now)

    @server.tool()
    async def report_quota_status(
        harness: str,
        model: str = "*",
        action: str = "reset",
        cooldown_seconds: int = 300,
    ) -> dict[str, Any]:
        """Manually reset or mark a quota circuit (actions: reset, open, half_open)."""

        try:
            return await router.report_quota_status(
                harness,
                model=model,
                action=action,
                cooldown_seconds=cooldown_seconds,
            )
        except ValueError as exc:
            return {"error": _error_detail(exc), "action": action}

    @server.tool()
    def apply_task_patch(task_id: str, strategy: str = "patch") -> dict[str, Any]:
        """Apply the isolated worktree changes back into the primary workspace.

        Only ``patch`` and ``merge`` are exposed over MCP. The ``copy``
        strategy is destructive and remains a CLI-only operation.
        """

        if strategy not in {"patch", "merge"}:
            return {
                "error": "MCP apply_task_patch supports only 'patch' or 'merge'; "
                "use the CLI `hmesh apply --strategy copy --force-copy`",
                "task_id": task_id,
            }
        try:
            return router.apply_task_patch(task_id, strategy=strategy)
        except (TaskNotFoundError, WorkspaceError, ValueError) as exc:
            return {"error": _error_detail(exc), "task_id": task_id}

    @server.tool()
    async def cancel_task(task_id: str) -> dict[str, Any]:
        """Cancel a running or waiting ticket. The worktree is retained for inspection."""

        try:
            return await router.cancel_task(task_id)
        except TaskNotFoundError as exc:
            return {"error": _error_detail(exc), "task_id": task_id}

    @server.tool()
    def get_project_context(
        category: str | None = None, include_instructions: bool = True
    ) -> dict[str, Any]:
        """Read the cross-harness project memory blackboard and instruction files."""

        return router.get_project_context(
            category=category, include_instructions=include_instructions
        )

    @server.tool()
    def update_project_context(
        key: str,
        content: str,
        category: str = "note",
        author_harness: str = "controller",
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Write or update one shared project fact or interface contract."""

        try:
            source = "worker" if _task_id_from_cwd(router) or os.environ.get("HARNESS_MESH_TASK_ID") else "controller"
            return router.update_project_context(
                key,
                content,
                category=category,
                author_harness=author_harness,
                task_id=task_id,
                source=source,
            )
        except Exception as exc:
            return {"error": _error_detail(exc), "key": key}

    return server


async def _serve(args: argparse.Namespace) -> None:
    config = load_config(args.config, args.workspace)
    runtime = create_mesh_runtime(
        config_path=args.config,
        workspace_root=args.workspace,
        config=config,
    )
    set_runtime(runtime)
    server = create_server(runtime)
    if not args.no_monitor:
        # Real-time quota recovery and durable-effect consumption are part of
        # the server contract; a stdio MCP process is long-lived, so start both
        # by default and let operators opt out explicitly.
        await runtime.start_monitor()
    try:
        # FastMCP exposes both sync and async transports; stdio is the MCP default.
        run = server.run_stdio_async if hasattr(server, "run_stdio_async") else server.run
        result = run()
        if asyncio.iscoroutine(result):
            await result
    finally:
        await runtime.aclose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hmesh-mcp", description="Harness-Mesh MCP server")
    parser.add_argument("--config", default=os.environ.get("HARNESS_MESH_CONFIG"))
    parser.add_argument("--workspace", default=os.environ.get("HARNESS_MESH_WORKSPACE") or os.getcwd())
    parser.add_argument(
        "--no-monitor",
        action="store_true",
        help="disable the background quota recovery monitor and effects consumer",
    )
    parser.add_argument("--monitor", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    asyncio.run(_serve(args))


if __name__ == "__main__":  # pragma: no cover
    main()
