"""``hmesh`` command-line interface."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import typer

from .approvals import APPROVAL_LEVELS, DEFAULT_APPROVAL_LEVEL
from .config import find_config_path, load_config
from .installer import NO_APPROVAL_DETAIL, HarnessInstaller
from .lineage import (
    _effective_call_depth,
    _merge_trace_chain,
    _resolved_lineage,
    _workspace_from_cwd,
)
from .market import (
    EPOCH_ATTRIBUTION,
    MarketService,
    filter_market_models,
    is_non_model_id,
    list_unmatched,
    match_model,
)
from .models import (
    QuotaExhaustedPolicy,
    RoutingMode,
    TaskDifficulty,
    TaskKind,
    TaskRequest,
    WorkspaceMode,
)
from .router import create_mesh_runtime
from .security import ensure_service_workspace
from .storage import StateStore

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Federated cross-harness scheduling mesh (Claude Code / Codex / DeepSeek / Antigravity / ZCode).",
)
context_app = typer.Typer(help="Read and update the cross-harness project context blackboard.")
app.add_typer(context_app, name="context")
market_app = typer.Typer(help="Inspect and refresh model prices and evaluation scores.")
app.add_typer(market_app, name="market")


def _json(data: Any) -> str:
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


@app.callback()
def main(
    ctx: typer.Context,
    config: Optional[str] = typer.Option(
        None, "--config", "-c", help="Path to harnesses YAML config (defaults to config/harnesses.example.yaml)."
    ),
    workspace: Optional[str] = typer.Option(
        None, "--workspace", "-w", help="Primary workspace root (defaults to the current directory)."
    ),
) -> None:
    """Global options shared by every subcommand."""

    ctx.obj = {
        "config": config,
        "workspace": workspace or os.environ.get("HARNESS_MESH_WORKSPACE") or _workspace_from_cwd(),
    }


def _runtime(ctx: typer.Context):
    options = ctx.obj or {}
    workspace = options.get("workspace") or _workspace_from_cwd()
    try:
        return create_mesh_runtime(
            config_path=options.get("config"),
            workspace_root=workspace,
        )
    except RuntimeError as exc:
        typer.echo(f"Cannot start harness-mesh from this workspace: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _ensure_market(runtime) -> None:
    """Refresh a missing or expired market cache before a one-shot command.

    The wait is bounded by ``market.wait_seconds`` (8 s by default).  A
    timeout or a failed request leaves the previous snapshot in place and the
    command continues; it never fails because a third-party site is down.
    """

    market = getattr(runtime.router, "market", None)
    if market is None:
        return
    try:
        asyncio.run(market.ensure_fresh())
    except Exception:
        pass


def _trace_chain_option(raw: str | None) -> list[str] | None:
    """Parse ``--trace-chain`` the same way the MCP tool accepts its list."""

    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return None


def _task_kind_value(raw: str) -> TaskKind:
    try:
        return TaskKind(str(raw).lower())
    except ValueError as exc:
        raise typer.BadParameter(
            "expected one of: " + ", ".join(item.value for item in TaskKind),
            param_hint="--kind",
        ) from exc


def _routing_mode_value(raw: str) -> RoutingMode:
    try:
        return RoutingMode(str(raw).lower())
    except ValueError as exc:
        raise typer.BadParameter(
            "expected one of: " + ", ".join(item.value for item in RoutingMode),
            param_hint="--routing-mode",
        ) from exc


def _approval_summary(level: str, results) -> str:
    approved: set[str] = set()
    for result in results:
        if result.installed:
            approved.update(result.approvals.get("tools") or [])
    if level == "none":
        return (
            "No MCP tools were pre-approved; headless runs will be denied. "
            "Use `--approve read` or `--approve dispatch` to pre-approve them."
        )
    if level == "all" and approved:
        return f"Approved {len(approved)} tools (all); headless runs no longer need per-call approval."
    if approved:
        return (
            f"Approved {len(approved)} tools; headless runs still need manual approval for "
            "`apply_task_patch` and other write tools. Use `--approve all` to change that."
        )
    return (
        "The selected harnesses need no approval settings; headless runs still need manual "
        "approval for `apply_task_patch` and other write tools. Use `--approve all` to change that."
    )


def _market_doctor_report(config, workspace: str) -> dict[str, Any]:
    """Read-only market health for ``hmesh doctor`` (never writes a file)."""

    report: dict[str, Any] = {
        "harness": "market",
        "sources": [],
        "last_refresh": None,
        "stale": None,
        "models": 0,
        "matched": 0,
        "match_rate": None,
        "unmatched": [],
        "note": None,
    }
    state_db = Path(config.state_path(workspace)) / "state.db"
    if not state_db.exists():
        report["note"] = "no market cache yet"
        return report
    try:
        store = StateStore(state_db, read_only=True)
    except Exception as exc:
        report["note"] = f"cannot read the ledger: {exc}"
        return report
    try:
        service = MarketService.from_config(config, store)
        catalog = service.catalog()
        report["sources"] = list(catalog.sources)
        report["last_refresh"] = catalog.fetched_at
        report["stale"] = bool(catalog.stale)
        discovered = [
            model
            for model in store.list_discovered_models()
            if not is_non_model_id(model.model_id)
        ]
        missing: list[str] = []
        matched = 0
        for model in discovered:
            found = match_model(
                model.model_id, catalog.models, aliases=config.market.aliases
            )
            if found.key:
                matched += 1
            else:
                missing.append(model.model_id)
        report["models"] = len(discovered)
        report["matched"] = matched
        report["match_rate"] = (
            round(matched / len(discovered), 3) if discovered else None
        )
        report["unmatched"] = missing
    finally:
        store.close()
    return report


def _echo_market_doctor(report: dict[str, Any]) -> None:
    if report.get("note"):
        typer.echo(f"{'market':14} {'empty':10} {report['note']}")
        return
    sources = ", ".join(report.get("sources") or []) or "-"
    last = report.get("last_refresh")
    stamp = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last)) if last else "-"
    )
    rate = report.get("match_rate")
    rate_text = f"{rate:.0%}" if rate is not None else "-"
    stale = "yes" if report.get("stale") else "no"
    unmatched = ", ".join(report.get("unmatched") or []) or "none"
    typer.echo(
        f"{'market':14} {'ok':10} sources={sources} last_refresh={stamp} "
        f"stale={stale} models={report.get('models')} "
        f"matched={report.get('matched')} ({rate_text}) unmatched={unmatched}"
    )


def _echo_install_results(results) -> None:
    for result in results:
        typer.echo(f"{result.harness:12} {result.action:12} {result.detail}")
        approvals = result.approvals or {}
        if approvals:
            tools = approvals.get("tools") or []
            typer.echo(
                f"  approvals: level={approvals.get('level')} tools={len(tools)} "
                f"status={approvals.get('status')}"
            )
            if approvals.get("status") == "planned":
                for entry in approvals.get("entries") or []:
                    typer.echo(f"    {entry}")
        for path in result.files:
            typer.echo(f"  file: {path}")
        for command in result.commands:
            typer.echo(f"  cmd : {' '.join(command)}")


@app.command()
def install(
    ctx: typer.Context,
    harness: Optional[list[str]] = typer.Option(
        None, "--harness", "-H", help="Install only this harness (repeatable). Default: all detected."
    ),
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove the MCP/plugin integration."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show planned commands/files without changing anything."),
    yes: bool = typer.Option(False, "--yes", help="Skip the interactive confirmation."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    approve: str = typer.Option(
        DEFAULT_APPROVAL_LEVEL,
        "--approve",
        help="Pre-approve MCP tools: read, dispatch (default), all, or none.",
    ),
) -> None:
    """One-command installation of Harness-Mesh into every detected harness UI."""

    level = approve.lower()
    if level not in APPROVAL_LEVELS:
        raise typer.BadParameter(
            "expected one of: " + ", ".join(APPROVAL_LEVELS),
            param_hint="--approve",
        )
    options = ctx.obj or {}
    workspace = options.get("workspace") or _workspace_from_cwd()
    config_path = options.get("config")
    config = load_config(config_path, workspace)
    try:
        ensure_service_workspace(workspace, auto_init_repo=config.auto_init_repo)
    except RuntimeError as exc:
        typer.echo(f"Cannot install from this workspace: {exc}", err=True)
        raise typer.Exit(code=1)

    def build(dry: bool):
        return HarnessInstaller(
            config,
            workspace,
            config_path=config_path or find_config_path(workspace),
            dry_run=dry,
            approval_level=level,
        )

    planned = build(True).install(harness, uninstall=uninstall)
    if json_output and dry_run:
        typer.echo(_json([result.as_dict() for result in planned]))
        return
    if not json_output:
        typer.echo("Planned changes:")
        _echo_install_results(planned)
    if dry_run:
        return
    if not yes:
        if not sys.stdin.isatty():
            typer.echo(
                "Refusing to modify harness configuration in a non-interactive environment; "
                "re-run with --yes or inspect with --dry-run.",
                err=True,
            )
            raise typer.Exit(code=1)
        if not typer.confirm("Apply these changes?"):
            typer.echo("Cancelled.")
            raise typer.Exit(code=0)
    results = build(False).install(harness, uninstall=uninstall)
    if json_output:
        typer.echo(_json([result.as_dict() for result in results]))
    else:
        typer.echo("Result:")
        _echo_install_results(results)
        if not uninstall and any(result.installed for result in results):
            typer.echo(_approval_summary(level, results))
    if any(result.action == "failed" for result in results):
        raise typer.Exit(code=1)


@app.command()
def doctor(
    ctx: typer.Context,
    harness: Optional[list[str]] = typer.Option(
        None, "--harness", "-H", help="Check only this harness (repeatable)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Check registered MCP/plugin commands and their launch health."""

    options = ctx.obj or {}
    workspace = options.get("workspace") or _workspace_from_cwd()
    config_path = options.get("config")
    config = load_config(config_path, workspace)
    installer = HarnessInstaller(
        config,
        workspace,
        config_path=config_path or find_config_path(workspace),
    )
    results = installer.doctor(harness)
    market_report = _market_doctor_report(config, workspace)
    for result in results:
        registration = installer._read_registration(result.harness)
        if registration is None:
            continue
        registered_workspace = (registration.get("env") or {}).get("HARNESS_MESH_WORKSPACE")
        if not registered_workspace:
            continue
        try:
            ensure_service_workspace(registered_workspace, auto_init_repo=config.auto_init_repo)
        except RuntimeError as exc:
            result.status = "failed"
            result.detail = f"registered workspace is invalid: {exc}"
    if json_output:
        typer.echo(_json([result.as_dict() for result in results] + [market_report]))
    else:
        for result in results:
            typer.echo(f"{result.harness:12} {result.status:14} {result.detail}")
            approvals = result.approvals or {}
            if not approvals:
                continue
            if approvals.get("status") == "not_required":
                typer.echo(f"  approvals: {NO_APPROVAL_DETAIL}")
            elif approvals.get("status") == "failed":
                typer.echo(f"  approvals: {approvals.get('detail')}")
            else:
                missing = ", ".join(approvals.get("missing") or []) or "none"
                typer.echo(
                    f"  approvals: {approvals.get('approved')}/{approvals.get('required')} approved "
                    f"(level {approvals.get('level')}); missing: {missing}"
                )
        _echo_market_doctor(market_report)
        installed = installer.installed_harnesses()
        total = len(
            [
                name
                for name in ("claude_code", "codex", "deepseek", "antigravity", "zcode")
                if name in config.harnesses
            ]
        )
        typer.echo(f"{len(installed)} of {total} harnesses installed")
    if any(result.status == "failed" for result in results):
        raise typer.Exit(code=1)


