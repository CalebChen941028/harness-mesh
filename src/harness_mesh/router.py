"""MeshRouter: ticket/assignment lifecycle, failover and quota recovery."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .adapters import AdapterCall, AdapterOutcome, BaseHarnessAdapter, build_adapters
from .config import (
    MeshConfig,
    build_handoff_prompt,
    cap_text,
    evaluate_difficulty,
    load_config,
)
from .context import ProjectContextManager
from .discovery import ModelDiscoveryEngine
from .models import (
    AssignmentRecord,
    ContinuationCheckpoint,
    DiscoveredModel,
    FailoverHop,
    HarnessStatus,
    QuotaExhaustedPolicy,
    TaskDifficulty,
    TaskRequest,
    TaskResult,
    TaskStatus,
    WorkspaceMode,
    utc_now,
)
from .quota import QuotaErrorDetector, QuotaGuardian, QuotaRecoveryMonitor
from .security import WorkspaceRootError, ensure_service_workspace, resolve_allowed_root
from .skills import SkillPluginManager
from .storage import StateStore, TaskNotFoundError
from .workspace import Changes, WorkspaceError, WorkspaceManager


@dataclass
class MeshRuntime:
    """Convenience container wiring every subsystem together."""

    config: MeshConfig
    storage: StateStore
    discovery: ModelDiscoveryEngine
    skills: SkillPluginManager
    context: ProjectContextManager
    workspace: WorkspaceManager
    adapters: dict[str, BaseHarnessAdapter]
    guardian: QuotaGuardian
    monitor: QuotaRecoveryMonitor
    router: "MeshRouter"

    async def start_monitor(self) -> None:
        """Start quota probing and the durable-effects consumer."""

        await self.monitor.start()
        await self.router.start_effects_worker()

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self.monitor.stop()
        with contextlib.suppress(Exception):
            await self.router.stop_effects_worker()
        self.storage.close()


class MeshRouter:
    def __init__(
        self,
        config: MeshConfig,
        storage: StateStore,
        *,
        discovery: ModelDiscoveryEngine | None = None,
        skills: SkillPluginManager | None = None,
        context: ProjectContextManager | None = None,
        workspace: WorkspaceManager | None = None,
        adapters: Mapping[str, Any] | None = None,
        guardian: QuotaGuardian | None = None,
        monitor: QuotaRecoveryMonitor | None = None,
        workspace_root: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        home_dir: str | Path | None = None,
        probe: Any | None = None,
        now_fn: Any | None = None,
        sleep_fn: Any | None = None,
    ):
        self.config = config
        self.storage = storage
        self.workspace_root = Path(workspace_root or Path.cwd()).expanduser().resolve()
        if not self.config.allowed_workspace_roots:
            self.config.allowed_workspace_roots = [str(self.workspace_root)]
        self.env = dict(env) if env is not None else None
        self.home_dir = home_dir
        self.now_fn = now_fn or time.monotonic
        self.sleep_fn = sleep_fn or asyncio.sleep

        self.workspace = workspace or WorkspaceManager(
            self.workspace_root, config.state_dir, auto_init_repo=config.auto_init_repo
        )
        self.discovery = discovery or ModelDiscoveryEngine(
            config,
            storage,
            self.workspace_root,
            home_dir=home_dir,
            env=self.env,
        )
        self.skills = skills or SkillPluginManager(
            config,
            self.workspace_root,
            home_dir=home_dir,
            env=self.env,
        )
        self.context = context or ProjectContextManager(
            storage, self.workspace_root, valid_harnesses=self.config.harnesses.keys()
        )
        self.adapters = dict(adapters) if adapters is not None else build_adapters(
            config,
            self.workspace_root,
            env=self.env,
        )
        self.guardian = guardian or QuotaGuardian(
            storage,
            cooldown_seconds=config.quota_cooldown_seconds,
            failover_priority=config.failover_priority,
        )
        self.monitor = monitor or QuotaRecoveryMonitor(
            self.guardian,
            probe=probe or self._default_probe,
            on_quota_recovered=self.on_quota_recovered,
            interval_seconds=config.quota_probe_interval_seconds,
        )
        if self.monitor.on_quota_recovered is None:
            self.monitor.on_quota_recovered = self.on_quota_recovered

        self._running_tasks: dict[str, asyncio.Task] = {}
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._resuming: set[str] = set()
        self._workspace_cache: dict[str, WorkspaceManager] = {
            str(self.workspace.workspace_root): self.workspace
        }
        self._last_discovery_errors: dict[str, str] = {}
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self._effects_task: asyncio.Task | None = None
        self._effects_stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # public entry points
    # ------------------------------------------------------------------
    async def dispatch(self, request: TaskRequest | Mapping[str, Any]) -> TaskResult:
        if not isinstance(request, TaskRequest):
            request = TaskRequest.model_validate(request)
        current = asyncio.current_task()
        active = self._running_tasks.get(request.task_id)
        if active is not None and not active.done():
            return self._duplicate_task_result(request)
        lock = self._task_locks.setdefault(request.task_id, asyncio.Lock())
        if current is not None:
            self._running_tasks[request.task_id] = current
        try:
            async with lock:
                return await self._dispatch(request)
        except asyncio.CancelledError:
            self._persist_cancelled(request.task_id)
            raise
        except Exception as exc:  # never leak an exception to a controller harness
            return self._unexpected_failure(request, exc)
        finally:
            if current is not None and self._running_tasks.get(request.task_id) is current:
                self._running_tasks.pop(request.task_id, None)
            self._task_locks.pop(request.task_id, None)

    async def dispatch_task(self, request: TaskRequest | Mapping[str, Any]) -> TaskResult:
        return await self.dispatch(request)

    async def parallel_dispatch(
        self, tasks: Sequence[TaskRequest | Mapping[str, Any]], max_concurrency: int = 8
    ) -> list[TaskResult]:
        requests = [task if isinstance(task, TaskRequest) else TaskRequest.model_validate(task) for task in tasks]
        semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))

        async def run(item: TaskRequest) -> TaskResult:
            async with semaphore:
                return await self.dispatch(item)

        results = await asyncio.gather(*(run(item) for item in requests), return_exceptions=True)
        normalized: list[TaskResult] = []
        for request, result in zip(requests, results):
            if isinstance(result, BaseException):
                normalized.append(self._unexpected_failure(request, result))
            else:
                normalized.append(result)
        return normalized

    # ------------------------------------------------------------------
    # main dispatch pipeline
    # ------------------------------------------------------------------
    async def _dispatch(self, request: TaskRequest) -> TaskResult:
        started = self.now_fn()
        target = request.target_harness.strip()
        trace_chain = list(request.trace_chain)

        rejection = self._check_recursion(target, request, trace_chain)
        if rejection is not None:
            return self._rejected_result(request, rejection, started)

        if target != "auto" and target not in self.config.harnesses:
            return self._rejected_result(
                request, f"unknown target_harness '{target}' (known: {list(self.config.harnesses)})", started
            )

        try:
            root = resolve_allowed_root(
                request.workspace_root or self.workspace_root,
                self.config.allowed_workspace_roots,
            )
        except WorkspaceRootError as exc:
            return self._rejected_result(request, str(exc), started)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._rejected_result(request, f"workspace is not writable: {exc}", started)

        ws = self._workspace_for(root)
        mode = (
            request.workspace_mode
            if isinstance(request.workspace_mode, WorkspaceMode)
            else WorkspaceMode(request.workspace_mode)
        )
        if mode in {WorkspaceMode.ISOLATED_WORKTREE, WorkspaceMode.READ_ONLY}:
            # READ_ONLY also runs in a disposable worktree so a misbehaving
            # agent can never mutate the primary workspace; its changes are
            # never collected or applied.
            lock = self._workspace_locks.setdefault(str(root), asyncio.Lock())
            try:
                async with lock:
                    info = ws.create_worktree(
                        request.task_id,
                        sync_uncommitted=request.sync_uncommitted,
                    )
            except WorkspaceError as exc:
                return self._rejected_result(request, f"failed to create worktree: {exc}", started)
            workdir = info.path
            worktree_path = str(info.path)
            worktree_branch = info.branch
        else:
            workdir = root
            worktree_path = None
            worktree_branch = None

        project_instructions = (
            self.context.format_instructions(root) if request.include_project_context else ""
        )
        project_memory = self.context.format_memory() if request.include_project_context else ""
        context_previews = self.context.render_context_file_previews(request.context_files, root)
        upstream_outputs = self._build_upstream_outputs(
            request, root, worktree_path, ws, mode
        )

        difficulty = (
            evaluate_difficulty(request.objective, request.context_files, request.context_notes, request.target_harness)
            if request.difficulty == TaskDifficulty.AUTO
            else request.difficulty
        )
        effort = request.reasoning_effort or self.config.effort_for(difficulty)

        previous = self._existing_record(request.task_id)
        existing_attempts = len(self.storage.list_assignments(request.task_id))
        if existing_attempts:
            # A previous process may have died mid-assignment; keep the ticket
            # resumable but close the stale attempt before adding a new one.
            for stale in self.storage.list_assignments(request.task_id):
                if stale.status in {"pending", "running"}:
                    self.storage.update_assignment(
                        stale.assignment_id,
                        status="ended_error",
                        ended_at=utc_now(),
                        error_reason="interrupted_before_resume",
                    )

        self.storage.create_task(
            request,
            status=TaskStatus.RUNNING,
            initial_harness=target if target != "auto" else None,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            checkpoint=request.checkpoint,
        )
        self.storage.enqueue_effect(
            "launch",
            request.task_id,
            {"target_harness": target, "difficulty": difficulty.value},
        )

        resumed_checkpoint = request.checkpoint or (previous.get("checkpoint") if previous else None)

        try:
            skills_selected, plugins_selected = self.skills.resolve_for_task(request)
        except Exception:
            skills_selected, plugins_selected = [], []
        try:
            mcp_config_path = self.skills.compile_mcp_config(
                request.task_id, plugins_selected
            ) if plugins_selected else None
        except Exception:
            mcp_config_path = None
        active_plugin_names: list[str] = []
        try:
            skills_prompt = self.skills.build_skills_prompt(skills_selected)
        except Exception:
            skills_prompt = ""

        selection = await self._select_initial(
            request, root, target, difficulty, workdir
        )
        if selection is None:
            return self._terminal_result(
                request,
                status=TaskStatus.FAILED,
                initial_harness=target,
                current_harness=target,
                current_model=request.model or "",
                difficulty=difficulty,
                effort=effort,
                summary="No authorised, quota-healthy model was available for this ticket.",
                error_detail="no_available_model",
                workdir=workdir,
                worktree_path=worktree_path,
                worktree_branch=worktree_branch,
                started=started,
                skills=[s.name for s in skills_selected],
                plugins=list(active_plugin_names),
            )
        current_harness, current_model, selected_model = selection
        if current_harness in trace_chain:
            if worktree_path:
                with contextlib.suppress(Exception):
                    ws.remove_worktree(request.task_id)
            return self._rejected_result(
                request,
                "recursive harness cycle rejected after resolution: "
                + " -> ".join([*trace_chain, current_harness]),
                started,
            )
        if target == "auto":
            target = current_harness
        self.storage.update_task(
            request.task_id,
            initial_harness=target,
            current_harness=current_harness,
            current_model=current_model,
            resolved_difficulty=difficulty.value,
            resolved_model=current_model,
            resolved_effort=effort,
            blocked_harness=None,
            blocked_model=None,
        )

        configured_attempts = (
            int(request.max_failover_attempts)
            if "max_failover_attempts" in request.model_fields_set
            else int(self.config.max_failover_attempts)
        )
        max_attempts = max(1, configured_attempts + 1)
        if request.on_quota_exhausted == QuotaExhaustedPolicy.WAIT_AND_RESUME:
            # Suspended tickets are not failed attempts: every resume must have
            # at least one fresh assignment to try, regardless of how often the
            # probe has already interrupted it.
            max_attempts = max(max_attempts, existing_attempts + 1)
        exclude_pairs: set[tuple[str, str]] = set()
        transient_count = 0
        checkpoint: ContinuationCheckpoint | None = resumed_checkpoint
        last_outcome: AdapterOutcome | None = None
        last_error = ""
        transcripts: list[dict[str, Any]] = []
        success = False
        replans = 0

        while existing_attempts < max_attempts:
            if replans > max_attempts + 3:
                last_error = last_error or "candidate replanning did not converge"
                break
            if not self._adapter_runnable(current_harness) or not current_model:
                pair = await self._pick_failover(
                    request, root, current_harness, current_model or "", exclude_pairs, difficulty
                )
                if pair is None:
                    break
                current_harness, current_model = pair
                replans += 1
                continue
            if not self.guardian.is_available(current_harness, current_model):
                pair = await self._pick_failover(
                    request, root, current_harness, current_model, exclude_pairs, difficulty
                )
                if pair is None:
                    break
                current_harness, current_model = pair
                replans += 1
                continue

            attempt_no = existing_attempts + 1
            assignment = AssignmentRecord(
                task_id=request.task_id,
                attempt=attempt_no,
                harness=current_harness,
                model=current_model,
                status="running",
                started_at=utc_now(),
            )
            self.storage.add_assignment(assignment)
            prior_history = self.storage.list_assignments(request.task_id)
            prior_history = [item for item in prior_history if item.assignment_id != assignment.assignment_id]
            include_checkpoint = checkpoint if checkpoint is not None else None
            prompt = build_handoff_prompt(
                request,
                include_checkpoint,
                next_harness=current_harness,
                next_model=current_model,
                skills_prompt=skills_prompt,
                assignment_history=prior_history,
                reasoning_effort=effort,
                project_instructions=project_instructions,
                project_memory=project_memory,
                context_previews=context_previews,
                upstream_outputs=upstream_outputs,
            )
            adapter = self.adapters.get(current_harness)
            if adapter is None:
                self.storage.update_assignment(
                    assignment.assignment_id, status="ended_error", ended_at=utc_now(), error_reason="adapter_missing"
                )
                last_error = f"adapter missing for {current_harness}"
                exclude_pairs.add((current_harness, current_model))
                existing_attempts += 1
                continue

            timeout_seconds = int(
                request.timeout_seconds
                or self.config.timeout_for(difficulty)
                or self.config.default_timeout_seconds
            )
            adapter_supports_mcp = bool(getattr(adapter, "supports_mcp_config", True))
            call_mcp_path = mcp_config_path if adapter_supports_mcp else None
            call_plugins: list[str] = []
            if adapter_supports_mcp and call_mcp_path is not None:
                for plugin in plugins_selected:
                    if plugin.name not in active_plugin_names:
                        active_plugin_names.append(plugin.name)
                call_plugins = [plugin.name for plugin in plugins_selected]
            call = AdapterCall(
                task_id=request.task_id,
                assignment_id=assignment.assignment_id,
                prompt=prompt,
                workdir=workdir,
                model=current_model,
                system_prompt=skills_prompt,
                reasoning_effort=effort,
                mcp_config_path=call_mcp_path,
                plugins=call_plugins,
                timeout_seconds=timeout_seconds,
                env={},
                trace_chain=[*trace_chain, current_harness],
                call_depth=request.call_depth + 1,
                read_only=(mode == WorkspaceMode.READ_ONLY),
                shared_write=(mode == WorkspaceMode.SHARED_WRITE),
                session_id=request.session_id,
                skills=[skill.name for skill in skills_selected],
                task=request,
            )
            call_started = self.now_fn()
            try:
                raw_outcome = await asyncio.wait_for(
                    adapter.execute(call), timeout=max(1.0, float(timeout_seconds)) + 1.0
                )
            except asyncio.TimeoutError:
                raw_outcome = AdapterOutcome(
                    exit_code=124,
                    timed_out=True,
                    stderr=f"[harness-mesh] assignment timed out after {timeout_seconds}s",
                    error_detail="timeout",
                )
            except Exception as exc:
                raw_outcome = AdapterOutcome(
                    exit_code=1,
                    stderr=str(exc),
                    error_detail=f"adapter_exception: {exc}",
                )
            outcome = self._normalize_outcome(raw_outcome)
            if outcome.duration_ms <= 0:
                outcome.duration_ms = int((self.now_fn() - call_started) * 1000)
            existing_attempts += 1
            last_outcome = outcome
            transcripts.append(
                {
                    "attempt": attempt_no,
                    "assignment_id": assignment.assignment_id,
                    "harness": current_harness,
                    "model": current_model,
                    "exit_code": outcome.exit_code,
                    "timed_out": outcome.timed_out,
                    "stdout": outcome.stdout,
                    "stderr": outcome.stderr,
                    "session_id": outcome.session_id,
                }
            )

            if outcome.timed_out:
                checkpoint = self._capture_checkpoint(
                    ws, request, workdir, current_harness, current_model,
                    "timeout", outcome.stdout, assignment.assignment_id, mode,
                )
                self.storage.update_assignment(
                    assignment.assignment_id,
                    status="ended_error",
                    ended_at=utc_now(),
                    error_reason="timeout",
                    duration_ms=outcome.duration_ms,
                    exit_code=outcome.exit_code,
                )
                return self._terminal_result(
                    request,
                    status=TaskStatus.TIMEOUT,
                    initial_harness=target,
                    current_harness=current_harness,
                    current_model=current_model,
                    difficulty=difficulty,
                    effort=effort,
                    summary=f"Assignment timed out on {current_harness}/{current_model} after {timeout_seconds}s.",
                    error_detail="timeout",
                    workdir=workdir,
                    worktree_path=worktree_path,
                    worktree_branch=worktree_branch,
                    started=started,
                    skills=[s.name for s in skills_selected],
                    plugins=list(active_plugin_names),
                    outcome=outcome,
                    transcripts=transcripts,
                    checkpoint=checkpoint,
                    mode=mode,
                    ws=ws,
                )

            # Error classification only applies to failed assignments. A
            # successful report may legitimately mention "429" or
            # "model_not_found" while describing completed work; treating that
            # as a live quota/auth event would fail or reroute the ticket.
            outcome_failed = outcome.exit_code != 0 or outcome.error_detail is not None
            if outcome_failed:
                is_quota, account_wide, quota_reason, retry_after = QuotaErrorDetector.detect_quota_exhaustion(
                    outcome.stdout, outcome.stderr, outcome.exit_code
                )
                unauthorized, unauthorized_reason = QuotaErrorDetector.detect_unauthorized(
                    outcome.stdout, outcome.stderr, outcome.exit_code
                )
                transient, transient_reason = QuotaErrorDetector.detect_transient(
                    outcome.stdout, outcome.stderr, outcome.exit_code
                )
            else:
                is_quota, account_wide, quota_reason, retry_after = False, False, "", None
                unauthorized, unauthorized_reason = False, ""
                transient, transient_reason = False, ""

            if is_quota:
                last_error = f"quota exhausted: {quota_reason}"
                record = self.guardian.record_exhaustion(
                    current_harness,
                    current_model,
                    quota_reason,
                    is_account_wide=account_wide,
                    retry_after_seconds=retry_after,
                )
                self.storage.enqueue_effect(
                    "quota_probe",
                    f"{current_harness}::{record.model}",
                    {"harness": current_harness, "model": record.model},
                )
                self.storage.update_assignment(
                    assignment.assignment_id,
                    status="ended_quota",
                    ended_at=utc_now(),
                    error_reason=quota_reason,
                    duration_ms=outcome.duration_ms,
                    exit_code=outcome.exit_code,
                    session_id=outcome.session_id,
                )
                checkpoint = self._capture_checkpoint(
                    ws, request, workdir, current_harness, current_model,
                    f"quota_exhausted:{quota_reason}", outcome.stdout, assignment.assignment_id, mode,
                )
                self.storage.update_task(
                    request.task_id,
                    checkpoint_json=checkpoint,
                    blocked_harness=current_harness,
                    blocked_model=current_model,
                )
                policy = request.on_quota_exhausted
                if policy == QuotaExhaustedPolicy.FAIL:
                    return self._terminal_result(
                        request,
                        status=TaskStatus.FAILED,
                        initial_harness=target,
                        current_harness=current_harness,
                        current_model=current_model,
                        difficulty=difficulty,
                        effort=effort,
                        summary=f"Quota exhausted on {current_harness}/{current_model}: {quota_reason}.",
                        error_detail=quota_reason,
                        workdir=workdir,
                        worktree_path=worktree_path,
                        worktree_branch=worktree_branch,
                        started=started,
                        skills=[s.name for s in skills_selected],
                        plugins=list(active_plugin_names),
                        outcome=outcome,
                        transcripts=transcripts,
                        checkpoint=checkpoint,
                        mode=mode,
                        ws=ws,
                    )
                if policy == QuotaExhaustedPolicy.WAIT_AND_RESUME:
                    return self._waiting_result(
                        request,
                        target,
                        current_harness,
                        current_model,
                        difficulty,
                        effort,
                        workdir,
                        worktree_path,
                        worktree_branch,
                        started,
                        skills_selected,
                        active_plugin_names,
                        outcome,
                        transcripts,
                        checkpoint,
                        mode,
                        ws,
                        quota_reason,
                    )
                exclude_pairs.add((current_harness, current_model))
                pair = await self._pick_failover(
                    request, root, current_harness, current_model, exclude_pairs, difficulty
                )
                if pair is None:
                    if self.config.suspend_when_no_candidate:
                        return self._waiting_result(
                            request,
                            target,
                            current_harness,
                            current_model,
                            difficulty,
                            effort,
                            workdir,
                            worktree_path,
                            worktree_branch,
                            started,
                            skills_selected,
                            active_plugin_names,
                            outcome,
                            transcripts,
                            checkpoint,
                            mode,
                            ws,
                            quota_reason,
                        )
                    return self._terminal_result(
                        request,
                        status=TaskStatus.FAILED,
                        initial_harness=target,
                        current_harness=current_harness,
                        current_model=current_model,
                        difficulty=difficulty,
                        effort=effort,
                        summary="All candidate harnesses are quota exhausted.",
                        error_detail="quota_exhausted_all_candidates",
                        workdir=workdir,
                        worktree_path=worktree_path,
                        worktree_branch=worktree_branch,
                        started=started,
                        skills=[s.name for s in skills_selected],
                        plugins=list(active_plugin_names),
                        outcome=outcome,
                        transcripts=transcripts,
                        checkpoint=checkpoint,
                        mode=mode,
                        ws=ws,
                    )
                new_harness, new_model = pair
                hop = FailoverHop(
                    attempt=attempt_no,
                    from_harness=current_harness,
                    from_model=current_model,
                    to_harness=new_harness,
                    to_model=new_model,
                    reason=f"quota_exhausted:{quota_reason}",
                    preserved_files=list(checkpoint.partial_modified_files),
                )
                self.storage.add_failover_hop(request.task_id, hop)
                current_harness, current_model = new_harness, new_model
                continue

            if unauthorized and not is_quota:
                last_error = f"model unauthorized: {unauthorized_reason}"
                if unauthorized_reason == "not_logged_in":
                    # Credentials are harness-wide, not model-scoped: open the
                    # account circuit so failover skips the whole harness.
                    self.guardian.record_exhaustion(
                        current_harness,
                        "*",
                        unauthorized_reason,
                        is_account_wide=True,
                    )
                else:
                    try:
                        self.discovery.mark_model_unauthorized(current_harness, current_model)
                    except Exception:
                        pass
                self.storage.update_assignment(
                    assignment.assignment_id,
                    status="ended_error",
                    ended_at=utc_now(),
                    error_reason=f"model_unauthorized:{unauthorized_reason}",
                    duration_ms=outcome.duration_ms,
                    exit_code=outcome.exit_code,
                    session_id=outcome.session_id,
                )
                exclude_pairs.add((current_harness, current_model))
                if mode == WorkspaceMode.ISOLATED_WORKTREE:
                    checkpoint = self._capture_checkpoint(
                        ws, request, workdir, current_harness, current_model,
                        f"model_unauthorized:{unauthorized_reason}", outcome.stdout,
                        assignment.assignment_id, mode,
                    )
                pair = await self._pick_failover(
                    request, root, current_harness, current_model, exclude_pairs, difficulty
                )
                if pair is not None:
                    new_harness, new_model = pair
                    hop = FailoverHop(
                        attempt=attempt_no,
                        from_harness=current_harness,
                        from_model=current_model,
                        to_harness=new_harness,
                        to_model=new_model,
                        reason=f"model_unauthorized:{unauthorized_reason}",
                        preserved_files=list(checkpoint.partial_modified_files) if checkpoint else [],
                    )
                    self.storage.add_failover_hop(request.task_id, hop)
                    current_harness, current_model = new_harness, new_model
                    continue
                return self._terminal_result(
                    request,
                    status=TaskStatus.FAILED,
                    initial_harness=target,
                    current_harness=current_harness,
                    current_model=current_model,
                    difficulty=difficulty,
                    effort=effort,
                    summary=f"Model {current_model} is no longer authorised and no alternative remains.",
                    error_detail=f"model_unauthorized:{unauthorized_reason}",
                    workdir=workdir,
                    worktree_path=worktree_path,
                    worktree_branch=worktree_branch,
                    started=started,
                    skills=[s.name for s in skills_selected],
                    plugins=list(active_plugin_names),
                    outcome=outcome,
                    transcripts=transcripts,
                    checkpoint=checkpoint,
                    mode=mode,
                    ws=ws,
                )

            if outcome.error_detail or outcome.exit_code != 0:
                last_error = outcome.error_detail or outcome.stderr.strip()[-1000:] or f"exit {outcome.exit_code}"
                self.storage.update_assignment(
                    assignment.assignment_id,
                    status="ended_error",
                    ended_at=utc_now(),
                    error_reason=last_error[:800],
                    duration_ms=outcome.duration_ms,
                    exit_code=outcome.exit_code,
                    session_id=outcome.session_id,
                )
                if transient and transient_count < max(0, int(self.config.transient_retries)):
                    transient_count += 1
                    checkpoint = self._capture_checkpoint(
                        ws, request, workdir, current_harness, current_model,
                        f"transient:{transient_reason}", outcome.stdout, assignment.assignment_id, mode,
                    )
                    await self.sleep_fn(0.25)
                    continue
                checkpoint = self._capture_checkpoint(
                    ws, request, workdir, current_harness, current_model,
                    f"execution_error:{last_error[:200]}", outcome.stdout, assignment.assignment_id, mode,
                )
                exclude_pairs.add((current_harness, current_model))
                if self.config.failover_on_error and request.on_quota_exhausted != QuotaExhaustedPolicy.FAIL:
                    pair = await self._pick_failover(
                        request, root, current_harness, current_model, exclude_pairs, difficulty
                    )
                    if pair is not None:
                        new_harness, new_model = pair
                        hop = FailoverHop(
                            attempt=attempt_no,
                            from_harness=current_harness,
                            from_model=current_model,
                            to_harness=new_harness,
                            to_model=new_model,
                            reason=f"execution_error:{last_error[:200]}",
                            preserved_files=list(checkpoint.partial_modified_files) if checkpoint else [],
                        )
                        self.storage.add_failover_hop(request.task_id, hop)
                        current_harness, current_model = new_harness, new_model
                        continue
                return self._terminal_result(
                    request,
                    status=TaskStatus.FAILED,
                    initial_harness=target,
                    current_harness=current_harness,
                    current_model=current_model,
                    difficulty=difficulty,
                    effort=effort,
                    summary=f"Assignment failed on {current_harness}/{current_model}: {last_error[:300]}",
                    error_detail=last_error[:1000],
                    workdir=workdir,
                    worktree_path=worktree_path,
                    worktree_branch=worktree_branch,
                    started=started,
                    skills=[s.name for s in skills_selected],
                    plugins=list(active_plugin_names),
                    outcome=outcome,
                    transcripts=transcripts,
                    checkpoint=checkpoint,
                    mode=mode,
                    ws=ws,
                )

            # A valid assistant response is the only proof that closes circuits.
            self.guardian.record_success(current_harness, current_model)
            summary_text, _truncated = cap_text(
                outcome.assistant_text.strip() or "assignment completed",
                self.config.summary_max_chars,
            )
            self.storage.update_assignment(
                assignment.assignment_id,
                status="completed",
                ended_at=utc_now(),
                report_summary=summary_text,
                duration_ms=outcome.duration_ms,
                exit_code=outcome.exit_code,
                session_id=outcome.session_id,
            )
            last_error = ""
            success = True
            break

        if not success:
            error = last_error or "all assignment attempts were exhausted"
            return self._terminal_result(
                request,
                status=TaskStatus.FAILED,
                initial_harness=target,
                current_harness=current_harness,
                current_model=current_model,
                difficulty=difficulty,
                effort=effort,
                summary=f"Ticket failed after {existing_attempts} assignment(s): {error[:300]}",
                error_detail=error[:1000],
                workdir=workdir,
                worktree_path=worktree_path,
                worktree_branch=worktree_branch,
                started=started,
                skills=[s.name for s in skills_selected],
                plugins=list(active_plugin_names),
                outcome=last_outcome,
                transcripts=transcripts,
                checkpoint=checkpoint,
                mode=mode,
                ws=ws,
            )

        # ------------------------------------------------------------------
        # collect combined results from every assignment in the same worktree
        # ------------------------------------------------------------------
        changes = Changes(worktree_path=str(workdir))
        if mode == WorkspaceMode.ISOLATED_WORKTREE:
            try:
                changes = ws.collect_changes(workdir, stage_untracked=True)
            except Exception:
                changes = Changes(worktree_path=str(workdir))
        elif mode == WorkspaceMode.SHARED_WRITE:
            try:
                changes = ws.collect_changes(root, stage_untracked=False)
            except Exception:
                changes = Changes(worktree_path=str(root))

        summary_text, _ = cap_text(
            (last_outcome.assistant_text if last_outcome else "").strip() or "assignment completed",
            self.config.summary_max_chars,
        )
        diff_preview, diff_truncated = cap_text(
            changes.diff or "", self.config.diff_preview_max_chars
        )
        transcript_text = self._render_transcripts(request, transcripts)
        result = self._build_result(
            request,
            status=TaskStatus.COMPLETED,
            initial_harness=target,
            current_harness=current_harness,
            current_model=current_model,
            difficulty=difficulty,
            effort=effort,
            summary=summary_text,
            error_detail=None,
            workdir=workdir,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            started=started,
            skills=[s.name for s in skills_selected],
            plugins=list(active_plugin_names),
            outcome=last_outcome,
            modified_files=changes.modified_files,
            git_diff=diff_preview,
            git_diff_truncated=diff_truncated,
            warnings=[f"skipped sensitive file: {path}" for path in changes.skipped_sensitive],
            transcript_text=transcript_text,
        )
        self.storage.save_resource(request.task_id, "diff", changes.diff or "")
        self.storage.update_task(
            request.task_id,
            blocked_harness=None,
            blocked_model=None,
            checkpoint_json=None,
        )
        self.storage.set_task_result(request.task_id, result)
        self.storage.mark_effect_done_by_key("launch", request.task_id)
        if request.auto_record_context:
            try:
                self.context.auto_record(
                    request.task_id,
                    summary_text,
                    author_harness=current_harness,
                    modified_files=changes.modified_files,
                )
            except Exception:
                pass
        return result

    # ------------------------------------------------------------------
    # selection helpers
    # ------------------------------------------------------------------
    def _build_upstream_outputs(
        self,
        request: TaskRequest,
        root: Path,
        worktree_path: str | None,
        ws: WorkspaceManager,
        mode: WorkspaceMode,
    ) -> str:
        if not request.depends_on:
            return ""
        blocks: list[str] = ["<upstream_task_outputs>"]
        for dependency in request.depends_on:
            try:
                record = self.storage.get_task(dependency)
            except TaskNotFoundError:
                blocks.append(f"## {dependency}\n(status: missing)")
                blocks.append("")
                continue
            result = record.get("result")
            summary = ""
            files: list[str] = []
            if result is not None:
                summary = result.summary or ""
                files = list(result.modified_files or [])
            diff = self.storage.get_resource(dependency, "diff") or (
                result.git_diff if result is not None else ""
            )
            inherited = False
            if diff and worktree_path and mode in {
                WorkspaceMode.ISOLATED_WORKTREE,
                WorkspaceMode.READ_ONLY,
            }:
                try:
                    inherited = ws.inherit_diff(
                        request.task_id, diff, label=f"dependency {dependency}"
                    )
                except WorkspaceError:
                    inherited = False
            summary_excerpt, _ = cap_text(summary, 1500)
            diff_excerpt, _ = cap_text(diff or "", 4000)
            blocks.append(f"## {dependency}")
            blocks.append(f"status: {record.get('status')}")
            blocks.append(f"summary: {summary_excerpt or '(none)'}")
            blocks.append("modified_files: " + (", ".join(files) if files else "(none)"))
            blocks.append(f"preapplied_to_worktree: {inherited}")
            if diff_excerpt:
                blocks.append("<upstream_diff>")
                blocks.append(diff_excerpt)
                blocks.append("</upstream_diff>")
            blocks.append("")
        blocks.append("</upstream_task_outputs>")
        return "\n".join(blocks)

    def _workspace_for(self, root: Path) -> WorkspaceManager:
        key = str(root)
        if key not in self._workspace_cache:
            self._workspace_cache[key] = WorkspaceManager(
                root, self.config.state_dir, auto_init_repo=self.config.auto_init_repo
            )
        return self._workspace_cache[key]

    def _check_recursion(self, target: str, request: TaskRequest, trace_chain: list[str]) -> str | None:
        if self.config.max_call_depth >= 0 and request.call_depth >= self.config.max_call_depth:
            return (
                f"maximum call depth exceeded: call_depth={request.call_depth} "
                f">= max_call_depth={self.config.max_call_depth}"
            )
        if target in trace_chain:
            return f"recursive harness cycle rejected: {' -> '.join([*trace_chain, target])}"
        return None

    async def _ensure_models(self, harness: str, root: Path) -> list[DiscoveredModel]:
        try:
            models = await self.discovery.discover_for_harness(
                harness, workspace_root=root, force_refresh=False
            )
        except TypeError:
            # Support lightweight discovery test doubles with a narrower signature.
            models = await self.discovery.discover_for_harness(harness)
        except Exception as exc:
            self._last_discovery_errors[harness] = str(exc)
            return []
        return [model for model in models if model.authorized]

    async def _gather_candidate_models(self, root: Path) -> list[DiscoveredModel]:
        candidates: list[DiscoveredModel] = []
        seen: set[tuple[str, str]] = set()
        for harness in self.config.ordered_candidates():
            if harness not in self.config.harnesses or not self.config.harnesses[harness].enabled:
                continue
            if not self._adapter_runnable(harness):
                continue
            models = await self._ensure_models(harness, root)
            for model in models:
                pair = (harness, model.model_id)
                if pair not in seen:
                    seen.add(pair)
                    candidates.append(model)
        return candidates

    def _adapter_runnable(self, harness: str) -> bool:
        adapter = self.adapters.get(harness)
        if adapter is None:
            return False
        flag = getattr(adapter, "available", None)
        if flag is not None:
            return bool(flag)
        resolve = getattr(adapter, "resolve_binary", None)
        if callable(resolve):
            try:
                if resolve():
                    return True
            except Exception:
                pass
            name = getattr(adapter, "harness_name", harness)
            if name == "deepseek":
                key_fn = getattr(adapter, "_api_key", None)
                if callable(key_fn):
                    try:
                        if key_fn():
                            return True
                    except Exception:
                        pass
            if name == "antigravity":
                sdk_fn = getattr(adapter, "_sdk_available", None)
                if callable(sdk_fn):
                    try:
                        if sdk_fn():
                            return True
                    except Exception:
                        pass
            return False
        return True

    async def _select_initial(
        self,
        request: TaskRequest,
        root: Path,
        target: str,
        difficulty: TaskDifficulty,
        workdir: Path,
    ) -> tuple[str, str, DiscoveredModel] | None:
        ordered_targets: list[str] = []
        if target == "auto":
            ordered_targets = self.config.ordered_candidates()
        else:
            ordered_targets = [target]
            for harness in [*request.fallback_harnesses, *self.config.ordered_candidates()]:
                if harness not in ordered_targets:
                    ordered_targets.append(harness)

        all_candidates: list[DiscoveredModel] = []
        seen: set[tuple[str, str]] = set()
        for harness in ordered_targets:
            if harness not in self.config.harnesses or not self.config.harnesses[harness].enabled:
                continue
            if harness in request.trace_chain:
                continue
            if not self._adapter_runnable(harness):
                continue
            models = await self._ensure_models(harness, root)
            for model in models:
                pair = (harness, model.model_id)
                if pair not in seen:
                    seen.add(pair)
                    all_candidates.append(model)

        if not all_candidates:
            return None

        preferred: DiscoveredModel | None = None
        if request.model:
            for model in all_candidates:
                if model.model_id == request.model:
                    preferred = model
                    break
        if preferred is None:
            for harness in ordered_targets:
                if harness in request.trace_chain:
                    continue
                candidate = self.discovery.resolve_model(harness, difficulty.value)
                if candidate is not None and candidate.authorized:
                    preferred = candidate
                    break
        if preferred is not None and self.guardian.is_available(preferred.harness, preferred.model_id):
            return preferred.harness, preferred.model_id, preferred

        pair = self.guardian.select_failover_candidate(
            all_candidates,
            preferred.harness if preferred else (ordered_targets[0] if ordered_targets else ""),
            preferred.model_id if preferred else "",
            preferred_harnesses=list(request.fallback_harnesses),
            allow_half_open=True,
        )
        if pair is None:
            return None
        harness, model_id = pair
        for model in all_candidates:
            if model.harness == harness and model.model_id == model_id:
                return harness, model_id, model
        return harness, model_id, DiscoveredModel(harness=harness, model_id=model_id, source="local_config")

    async def _pick_failover(
        self,
        request: TaskRequest,
        root: Path,
        current_harness: str,
        current_model: str,
        exclude_pairs: set[tuple[str, str]],
        difficulty: TaskDifficulty,
    ) -> tuple[str, str] | None:
        models = await self._gather_candidate_models(root)
        if not models:
            return None
        blocked_ancestors = set(request.trace_chain)
        if blocked_ancestors:
            models = [model for model in models if model.harness not in blocked_ancestors]
            if not models:
                return None
        preferred = list(request.fallback_harnesses)
        return self.guardian.select_failover_candidate(
            models,
            current_harness,
            current_model,
            exclude=exclude_pairs,
            preferred_harnesses=preferred,
            allow_half_open=False,
        )

    # ------------------------------------------------------------------
    # result helpers
    # ------------------------------------------------------------------
    def _build_result(
        self,
        request: TaskRequest,
        *,
        status: TaskStatus,
        initial_harness: str,
        current_harness: str,
        current_model: str,
        difficulty: TaskDifficulty,
        effort: str,
        summary: str,
        error_detail: str | None,
        workdir: Path,
        worktree_path: str | None,
        worktree_branch: str | None,
        started: float,
        skills: list[str],
        plugins: list[str],
        outcome: AdapterOutcome | None,
        modified_files: list[str] | None = None,
        git_diff: str = "",
        git_diff_truncated: bool = False,
        warnings: list[str] | None = None,
        transcript_text: str = "",
        checkpoint: ContinuationCheckpoint | None = None,
        mode: WorkspaceMode = WorkspaceMode.ISOLATED_WORKTREE,
        ws: WorkspaceManager | None = None,
    ) -> TaskResult:
        assignments = self.storage.list_assignments(request.task_id)
        hops = self.storage.list_failover_hops(request.task_id)
        task_id = request.task_id
        raw_stdout, _ = cap_text(outcome.stdout if outcome else "", self.config.diff_preview_max_chars)
        raw_stderr, _ = cap_text(outcome.stderr if outcome else "", self.config.diff_preview_max_chars)
        result = TaskResult(
            task_id=task_id,
            initial_harness=initial_harness,
            target_harness=current_harness,
            status=status,
            resolved_difficulty=difficulty,
            resolved_model=current_model or None,
            resolved_reasoning_effort=effort,
            activated_skills=skills,
            activated_plugins=plugins,
            failover_count=len(hops),
            failover_history=hops,
            assignments=assignments,
            summary=summary,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            modified_files=list(modified_files or []),
            git_diff=git_diff,
            git_diff_truncated=git_diff_truncated,
            warnings=list(warnings or []),
            read_pointers={
                "diff": f"ticket/{task_id}/diff",
                "transcript": f"ticket/{task_id}/transcript",
                "assignments": f"ticket/{task_id}/assignments",
            },
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            native_session_id=outcome.session_id if outcome else None,
            duration_ms=int((self.now_fn() - started) * 1000),
            error_detail=error_detail,
        )
        if checkpoint is not None:
            self.storage.update_task(task_id, checkpoint_json=checkpoint)
        self._append_transcript(task_id, transcript_text)
        return result

    def _terminal_result(
        self,
        request: TaskRequest,
        *,
        status: TaskStatus,
        initial_harness: str,
        current_harness: str,
        current_model: str,
        difficulty: TaskDifficulty,
        effort: str,
        summary: str,
        error_detail: str,
        workdir: Path,
        worktree_path: str | None,
        worktree_branch: str | None,
        started: float,
        skills: list[str],
        plugins: list[str],
        outcome: AdapterOutcome | None = None,
        transcripts: list[dict[str, Any]] | None = None,
        checkpoint: ContinuationCheckpoint | None = None,
        mode: WorkspaceMode = WorkspaceMode.ISOLATED_WORKTREE,
        ws: WorkspaceManager | None = None,
    ) -> TaskResult:
        modified: list[str] = []
        diff = ""
        truncated = False
        if mode == WorkspaceMode.ISOLATED_WORKTREE and ws is not None:
            try:
                changes = ws.collect_changes(workdir, stage_untracked=True)
                modified = changes.modified_files
                diff, truncated = cap_text(changes.diff or "", self.config.diff_preview_max_chars)
                self.storage.save_resource(request.task_id, "diff", changes.diff or "")
            except Exception:
                pass
        transcript_text = (
            self._render_transcripts(request, transcripts) if transcripts is not None else ""
        )
        result = self._build_result(
            request,
            status=status,
            initial_harness=initial_harness,
            current_harness=current_harness,
            current_model=current_model,
            difficulty=difficulty,
            effort=effort,
            summary=summary,
            error_detail=error_detail,
            workdir=workdir,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            started=started,
            skills=skills,
            plugins=plugins,
            outcome=outcome,
            modified_files=modified,
            git_diff=diff,
            git_diff_truncated=truncated,
            transcript_text=transcript_text,
            checkpoint=checkpoint,
            mode=mode,
            ws=ws,
        )
        self.storage.set_task_result(request.task_id, result)
        if status != TaskStatus.WAITING_FOR_QUOTA:
            self.storage.update_task(
                request.task_id,
                blocked_harness=None,
                blocked_model=None,
                checkpoint_json=None,
            )
        return result

    def _waiting_result(
        self,
        request: TaskRequest,
        initial_harness: str,
        current_harness: str,
        current_model: str,
        difficulty: TaskDifficulty,
        effort: str,
        workdir: Path,
        worktree_path: str | None,
        worktree_branch: str | None,
        started: float,
        skills: list[Any],
        plugins: list[Any],
        outcome: AdapterOutcome,
        transcripts: list[dict[str, Any]],
        checkpoint: ContinuationCheckpoint | None,
        mode: WorkspaceMode,
        ws: WorkspaceManager,
        quota_reason: str,
    ) -> TaskResult:
        summary = (
            f"Ticket suspended on {current_harness}/{current_model} ({quota_reason}); "
            "it will resume automatically when the quota probe succeeds."
        )
        result = self._terminal_result(
            request,
            status=TaskStatus.WAITING_FOR_QUOTA,
            initial_harness=initial_harness,
            current_harness=current_harness,
            current_model=current_model,
            difficulty=difficulty,
            effort=effort,
            summary=summary,
            error_detail=quota_reason,
            workdir=workdir,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            started=started,
            skills=[item.name for item in skills],
            plugins=list(plugins),
            outcome=outcome,
            transcripts=transcripts,
            checkpoint=checkpoint,
            mode=mode,
            ws=ws,
        )
        self.storage.update_task(
            request.task_id,
            status=TaskStatus.WAITING_FOR_QUOTA,
            blocked_harness=current_harness,
            blocked_model=current_model,
        )
        self.storage.enqueue_effect(
            "resume_waiting",
            request.task_id,
            {"task_id": request.task_id, "harness": current_harness, "model": current_model},
        )
        self.storage.mark_effect_done_by_key("launch", request.task_id)
        return result

    def _duplicate_task_result(self, request: TaskRequest) -> TaskResult:
        """Reject a second live dispatch for a task id without touching storage.

        Letting both calls run would create duplicate attempt numbers, execute
        two adapters in one worktree and make status/results last-write-wins.
        """

        return TaskResult(
            task_id=request.task_id,
            initial_harness=request.target_harness,
            target_harness=request.target_harness,
            status=TaskStatus.REJECTED,
            summary="Rejected: another dispatch for this task_id is still active.",
            error_detail=f"duplicate_active_task:{request.task_id}",
        )

    def _rejected_result(self, request: TaskRequest, reason: str, started: float) -> TaskResult:
        try:
            self.storage.create_task(request, status=TaskStatus.REJECTED)
            self.storage.update_task(
                request.task_id, worktree_path=None, worktree_branch=None
            )
        except Exception:
            pass
        difficulty = (
            evaluate_difficulty(request.objective, request.context_files, request.context_notes)
            if request.difficulty == TaskDifficulty.AUTO
            else request.difficulty
        )
        result = self._build_result(
            request,
            status=TaskStatus.REJECTED,
            initial_harness=request.target_harness,
            current_harness=request.target_harness,
            current_model=request.model or "",
            difficulty=difficulty,
            effort=request.reasoning_effort or self.config.effort_for(difficulty),
            summary=f"Rejected: {reason}",
            error_detail=reason,
            workdir=Path(request.workspace_root or self.workspace_root),
            worktree_path=None,
            worktree_branch=None,
            started=started,
            skills=[],
            plugins=[],
            outcome=None,
        )
        self.storage.set_task_result(request.task_id, result)
        return result

    def _unexpected_failure(self, request: TaskRequest, exc: BaseException) -> TaskResult:
        message = f"unexpected router failure: {exc}"
        try:
            record = self._existing_record(request.task_id)
            if record is None or record.get("status") == "pending":
                self.storage.create_task(request, status=TaskStatus.FAILED)
        except Exception:
            pass
        try:
            difficulty = (
                evaluate_difficulty(request.objective, request.context_files, request.context_notes)
                if request.difficulty == TaskDifficulty.AUTO
                else request.difficulty
            )
            result = self._build_result(
                request,
                status=TaskStatus.FAILED,
                initial_harness=request.target_harness,
                current_harness=request.target_harness,
                current_model=request.model or "",
                difficulty=difficulty,
                effort=request.reasoning_effort or self.config.effort_for(difficulty),
                summary=message,
                error_detail=message,
                workdir=Path(request.workspace_root or self.workspace_root),
                worktree_path=None,
                worktree_branch=None,
                started=self.now_fn(),
                skills=[],
                plugins=[],
                outcome=None,
            )
            self.storage.set_task_result(request.task_id, result)
            return result
        except Exception:
            return TaskResult(
                task_id=request.task_id,
                initial_harness=request.target_harness,
                target_harness=request.target_harness,
                status=TaskStatus.FAILED,
                summary=message,
                error_detail=message,
            )

    def _persist_cancelled(self, task_id: str) -> None:
        try:
            record = self.storage.get_task(task_id)
        except TaskNotFoundError:
            return
        for assignment in self.storage.list_assignments(task_id):
            if assignment.status in {"pending", "running"}:
                self.storage.update_assignment(
                    assignment.assignment_id,
                    status="ended_error",
                    ended_at=utc_now(),
                    error_reason="cancelled",
                )
        request = record.get("request")
        if request is None:
            self.storage.update_task(task_id, status=TaskStatus.CANCELLED)
            return
        difficulty = (
            evaluate_difficulty(request.objective, request.context_files, request.context_notes)
            if request.difficulty == TaskDifficulty.AUTO
            else request.difficulty
        )
        result = self._build_result(
            request,
            status=TaskStatus.CANCELLED,
            initial_harness=record.get("initial_harness") or request.target_harness,
            current_harness=record.get("current_harness") or request.target_harness,
            current_model=record.get("current_model") or request.model or "",
            difficulty=difficulty,
            effort=request.reasoning_effort or self.config.effort_for(difficulty),
            summary="Ticket cancelled by controller.",
            error_detail="cancelled",
            workdir=Path(request.workspace_root or self.workspace_root),
            worktree_path=record.get("worktree_path"),
            worktree_branch=record.get("worktree_branch"),
            started=self.now_fn(),
            skills=[],
            plugins=[],
            outcome=None,
        )
        self.storage.set_task_result(task_id, result)
        self.storage.update_task(
            task_id,
            blocked_harness=None,
            blocked_model=None,
            checkpoint_json=None,
        )
        self.storage.mark_effect_done_by_key("resume_waiting", task_id)

    def _capture_checkpoint(
        self,
        ws: WorkspaceManager,
        request: TaskRequest,
        workdir: Path,
        harness: str,
        model: str,
        reason: str,
        stdout: str,
        assignment_id: str,
        mode: WorkspaceMode,
    ) -> ContinuationCheckpoint:
        if mode == WorkspaceMode.READ_ONLY:
            checkpoint = ContinuationCheckpoint(
                interrupted_harness=harness,
                interrupted_model=model,
                interruption_reason=reason,
                partial_modified_files=[],
                partial_git_diff="",
                partial_stdout_tail=(stdout or "")[-4000:],
                worktree_path=str(workdir),
                last_assignment_id=assignment_id,
            )
        else:
            try:
                checkpoint = ws.capture_checkpoint(
                    request,
                    workdir,
                    harness,
                    model,
                    reason,
                    stdout,
                    assignment_id=assignment_id,
                    stage_untracked=(mode == WorkspaceMode.ISOLATED_WORKTREE),
                )
            except Exception:
                checkpoint = ContinuationCheckpoint(
                    interrupted_harness=harness,
                    interrupted_model=model,
                    interruption_reason=reason,
                    partial_stdout_tail=(stdout or "")[-4000:],
                    worktree_path=str(workdir),
                    last_assignment_id=assignment_id,
                )
        try:
            self.storage.update_task(request.task_id, checkpoint_json=checkpoint)
        except Exception:
            pass
        return checkpoint

    @staticmethod
    def _normalize_outcome(outcome: Any) -> AdapterOutcome:
        if isinstance(outcome, AdapterOutcome):
            return outcome
        if isinstance(outcome, Mapping):
            return AdapterOutcome(
                exit_code=int(outcome.get("exit_code", 0)),
                stdout=str(outcome.get("stdout", "")),
                stderr=str(outcome.get("stderr", "")),
                timed_out=bool(outcome.get("timed_out", False)),
                session_id=outcome.get("session_id"),
                duration_ms=int(outcome.get("duration_ms", 0)),
                error_detail=outcome.get("error_detail"),
                parsed_output=str(outcome.get("parsed_output", "")),
                metadata=dict(outcome.get("metadata", {})),
            )
        return AdapterOutcome(
            exit_code=0,
            stdout=str(outcome),
            parsed_output=str(outcome),
        )

    def _append_transcript(self, task_id: str, transcript_text: str) -> None:
        """Append a dispatch's transcript to the ticket-level resource.

        A resumed ticket executes in a second dispatch; replacing the resource
        would lose the stdout/stderr of the quota-interrupted assignment and
        make ``read_task_resource(task_id, "transcript")`` incomplete.
        """

        if not transcript_text:
            return
        existing = self.storage.get_resource(task_id, "transcript") or ""
        if existing and existing.endswith(transcript_text):
            return
        combined = f"{existing}\n\n{transcript_text}" if existing else transcript_text
        self.storage.save_resource(task_id, "transcript", combined)

    def _render_transcripts(self, request: TaskRequest, transcripts: list[dict[str, Any]]) -> str:
        if not transcripts:
            return ""
        blocks: list[str] = []
        for item in transcripts:
            blocks.append(
                "\n".join(
                    [
                        f"=== Assignment #{item.get('attempt')} "
                        f"[{item.get('harness')}/{item.get('model')}] ===",
                        f"assignment_id: {item.get('assignment_id')}",
                        f"exit_code: {item.get('exit_code')} timed_out: {item.get('timed_out')}",
                        f"session_id: {item.get('session_id') or 'n/a'}",
                        "<stdout>",
                        item.get("stdout") or "",
                        "</stdout>",
                        "<stderr>",
                        item.get("stderr") or "",
                        "</stderr>",
                    ]
                )
            )
        return "\n\n".join(blocks)

    def _existing_record(self, task_id: str) -> dict[str, Any] | None:
        try:
            return self.storage.get_task(task_id)
        except TaskNotFoundError:
            return None

    # ------------------------------------------------------------------
    # recovery / resume
    # ------------------------------------------------------------------
    async def resume_task(self, task_id: str, *, force: bool = False) -> TaskResult | None:
        try:
            record = self.storage.get_task(task_id)
        except TaskNotFoundError:
            return None
        request: TaskRequest | None = record.get("request")
        if request is None:
            return None
        if record["status"] != TaskStatus.WAITING_FOR_QUOTA and not force:
            return None
        if task_id in self._resuming:
            return None
        blocked_harness = record.get("blocked_harness")
        blocked_model = record.get("blocked_model")
        if blocked_harness and blocked_model and not force:
            if not self.guardian.is_available(blocked_harness, blocked_model):
                return None
        checkpoint = record.get("checkpoint") or request.checkpoint
        resumed_request = request.model_copy(update={"checkpoint": checkpoint, "call_depth": request.call_depth})
        self._resuming.add(task_id)
        try:
            result = await self.dispatch(resumed_request)
        finally:
            self._resuming.discard(task_id)
        self.storage.mark_effect_done_by_key("resume_waiting", task_id)
        return result

    async def resume_waiting_tasks(
        self, harness: str | None = None, model: str | None = None
    ) -> list[TaskResult]:
        results: list[TaskResult] = []
        for task in self.storage.list_tasks(status=TaskStatus.WAITING_FOR_QUOTA, limit=500):
            task_id = task["task_id"]
            if harness and task.get("blocked_harness") not in {None, harness}:
                continue
            if model and model != "*" and task.get("blocked_model") not in {None, "*", model}:
                continue
            result = await self.resume_task(task_id)
            if result is not None:
                results.append(result)
        return results

    async def on_quota_recovered(self, harness: str, model: str = "*") -> list[TaskResult]:
        return await self.resume_waiting_tasks(harness=harness, model=model)

    async def _default_probe(self, harness: str, model: str | None = None) -> tuple[bool, str]:
        """Probe the provider itself before closing a quota circuit.

        A local configuration scan or a ``--version`` call cannot prove that a
        provider budget recovered, so the adapter's real lightweight
        completion probe is the only accepted evidence here.
        """

        adapter = self.adapters.get(harness)
        if adapter is None:
            return False, "adapter not configured"
        quota_probe = getattr(adapter, "probe_quota", None)
        if not callable(quota_probe):
            return False, "adapter does not expose a quota probe"
        try:
            result = quota_probe(model)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            return False, f"quota probe error: {exc}"
        if result is None:
            return False, "adapter cannot probe this channel"
        if isinstance(result, tuple):
            return bool(result[0]), str(result[1]) if len(result) > 1 else ""
        if isinstance(result, dict):
            return bool(result.get("success", result.get("ok", False))), str(result.get("detail", ""))
        return bool(result), ""

    async def start_effects_worker(self, interval_seconds: float = 5.0) -> None:
        """Consume the durable effects queue in the background.

        This is what makes crash recovery real: pending ``resume_waiting`` and
        ``quota_probe`` rows are executed, and stale ``running`` rows are
        re-queued after an interrupted process.
        """

        if self._effects_task is not None and not self._effects_task.done():
            return
        self._effects_stop_event = asyncio.Event()
        self._effects_task = asyncio.create_task(
            self._effects_loop(max(0.5, float(interval_seconds))),
            name="harness-mesh-effects-worker",
        )

    async def _effects_loop(self, interval_seconds: float) -> None:
        while not self._effects_stop_event.is_set():
            try:
                self.storage.reset_stale_effects(older_than_seconds=max(300.0, interval_seconds * 12))
                await self.process_due_effects()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The worker must survive malformed effects and storage hiccups.
                pass
            try:
                await asyncio.wait_for(self._effects_stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def stop_effects_worker(self) -> None:
        self._effects_stop_event.set()
        if self._effects_task is not None:
            self._effects_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._effects_task
            self._effects_task = None

    @property
    def effects_worker_running(self) -> bool:
        return self._effects_task is not None and not self._effects_task.done()

    async def process_due_effects(self, limit: int = 50) -> list[dict[str, Any]]:
        processed: list[dict[str, Any]] = []
        for effect in self.storage.due_effects(limit=limit):
            self.storage.mark_effect_running(effect["id"])
            try:
                if effect["kind"] == "resume_waiting":
                    task_id = effect.get("payload", {}).get("task_id") or effect["key"]
                    result = await self.resume_task(task_id)
                    if result is None:
                        # Still blocked (circuit open or task not resumable):
                        # keep the effect for a later recovery round.
                        task = self._existing_record(task_id)
                        if task is not None and task.get("status") == TaskStatus.WAITING_FOR_QUOTA:
                            self.storage.mark_effect_failed(effect["id"])
                        else:
                            self.storage.mark_effect_done(effect["id"])
                    else:
                        self.storage.mark_effect_done(effect["id"])
                    processed.append(
                        {
                            "kind": "resume_waiting",
                            "task_id": task_id,
                            "result": result.status.value if result else None,
                        }
                    )
                elif effect["kind"] == "quota_probe":
                    await self.monitor.check_and_recover_all(force=True)
                    self.storage.mark_effect_done(effect["id"])
                    processed.append({"kind": "quota_probe", "harness": effect.get("payload", {}).get("harness")})
                elif effect["kind"] == "launch":
                    self.storage.mark_effect_done(effect["id"])
                    processed.append({"kind": "launch", "task_id": effect["key"]})
                else:
                    self.storage.mark_effect_done(effect["id"])
                    processed.append({"kind": effect["kind"], "status": "ignored"})
            except Exception as exc:
                self.storage.mark_effect_failed(effect["id"])
                processed.append({"kind": effect["kind"], "error": str(exc)})
        return processed

    # ------------------------------------------------------------------
    # query / control surface
    # ------------------------------------------------------------------
    async def list_harnesses(self, refresh_models: bool = False) -> list[HarnessStatus]:
        statuses: list[HarnessStatus] = []
        for harness, harness_config in self.config.harnesses.items():
            if not harness_config.enabled:
                continue
            adapter = self.adapters.get(harness)
            status = HarnessStatus(harness=harness, available=False, mode="missing")
            if adapter is not None:
                detect = getattr(adapter, "detect", None)
                if callable(detect):
                    try:
                        detected = detect()
                        if asyncio.iscoroutine(detected):
                            detected = await detected
                        if isinstance(detected, HarnessStatus):
                            status = detected
                    except Exception as exc:
                        status.version_or_detail = f"detect failed: {exc}"
                else:
                    status = HarnessStatus(
                        harness=harness,
                        available=bool(getattr(adapter, "available", True)),
                        mode="custom",
                        version_or_detail="custom adapter",
                    )
            try:
                models = await self.discovery.discover_for_harness(
                    harness, workspace_root=self.workspace_root, force_refresh=refresh_models
                )
            except Exception:
                models = self.storage.list_discovered_models(harness)
            authorized = [model.model_id for model in models if model.authorized]
            tier_models = self._tier_models_from_models(models)
            status.authorized_models = authorized
            status.tier_models = tier_models
            if not status.active_default_model:
                for model in models:
                    if model.authorized and model.is_active_default:
                        status.active_default_model = model.model_id
                        break
            circuits = self.guardian.list_circuits(harness=harness)
            status.circuits = circuits
            if not circuits:
                status.quota_state = "closed"
            else:
                states = {record.state.value for record in circuits}
                if "open" in states:
                    status.quota_state = "open"
                elif "half_open" in states:
                    status.quota_state = "half_open"
                else:
                    status.quota_state = "closed"
            statuses.append(status)
        return statuses

    def _tier_models_from_models(self, models: Iterable[DiscoveredModel]) -> dict[str, str]:
        model_list = list(models)
        if not model_list:
            return {}
        tier_fn = getattr(self.discovery, "tier_models_for", None)
        if callable(tier_fn):
            try:
                return tier_fn(model_list)
            except Exception:
                pass
        authorized = [model for model in model_list if model.authorized]
        if not authorized:
            return {}
        ranked = sorted(authorized, key=lambda item: item.capability_score, reverse=True)
        return {tier: ranked[0].model_id for tier in ("low", "medium", "high", "critical")}

    async def discover_harness_models(
        self, harness: str | None = None, force_refresh: bool = True
    ) -> dict[str, Any]:
        if harness is None:
            result: dict[str, Any] = {}
            for name in self.config.enabled_harnesses():
                result[name] = await self._discover_payload(name, force_refresh)
            return result
        return await self._discover_payload(harness, force_refresh)

    async def _discover_payload(self, harness: str, force_refresh: bool) -> dict[str, Any]:
        if harness not in self.config.harnesses:
            return {"harness": harness, "error": "unknown harness", "models": [], "tier_models": {}}
        try:
            models = await self.discovery.discover_for_harness(
                harness, workspace_root=self.workspace_root, force_refresh=force_refresh
            )
        except Exception as exc:
            models = self.storage.list_discovered_models(harness)
            return {
                "harness": harness,
                "error": str(exc),
                "models": [model.model_dump(mode="json") for model in models],
                "tier_models": self._tier_models_from_models(models),
            }
        return {
            "harness": harness,
            "models": [model.model_dump(mode="json") for model in models],
            "tier_models": self._tier_models_from_models(models),
            "authorized_models": [model.model_id for model in models if model.authorized],
        }

    def list_skills_and_plugins(self, query: str = "") -> dict[str, Any]:
        return self.skills.list_available(query)

    def get_project_context(
        self, category: str | None = None, include_instructions: bool = True
    ) -> dict[str, Any]:
        return self.context.snapshot(category=category, include_instructions=include_instructions)

    def update_project_context(
        self,
        key: str,
        content: str,
        *,
        category: str = "note",
        author_harness: str = "controller",
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return self.context.update(
            key,
            content,
            category=category,
            author_harness=author_harness,
            task_id=task_id,
        )

    def get_task_status(self, task_id: str) -> dict[str, Any]:
        record = self.storage.get_task(task_id)
        result = record.get("result")
        assignments = self.storage.list_assignments(task_id)
        return {
            "task_id": task_id,
            "status": record["status"],
            "initial_harness": record.get("initial_harness"),
            "current_harness": record.get("current_harness"),
            "current_model": record.get("current_model"),
            "resolved_difficulty": record.get("resolved_difficulty"),
            "resolved_model": record.get("resolved_model"),
            "blocked_harness": record.get("blocked_harness"),
            "blocked_model": record.get("blocked_model"),
            "worktree_path": record.get("worktree_path"),
            "worktree_branch": record.get("worktree_branch"),
            "objective": record["request"].objective if record.get("request") else None,
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "error_detail": record.get("error_detail"),
            "assignments": [assignment.model_dump(mode="json") for assignment in assignments],
            "failover_history": [hop.model_dump(mode="json") for hop in self.storage.list_failover_hops(task_id)],
            "result": result.model_dump(mode="json") if result is not None else None,
        }

    def read_task_resource(
        self, task_id: str, resource: str = "diff", offset: int = 0, limit: int = 4000
    ) -> dict[str, Any]:
        page = self.storage.read_task_resource(task_id, resource, offset=offset, limit=limit)
        return page.model_dump(mode="json")

    async def get_quota_status(self, probe_now: bool = False) -> dict[str, Any]:
        recovery: list[dict[str, Any]] = []
        if probe_now:
            recovery = await self.monitor.check_and_recover_all(force=True)
        waiting = self.storage.list_tasks(status=TaskStatus.WAITING_FOR_QUOTA, limit=500)
        return {
            "circuits": self.guardian.status_snapshot(),
            "waiting_for_quota": [
                {
                    "task_id": task["task_id"],
                    "blocked_harness": task.get("blocked_harness"),
                    "blocked_model": task.get("blocked_model"),
                    "updated_at": task.get("updated_at"),
                }
                for task in waiting
            ],
            "recovery_probe_results": recovery,
            "effects": self.storage.list_effects(),
        }

    async def report_quota_status(
        self,
        harness: str,
        model: str = "*",
        action: str = "reset",
        cooldown_seconds: int = 300,
    ) -> dict[str, Any]:
        normalized = (action or "reset").strip().lower()
        if normalized in {"reset", "close", "clear"}:
            affected = self.guardian.reset(harness, model)
            return {"action": normalized, "harness": harness, "model": model, "affected": affected}
        if normalized in {"open", "exhausted", "mark_exhausted", "fail"}:
            record = self.guardian.record_exhaustion(
                harness,
                model,
                "manual_report",
                is_account_wide=model == "*",
                cooldown_seconds=cooldown_seconds,
            )
            return {"action": normalized, "circuit": record.model_dump(mode="json")}
        if normalized in {"half_open", "probe"}:
            record = self.guardian.mark_half_open(harness, model)
            return {"action": normalized, "circuit": record.model_dump(mode="json")}
        raise ValueError(f"unknown quota action: {action}")

    def apply_task_patch(self, task_id: str, strategy: str = "patch") -> dict[str, Any]:
        record = self.storage.get_task(task_id)
        if record.get("status") != TaskStatus.COMPLETED:
            raise WorkspaceError(
                f"task {task_id} is not completed (status={record.get('status')}); "
                "only completed tickets can be applied"
            )
        request = record.get("request")
        if request is not None and request.workspace_mode == WorkspaceMode.READ_ONLY:
            raise WorkspaceError(
                f"task {task_id} was executed in read_only mode; its disposable worktree is not applied"
            )
        try:
            root = resolve_allowed_root(
                request.workspace_root if request else self.workspace_root,
                self.config.allowed_workspace_roots,
            )
        except WorkspaceRootError as exc:
            raise WorkspaceError(str(exc)) from exc
        ws = self._workspace_for(root)
        apply_result = ws.apply_worktree_changes(task_id, workspace_root=root, strategy=strategy)
        return apply_result.as_dict()

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        record = self.storage.get_task(task_id)
        running = self._running_tasks.get(task_id)
        current = asyncio.current_task()
        if running is not None and running is not current and not running.done():
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await running
        request = record.get("request")
        if request is not None:
            difficulty = (
                evaluate_difficulty(request.objective, request.context_files, request.context_notes)
                if request.difficulty == TaskDifficulty.AUTO
                else request.difficulty
            )
            for assignment in self.storage.list_assignments(task_id):
                if assignment.status in {"pending", "running"}:
                    self.storage.update_assignment(
                        assignment.assignment_id,
                        status="ended_error",
                        ended_at=utc_now(),
                        error_reason="cancelled",
                    )
            result = self._build_result(
                request,
                status=TaskStatus.CANCELLED,
                initial_harness=record.get("initial_harness") or request.target_harness,
                current_harness=record.get("current_harness") or request.target_harness,
                current_model=record.get("current_model") or request.model or "",
                difficulty=difficulty,
                effort=request.reasoning_effort or self.config.effort_for(difficulty),
                summary="Ticket cancelled by controller.",
                error_detail="cancelled",
                workdir=Path(request.workspace_root or self.workspace_root),
                worktree_path=record.get("worktree_path"),
                worktree_branch=record.get("worktree_branch"),
                started=self.now_fn(),
                skills=[],
                plugins=[],
                outcome=None,
            )
            self.storage.set_task_result(task_id, result)
            self.storage.update_task(
                task_id,
                blocked_harness=None,
                blocked_model=None,
                checkpoint_json=None,
            )
        else:
            self.storage.update_task(task_id, status=TaskStatus.CANCELLED)
        self.storage.mark_effect_done_by_key("resume_waiting", task_id)
        self.storage.mark_effect_done_by_key("launch", task_id)
        return {"task_id": task_id, "status": TaskStatus.CANCELLED.value}

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.storage.list_tasks(status=status)

    # ------------------------------------------------------------------
    # durable effects introspection
    # ------------------------------------------------------------------
    def list_effects(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.storage.list_effects(status=status)


def create_mesh_runtime(
    config_path: str | Path | None = None,
    workspace_root: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    config: MeshConfig | None = None,
) -> MeshRuntime:
    root = Path(workspace_root or Path.cwd()).expanduser().resolve()
    root = ensure_service_workspace(root)
    cfg = config or load_config(config_path, root)
    if not cfg.allowed_workspace_roots:
        cfg.allowed_workspace_roots = [str(root)]
    home_dir = (env or {}).get("HOME") or (env or {}).get("USERPROFILE")
    state_dir = cfg.state_path(root)
    storage = StateStore(state_dir / "state.db")
    discovery = ModelDiscoveryEngine(
        cfg,
        storage,
        root,
        home_dir=home_dir,
        env=env,
    )
    skills = SkillPluginManager(cfg, root, home_dir=home_dir, env=env)
    context = ProjectContextManager(storage, root, valid_harnesses=cfg.harnesses.keys())
    workspace = WorkspaceManager(root, cfg.state_dir, auto_init_repo=cfg.auto_init_repo)
    adapters = build_adapters(cfg, root, env=env)
    guardian = QuotaGuardian(
        storage,
        cooldown_seconds=cfg.quota_cooldown_seconds,
        failover_priority=cfg.failover_priority,
    )
    router = MeshRouter(
        cfg,
        storage,
        discovery=discovery,
        skills=skills,
        context=context,
        workspace=workspace,
        adapters=adapters,
        guardian=guardian,
        workspace_root=root,
        env=env,
        home_dir=home_dir,
    )
    return MeshRuntime(
        config=cfg,
        storage=storage,
        discovery=discovery,
        skills=skills,
        context=context,
        workspace=workspace,
        adapters=adapters,
        guardian=guardian,
        monitor=router.monitor,
        router=router,
    )


__all__ = ["MeshRouter", "MeshRuntime", "create_mesh_runtime"]
