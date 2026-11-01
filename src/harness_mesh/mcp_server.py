"""FastMCP server exposing the fifteen cross-harness orchestration tools."""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import os
from typing import Any

try:  # MCP v1 (the pinned API)
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - MCP v2 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore

from .config import load_config
from .lineage import (
    _effective_call_depth,
    _inherited_lineage,
    _merge_trace_chain,
    _task_id_from_cwd,
    _workspace_from_cwd,
)
from .models import (
    ContinuationCheckpoint,
    QuotaExhaustedPolicy,
    TaskDifficulty,
    TaskKind,
    TaskRequest,
    WorkspaceMode,
)
from .router import MeshRouter, MeshRuntime, create_mesh_runtime
from .storage import ResourceNotFoundError, TaskNotFoundError
from .workspace import WorkspaceError

TOOL_NAMES = [
    "list_harnesses",
    "discover_harness_models",
    "get_model_market",
    "preview_routing",
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

# Sent to every client in the MCP ``initialize`` response.  Fixed text, no
# user data and no paths: the controller never needs to name a model or a
# harness, and the mesh owns that decision.
MESH_INSTRUCTIONS = (
    "Ask for an outcome. Do not pass model or target_harness: the mesh picks a "
    "model from the task kind and price/performance. Pass task_kind when you "
    "know it. Review changes with get_task_status and read_task_resource before "
    "apply_task_patch."
)

_runtime: MeshRuntime | None = None


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
    server = FastMCP("harness-mesh", instructions=MESH_INSTRUCTIONS)
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
    def get_model_market(query: str = "", limit: int = 50) -> dict[str, Any]:
        """Read cached model prices, context windows and intelligence scores.

        ``intelligence_source`` says whether a score came from Artificial
        Analysis, a user override or the local heuristic.  This tool never
        refreshes the cache and never performs a network request.
        """

        catalog = router.market.catalog()
        pattern = (query or "").strip().lower()
        models = list(catalog.models)
        if pattern:
            if any(character in pattern for character in "*?["):
                models = [
                    model
                    for model in models
                    if fnmatch.fnmatchcase(model.key.lower(), pattern)
                    or fnmatch.fnmatchcase(model.name.lower(), pattern)
                ]
            else:
                models = [
                    model
                    for model in models
                    if pattern in model.key.lower() or pattern in model.name.lower()
                ]
        cap = max(1, min(int(limit), 500))
        return {
            "models": [model.model_dump(mode="json") for model in models[:cap]],
            "matched": len(models),
            "returned": min(len(models), cap),
            "sources": list(catalog.sources),
            "fetched_at": catalog.fetched_at,
            "stale": catalog.stale,
            "expired": catalog.expired,
            "warnings": list(catalog.warnings),
        }

    @server.tool()
    async def preview_routing(
        objective: str,
        task_kind: str = "auto",
        difficulty: str = "auto",
        target_harness: str = "auto",
    ) -> dict[str, Any]:
        """Preview the routing decision for an objective without dispatching it."""

        try:
            return await router.preview_routing(
                objective,
                task_kind=task_kind,
                difficulty=difficulty,
                target_harness=target_harness,
            )
        except (ValueError, TypeError) as exc:
            return {"error": _error_detail(exc), "objective": objective}

    @server.tool()
    def list_skills_and_plugins(query: str = "") -> dict[str, Any]:
        """Search indexed SKILL.md files and MCP/plugin manifests."""

        return router.list_skills_and_plugins(query=query)

    @server.tool()
    async def dispatch_task(
        objective: str,
        target_harness: str = "auto",
        difficulty: str = "auto",
        task_kind: str = "auto",
        routing_mode: str | None = None,
        max_price_per_mtok: float | None = None,
        min_intelligence: float | None = None,
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

        Only the objective is required. Leave target_harness at "auto" and model
        unset to let the mesh choose; pass task_kind when it is known. Quota
        exhaustion creates a new Assignment on another harness and resumes in
        the same worktree.
        """

        inherited_depth, inherited_trace = _inherited_lineage(router)
        request_kwargs: dict[str, Any] = {
            "target_harness": target_harness,
            "objective": objective,
            "difficulty": TaskDifficulty(difficulty),
            "task_kind": TaskKind(task_kind),
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
        if routing_mode is not None:
            request_kwargs["routing_mode"] = routing_mode
        if max_price_per_mtok is not None:
            request_kwargs["max_price_per_mtok"] = max_price_per_mtok
        if min_intelligence is not None:
            request_kwargs["min_intelligence"] = min_intelligence
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
        """Dispatch multiple tickets concurrently.

        Every task accepts the same fields as ``dispatch_task``, including
        ``task_kind``, ``routing_mode``, ``max_price_per_mtok`` and
        ``min_intelligence``. ``target_harness`` is optional and defaults to
        ``auto``.
        """

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
    # When the host launches the MCP server from inside a worktree (Codex
    # forwards cwd but not the mesh environment), the project workspace is the
    # parent of .harness-mesh, not the worktree directory itself.
    parser.add_argument(
        "--workspace",
        default=os.environ.get("HARNESS_MESH_WORKSPACE") or _workspace_from_cwd(),
    )
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