@app.command()
def status(
    ctx: typer.Context,
    refresh: bool = typer.Option(False, "--refresh", help="Refresh model discovery before reporting."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show harness availability, authorised models and quota state."""

    runtime = _runtime(ctx)
    _ensure_market(runtime)
    try:
        statuses = asyncio.run(runtime.router.list_harnesses(refresh_models=refresh))
    finally:
        runtime.storage.close()
    payload = [item.model_dump(mode="json") for item in statuses]
    if json_output:
        typer.echo(_json(payload))
        return
    for item in statuses:
        if not item.installed:
            typer.echo(
                f"{item.harness:14} not installed  {item.version_or_detail or 'binary not found'}"
            )
            continue
        state = "available" if item.available else "missing"
        models = ", ".join(item.authorized_models) or "(none discovered)"
        typer.echo(f"{item.harness:14} {state:10} quota={item.quota_state:9} mode={item.mode}")
        typer.echo(f"  binary: {item.binary_path or '-'}  {item.version_or_detail}")
        typer.echo(f"  models: {models}")
        for detail in item.model_details:
            tiers = ",".join(detail.get("assigned_tiers") or []) or "-"
            price = detail.get("blended_price")
            typer.echo(
                f"    {detail['model_id']}  intel={detail.get('intelligence')} "
                f"intel_source={detail.get('intelligence_source')} "
                f"blended={_price(price)} tiers={tiers}"
            )
        if item.tier_models:
            typer.echo("  tiers : " + ", ".join(f"{tier}={model}" for tier, model in item.tier_models.items()))


@app.command()
def models(
    ctx: typer.Context,
    refresh: bool = typer.Option(False, "--refresh", help="Force the three-layer discovery waterfall."),
    harness: Optional[str] = typer.Option(None, "--harness", "-H", help="Limit discovery to one harness."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Discover current authorised models and difficulty-tier bindings."""

    runtime = _runtime(ctx)
    _ensure_market(runtime)
    try:
        payload = asyncio.run(
            runtime.router.discover_harness_models(harness=harness, force_refresh=refresh)
        )
    finally:
        runtime.storage.close()
    if json_output:
        typer.echo(_json(payload))
        return
    if harness:
        entries = {harness: payload}
    else:
        entries = payload
    for name, item in entries.items():
        typer.echo(f"[{name}]")
        if item.get("installed") is False:
            reason = item.get("reason") or item.get("error") or "binary not found"
            typer.echo(f"  not installed ({reason})")
            continue
        if item.get("error"):
            typer.echo(f"  error: {item['error']}")
        for model in item.get("models", []):
            tiers = ",".join(model.get("assigned_tiers") or []) or "-"
            authorized = "yes" if model.get("authorized") else "no"
            typer.echo(
                f"  {model.get('model_id')}  score={model.get('capability_score')} "
                f"intel={model.get('intelligence')} "
                f"intel_source={model.get('intelligence_source')} "
                f"blended={_price(model.get('blended_price'))} "
                f"source={model.get('source')} authorized={authorized} tiers={tiers}"
            )
        tiers = item.get("tier_models") or {}
        if tiers:
            typer.echo("  tier map: " + ", ".join(f"{tier}={model}" for tier, model in tiers.items()))


@app.command()
def quota(
    ctx: typer.Context,
    probe: bool = typer.Option(False, "--probe", help="Probe open circuits now and resume waiting tickets."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Inspect quota circuits and the waiting_for_quota queue."""

    runtime = _runtime(ctx)
    try:
        payload = asyncio.run(runtime.router.get_quota_status(probe_now=probe))
    finally:
        runtime.storage.close()
    if json_output:
        typer.echo(_json(payload))
        return
    circuits = payload.get("circuits", [])
    if not circuits:
        typer.echo("All quota circuits are CLOSED.")
    for circuit in circuits:
        typer.echo(
            f"{circuit.get('harness')}/{circuit.get('model')}: state={circuit.get('state')} "
            f"reason={circuit.get('failure_reason') or '-'} probes={circuit.get('probe_count', 0)}"
        )
    waiting = payload.get("waiting_for_quota", [])
    typer.echo(f"waiting tickets: {len(waiting)}")
    for task in waiting:
        typer.echo(
            f"  {task.get('task_id')} blocked_on={task.get('blocked_harness')}/{task.get('blocked_model')}"
        )
    for outcome in payload.get("recovery_probe_results", []):
        typer.echo(f"  probe {outcome.get('harness')}/{outcome.get('model')}: {outcome}")


@app.command()
def skills(
    ctx: typer.Context,
    query: str = typer.Option("", "--query", "-q", help="Filter skills/plugins by keyword."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List indexed SKILL.md files and MCP/plugin manifests."""

    runtime = _runtime(ctx)
    try:
        payload = runtime.router.list_skills_and_plugins(query=query)
    finally:
        runtime.storage.close()
    if json_output:
        typer.echo(_json(payload))
        return
    typer.echo(f"skills ({len(payload.get('skills', []))}):")
    for item in payload.get("skills", []):
        typer.echo(f"  {item.get('name')}  {item.get('path')}")
        if item.get("description"):
            typer.echo(f"    {item.get('description')[:160]}")
    typer.echo(f"plugins ({len(payload.get('plugins', []))}):")
    for item in payload.get("plugins", []):
        typer.echo(f"  {item.get('name')}  {item.get('command') or item.get('path')}")


@app.command()
def dispatch(
    ctx: typer.Context,
    objective: str = typer.Option(..., "--objective", "-o", help="Task objective."),
    harness: str = typer.Option("auto", "--harness", "-H", help="Target harness or 'auto'."),
    difficulty: str = typer.Option("auto", "--difficulty", "-d", help="auto|low|medium|high|critical."),
    kind: str = typer.Option("auto", "--kind", "-k", help="auto|plan|audit|review|implement|refactor|debug|test|docs."),
    routing_mode: Optional[str] = typer.Option(
        None,
        "--routing-mode",
        help="auto|manual|off. Omit to use routing.mode or HARNESS_MESH_ROUTING.",
    ),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    policy: str = typer.Option("failover", "--policy", help="failover|wait_and_resume|fail."),
    fallback: Optional[list[str]] = typer.Option(None, "--fallback", help="Preferred failover harness (repeatable)."),
    timeout: Optional[int] = typer.Option(None, "--timeout", help="Assignment timeout in seconds."),
    workspace_mode: str = typer.Option("isolated_worktree", "--workspace-mode"),
    context_file: Optional[list[str]] = typer.Option(None, "--context-file", help="Context file path (repeatable)."),
    auto_match_skills: bool = typer.Option(False, "--auto-match-skills"),
    call_depth: Optional[int] = typer.Option(
        None,
        "--call-depth",
        help="Nested call depth; only raises the depth inherited from the environment or the ledger.",
    ),
    trace_chain: Optional[str] = typer.Option(
        None,
        "--trace-chain",
        help="JSON list of ancestor harnesses, merged with the environment and the ledger chain.",
    ),
    json_output: bool = typer.Option(True, "--json/--no-json"),
) -> None:
    """Dispatch a ticket and print the assignment result."""

    # The command line is a reachable entry point for a worker shell, so it
    # resolves lineage from the same ledger as the MCP server instead of
    # trusting --call-depth or the (host-controlled) environment alone.
    runtime = _runtime(ctx)
    _ensure_market(runtime)
    if runtime.router.market is not None:
        # The one-shot refresh above already spent the dispatch budget.
        runtime.router.market.skip_next_dispatch_wait = True
    try:
        lineage = _resolved_lineage(runtime.router)
        inherited_depth = lineage.call_depth if lineage is not None else 0
        inherited_trace = list(lineage.trace_chain) if lineage is not None else []
        request = TaskRequest(
            target_harness=harness,
            objective=objective,
            difficulty=TaskDifficulty(difficulty),
            task_kind=_task_kind_value(kind),
            model=model,
            on_quota_exhausted=QuotaExhaustedPolicy(policy),
            fallback_harnesses=list(fallback or []),
            timeout_seconds=timeout,
            workspace_mode=WorkspaceMode(workspace_mode),
            workspace_root=str(runtime.router.workspace_root),
            context_files=list(context_file or []),
            auto_match_skills=auto_match_skills,
            call_depth=_effective_call_depth(call_depth, inherited_depth),
            trace_chain=_merge_trace_chain(_trace_chain_option(trace_chain), inherited_trace),
            parent_task_id=os.environ.get("HARNESS_MESH_TASK_ID"),
        )
        if routing_mode is not None:
            request.routing_mode = _routing_mode_value(routing_mode)
        result = asyncio.run(runtime.router.dispatch(request))
    finally:
        runtime.storage.close()
    if json_output:
        typer.echo(_json(result))
    else:
        typer.echo(f"{result.task_id}: {result.status.value} on {result.target_harness}")
        typer.echo(result.summary)


@app.command()
def tasks(
    ctx: typer.Context,
    status_filter: Optional[str] = typer.Option(None, "--status", "-s"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List tickets in the state ledger."""

    runtime = _runtime(ctx)
    try:
        rows = runtime.router.list_tasks(status=status_filter)
    finally:
        runtime.storage.close()
    if json_output:
        typer.echo(_json(rows))
        return
    if not rows:
        typer.echo("No tickets.")
    for row in rows:
        typer.echo(
            f"{row['task_id']}  {row['status']:18} {row.get('initial_harness') or '-':12} "
            f"worktree={row.get('worktree_path') or '-'}"
        )


@app.command("read")
def read_resource(
    ctx: typer.Context,
    task_id: str = typer.Argument(...),
    resource: str = typer.Argument("diff", help="diff | transcript | assignments"),
    offset: int = typer.Option(0, "--offset"),
    limit: int = typer.Option(4000, "--limit"),
) -> None:
    """Page through a task resource (diff/transcript/assignments)."""

    runtime = _runtime(ctx)
    try:
        page = runtime.router.read_task_resource(task_id, resource=resource, offset=offset, limit=limit)
    finally:
        runtime.storage.close()
    typer.echo(page.get("content", ""))
    typer.echo(
        f"\n[offset={page.get('offset')} limit={page.get('limit')} "
        f"total={page.get('total_length')} has_more={page.get('has_more')} "
        f"next_offset={page.get('next_offset')}]",
        err=True,
    )


@app.command()
def apply(
    ctx: typer.Context,
    task_id: str = typer.Argument(...),
    strategy: str = typer.Option("patch", "--strategy", help="patch|merge|copy"),
    force_copy: bool = typer.Option(
        False, "--force-copy", help="Required to use the destructive copy strategy."
    ),
) -> None:
    """Apply isolated worktree changes to the primary workspace."""

    if strategy == "copy" and not force_copy:
        typer.echo("copy strategy is destructive; pass --force-copy to confirm", err=True)
        raise typer.Exit(code=2)
    if strategy not in {"patch", "merge", "copy"}:
        typer.echo(f"unsupported strategy: {strategy}", err=True)
        raise typer.Exit(code=2)
    runtime = _runtime(ctx)
    try:
        payload = runtime.router.apply_task_patch(task_id, strategy=strategy)
    finally:
        runtime.storage.close()
    typer.echo(_json(payload))


@app.command("init-mcp")
def init_mcp(
    ctx: typer.Context,
    target: Optional[str] = typer.Option(None, "--target", help="Write a .mcp.json file at this path."),
    print_config: bool = typer.Option(False, "--print", help="Print the MCP server configuration."),
    command: str = typer.Option("hmesh-mcp", "--command", help="MCP server command."),
) -> None:
    """Emit or install the MCP server entry for a controller harness."""

    options = ctx.obj or {}
    workspace = options.get("workspace") or _workspace_from_cwd()
    config = {
        "mcpServers": {
            "harness-mesh": {
                "command": command,
                "args": [],
                "env": {"HARNESS_MESH_WORKSPACE": str(workspace)},
            }
        }
    }
    if target:
        path = Path(target).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        typer.echo(f"wrote {path}")
        return
    typer.echo(json.dumps(config, indent=2, ensure_ascii=False))


@context_app.command("list")
def context_list(
    ctx: typer.Context,
    category: Optional[str] = typer.Option(None, "--category", "-c", help="Filter by context category."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List shared project facts and the scanned instruction files."""

    runtime = _runtime(ctx)
    try:
        payload = runtime.router.get_project_context(
            category=category, include_instructions=True
        )
    finally:
        runtime.storage.close()
    if json_output:
        typer.echo(_json(payload))
        return
    entries = payload.get("entries", [])
    typer.echo(f"context entries: {len(entries)}")
    for entry in entries:
        typer.echo(
            f"  [{entry.get('category')}] {entry.get('key')} "
            f"(author={entry.get('author_harness')}, task={entry.get('task_id') or '-'})"
        )
        typer.echo(f"    {str(entry.get('content') or '').splitlines()[0][:240]}")
    instructions = payload.get("instructions", [])
    if instructions:
        typer.echo("project instruction files:")
        for item in instructions:
            typer.echo(f"  {item.get('path')} ({len(item.get('content') or '')} chars)")


@context_app.command("set")
def context_set(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Stable context key, e.g. arch.overview."),
    content: str = typer.Argument(..., help="Fact, interface contract or decision text."),
    category: str = typer.Option(
        "note",
        "--category",
        "-c",
        help="architecture|convention|api_contract|decision|pitfall|note",
    ),
    author_harness: str = typer.Option("controller", "--author", help="Harness or controller author."),
    task_id: Optional[str] = typer.Option(None, "--task-id"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Write or update one shared project context entry."""

    runtime = _runtime(ctx)
    try:
        payload = runtime.router.update_project_context(
            key,
            content,
            category=category,
            author_harness=author_harness,
            task_id=task_id,
        )
    finally:
        runtime.storage.close()
    if json_output:
        typer.echo(_json(payload))
        return
    typer.echo(f"saved [{payload.get('category')}] {payload.get('key')}")


def _price(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


@market_app.command("refresh")
def market_refresh(
    ctx: typer.Context,
    source: Optional[list[str]] = typer.Option(
        None, "--source", "-s", help="Refresh only this source (repeatable)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Fetch the configured market sources and update the local snapshot cache."""

    runtime = _runtime(ctx)
    try:
        result = asyncio.run(
            runtime.router.market.refresh(force=True, sources=source)
        )
    finally:
        runtime.storage.close()
    if json_output:
        typer.echo(_json(result))
        return
    for status in result.statuses:
        detail = f" error={status.error}" if status.error else ""
        typer.echo(
            f"{status.source:20} {status.status:12} models={status.models}{detail}"
        )
    catalog = result.catalog
    typer.echo(
        f"catalog: {len(catalog.models)} models from "
        f"{', '.join(catalog.sources) or '-'} stale={catalog.stale}"
    )
    for warning in catalog.warnings:
        typer.echo(f"warning: {warning}")


@market_app.command("show")
def market_show(
    ctx: typer.Context,
    model: Optional[str] = typer.Option(
        None, "--model", "-m", help="Filter by a model glob (matched against key and name)."
    ),
    axis: Optional[str] = typer.Option(
        None, "--axis", help="reasoning|coding: show that axis and sort by it."
    ),
    unmatched: bool = typer.Option(
        False, "--unmatched", help="List harness models that have no market match."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show the cached market catalogue and optional unmatched harness models."""

    axis_name = (axis or "").strip().lower() or None
    if axis_name is not None and axis_name not in {"reasoning", "coding"}:
        raise typer.BadParameter(
            "expected one of: reasoning, coding", param_hint="--axis"
        )
    runtime = _runtime(ctx)
    _ensure_market(runtime)
    ledger_hint: str | None = None
    catalog_empty = True
    try:
        catalog = runtime.router.market.catalog()
        catalog_empty = not catalog.models
        rows = filter_market_models(catalog.models, model)
        if axis_name is not None:
            field = f"intelligence_{axis_name}"
            rows = sorted(
                rows,
                key=lambda item: (
                    getattr(item, field) is None,
                    -(getattr(item, field) or 0.0),
                    item.key,
                ),
            )
        payload: dict[str, Any] = {
            "models": [item.model_dump(mode="json") for item in rows],
            "matched": len(rows),
            "axis": axis_name,
            "sources": list(catalog.sources),
            "fetched_at": catalog.fetched_at,
            "stale": catalog.stale,
            "expired": catalog.expired,
            "warnings": list(catalog.warnings),
            "attribution": EPOCH_ATTRIBUTION,
        }
        if unmatched:
            ledger = runtime.storage.list_discovered_models()
            if not ledger:
                payload["unmatched"] = []
                ledger_hint = (
                    "No harness models are in the discovery ledger yet; "
                    "run `hmesh models --refresh` first."
                )
            else:
                inventory: dict[str, list[str]] = {}
                for found in ledger:
                    inventory.setdefault(found.harness, []).append(found.model_id)
                payload["unmatched"] = list_unmatched(
                    inventory,
                    catalog.models,
                    aliases=runtime.config.market.aliases,
                )
    finally:
        runtime.storage.close()

    if json_output:
        typer.echo(_json(payload))
        return
    if catalog_empty:
        typer.echo("No market data is cached. Run `hmesh market refresh`.")
    elif model and not rows:
        typer.echo(f"no model matches {model}")
    for item in rows:
        axis_column = ""
        if axis_name is not None:
            value = getattr(item, f"intelligence_{axis_name}")
            axis_column = f" {axis_name}={value if value is not None else '-':>5}"
        typer.echo(
            f"{item.key:48} in={_price(item.input_per_mtok):>8} "
            f"out={_price(item.output_per_mtok):>8} "
            f"intel={item.intelligence if item.intelligence is not None else '-':>5} "
            f"source={item.intelligence_source or '-'} "
            f"bench={len(item.benchmarks_used)} "
            f"data={item.benchmark_data_date or '-'} "
            f"ctx={item.context_window or '-'}{axis_column}"
        )
    typer.echo(EPOCH_ATTRIBUTION)
    if payload["stale"]:
        typer.echo("cache is stale; refresh it when the network is available")
    if unmatched:
        entries = payload["unmatched"]
        if ledger_hint:
            typer.echo(ledger_hint)
        elif not entries:
            typer.echo("No unmatched harness models.")
        for entry in entries:
            effort = f" effort={entry['effort_variant']}" if entry["effort_variant"] else ""
            typer.echo(
                f"unmatched {entry['harness']:14} {entry['model_id']}{effort} "
                f"confidence={entry.get('confidence', 'none')} -> add market.aliases"
            )


@app.command()
def route(
    ctx: typer.Context,
    objective: str = typer.Option(..., "--objective", "-o", help="Task objective to preview."),
    kind: str = typer.Option("auto", "--kind", "-k", help="auto|plan|audit|review|implement|refactor|debug|test|docs."),
    difficulty: str = typer.Option("auto", "--difficulty", "-d", help="auto|low|medium|high|critical."),
    harness: str = typer.Option("auto", "--harness", "-H", help="Target harness or 'auto'."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Preview the model routing decision without dispatching a ticket."""

    task_kind = _task_kind_value(kind)
    try:
        level = TaskDifficulty(str(difficulty).lower())
    except ValueError as exc:
        raise typer.BadParameter(
            "expected one of: " + ", ".join(item.value for item in TaskDifficulty),
            param_hint="--difficulty",
        ) from exc
    runtime = _runtime(ctx)
    _ensure_market(runtime)
    try:
        payload = asyncio.run(
            runtime.router.preview_routing(
                objective,
                task_kind=task_kind.value,
                difficulty=level.value,
                target_harness=harness,
            )
        )
    finally:
        runtime.storage.close()

    if json_output:
        typer.echo(_json(payload))
        return
    if payload.get("error"):
        typer.echo(f"error: {payload['error']}", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        f"task_kind={payload['task_kind']} tier={payload['tier']} "
        f"candidates={payload['candidate_count']} market_models={payload['market_models']}"
    )
    routing = payload.get("routing") or {}
    chosen = routing.get("chosen")
    if chosen:
        typer.echo(
            f"route: {chosen['harness']}/{chosen['model']} "
            f"effort={chosen.get('effort') or 'default'} "
            f"intelligence={chosen.get('intelligence')} "
            f"source={chosen.get('intelligence_source')} "
            f"blended_price={chosen.get('blended_price')}"
        )
    else:
        typer.echo("no routed choice; dispatch would use the existing difficulty tier")
    for alternative in routing.get("alternatives") or []:
        typer.echo(
            f"  alternative {alternative['harness']}/{alternative['model']} "
            f"score={alternative['score']} intelligence={alternative['intelligence']}"
        )
    for reason in routing.get("reasons") or []:
        typer.echo(f"  - {reason}")
    typer.echo(routing.get("attribution") or EPOCH_ATTRIBUTION)


@app.command()
def version() -> None:
    """Print the harness-mesh version."""

    from . import __version__

    typer.echo(f"harness-mesh {__version__}")


if __name__ == "__main__":  # pragma: no cover
    app()
