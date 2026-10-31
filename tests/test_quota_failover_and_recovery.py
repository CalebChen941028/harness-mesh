"""Quota detection, cross-assignment failover and live recovery resumption."""

from __future__ import annotations

import asyncio
import json

import pytest

from harness_mesh.models import CircuitState, QuotaExhaustedPolicy, TaskRequest, TaskStatus
from harness_mesh.quota import QuotaErrorDetector
from tests.helpers import (
    FakeAdapter,
    failure,
    make_mesh,
    quota_failure,
    success,
    write_claude_settings,
    write_codex_config,
)


def test_quota_error_detector_splits_account_and_model_scopes():
    is_quota, account_wide, reason, retry_after = QuotaErrorDetector.detect_quota_exhaustion(
        "", "API error: budget_exceeded — Budget has been exceeded", 1
    )
    assert is_quota is True
    assert account_wide is True
    assert "budget" in reason

    is_quota, account_wide, reason, _ = QuotaErrorDetector.detect_quota_exhaustion(
        "", "HTTP 402 Payment Required: Insufficient Balance", 1
    )
    assert is_quota is True and account_wide is True

    is_quota, account_wide, reason, retry_after = QuotaErrorDetector.detect_quota_exhaustion(
        "", "HTTP 429 Too Many Requests; retry-after: 17", 1
    )
    assert is_quota is True
    assert account_wide is False
    assert retry_after == 17

    is_quota, account_wide, reason, retry_after = QuotaErrorDetector.detect_quota_exhaustion(
        "", "RESOURCE_EXHAUSTED: try again in 2m", 1
    )
    assert is_quota is True
    assert account_wide is False
    assert retry_after == 120

    is_quota, _account_wide, _reason, retry_after = QuotaErrorDetector.detect_quota_exhaustion(
        "", "429 too many requests, retry after 30 seconds", 1
    )
    assert is_quota is True
    assert retry_after == 30

    assert QuotaErrorDetector.detect_quota_exhaustion("", "503 Service Unavailable", 1)[0] is False
    assert QuotaErrorDetector.detect_transient("", "503 Service Unavailable", 1)[0] is True
    assert QuotaErrorDetector.detect_transient("", "connection reset by peer", 1)[0] is True

    unauthorized, reason = QuotaErrorDetector.detect_unauthorized(
        "", '{"error": {"code": "model_not_found", "message": "model does not exist"}}', 1
    )
    assert unauthorized is True
    assert "model" in reason

    unauthorized, _ = QuotaErrorDetector.detect_unauthorized(
        "", "You do not have access to model gpt-x", 1
    )
    assert unauthorized is True

    unauthorized, _ = QuotaErrorDetector.detect_unauthorized("", "403 Forbidden", 403)
    assert unauthorized is True


def test_circuit_enters_half_open_after_cooldown_and_probe_closes_it(tmp_path):
    from harness_mesh.quota import QuotaGuardian, QuotaRecoveryMonitor
    from harness_mesh.storage import StateStore

    storage = StateStore(tmp_path / "state.db")
    clock = [1000.0]
    guardian = QuotaGuardian(storage, cooldown_seconds=10, now_fn=lambda: clock[0])
    try:
        guardian.record_exhaustion("claude_code", "claude-opus-9", "rate_limit_429", is_account_wide=False)
        assert guardian.availability("claude_code", "claude-opus-9") == "open"
        assert guardian.is_available("claude_code", "claude-opus-9", allow_half_open=True) is False

        clock[0] += 11
        assert guardian.availability("claude_code", "claude-opus-9") == "half_open"
        assert guardian.is_available("claude_code", "claude-opus-9", allow_half_open=True) is True
        assert guardian.is_available("claude_code", "claude-opus-9", allow_half_open=False) is False

        async def probe(harness, model):
            return True, f"{harness}/{model} recovered"

        monitor = QuotaRecoveryMonitor(guardian, probe=probe, now_fn=lambda: clock[0])
        results = asyncio.run(monitor.check_and_recover_all(force=True))
        assert any(item.get("success") for item in results)
        circuit = guardian.get_circuit("claude_code", "claude-opus-9")
        assert circuit is not None and circuit.state == CircuitState.CLOSED
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_not_logged_in_opens_harness_wide_circuit_and_fails_over(tmp_path):
    codex_calls = {"count": 0}

    def claude_handler(adapter, call):
        return failure("authentication error: please run /login", exit_code=1)

    def codex_handler(adapter, call):
        codex_calls["count"] += 1
        return success("codex completed after credential failure")

    mesh = make_mesh(
        tmp_path,
        adapters={
            "claude_code": FakeAdapter("claude_code", claude_handler),
            "codex": FakeAdapter("codex", codex_handler),
        },
    )
    try:
        write_claude_settings(mesh.home_dir, model="demo-opus-5-5")
        write_codex_config(mesh.home_dir, model="demo-gpt-6.1-sol")
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="Fail over when the harness is not logged in.",
                fallback_harnesses=["codex"],
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED
        assert result.target_harness == "codex"
        assert codex_calls["count"] == 1
        assert "not_logged_in" in (result.failover_history[0].reason or "")
        circuit = mesh.guardian.get_circuit("claude_code", "*")
        assert circuit is not None
        assert circuit.state == CircuitState.OPEN
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_fail_policy_does_not_failover(tmp_path):
    codex_calls = {"count": 0}

    def claude_handler(adapter, call):
        return quota_failure("HTTP 429 too many requests: rate_limit_exceeded")

    def codex_handler(adapter, call):
        codex_calls["count"] += 1
        return success("codex should not run")

    mesh = make_mesh(
        tmp_path,
        adapters={
            "claude_code": FakeAdapter("claude_code", claude_handler),
            "codex": FakeAdapter("codex", codex_handler),
        },
    )
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        write_codex_config(mesh.home_dir, model="gpt-5-codex")
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="This must fail instead of failing over.",
                on_quota_exhausted=QuotaExhaustedPolicy.FAIL,
                fallback_harnesses=["codex"],
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.FAILED
        assert result.failover_count == 0
        assert codex_calls["count"] == 0
        assert [assignment.status for assignment in result.assignments] == ["ended_quota"]
        assert result.error_detail in {"rate_limit_429", "rate_limit_exceeded"}
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_model_scoped_quota_prefers_same_harness_before_crossing(tmp_path):
    codex_calls = {"count": 0}

    def claude_handler(adapter, call):
        if call.model == "claude-opus-9":
            return quota_failure("HTTP 429 too many requests: rate_limit_exceeded")
        return success("same harness fallback completed")

    def codex_handler(adapter, call):
        codex_calls["count"] += 1
        return success("cross harness should not be needed")

    mesh = make_mesh(
        tmp_path,
        adapters={
            "claude_code": FakeAdapter("claude_code", claude_handler),
            "codex": FakeAdapter("codex", codex_handler),
        },
    )
    try:
        write_claude_settings(
            mesh.home_dir,
            model="claude-opus-9",
            env={"ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-9"},
        )
        write_codex_config(mesh.home_dir, model="gpt-5-codex")
        request = TaskRequest(
            target_harness="claude_code",
            objective="Use the strongest model for this ticket.",
            difficulty="critical",
            fallback_harnesses=["codex"],
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        result = await mesh.router.dispatch(request)

        assert result.status == TaskStatus.COMPLETED
        assert result.target_harness == "claude_code"
        assert result.resolved_model == "claude-haiku-9"
        assert codex_calls["count"] == 0
        assert [assignment.harness for assignment in result.assignments] == ["claude_code", "claude_code"]
        assert [assignment.model for assignment in result.assignments] == ["claude-opus-9", "claude-haiku-9"]
        assert result.failover_history[0].to_harness == "claude_code"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_mesh_config_max_failover_attempts_is_used_when_request_omits_it(tmp_path):
    codex_calls = {"count": 0}

    def claude_handler(adapter, call):
        return quota_failure("HTTP 429 too many requests: rate_limit_exceeded")

    def codex_handler(adapter, call):
        codex_calls["count"] += 1
        return success("codex should not run with zero configured failovers")

    mesh = make_mesh(
        tmp_path / "zero",
        adapters={
            "claude_code": FakeAdapter("claude_code", claude_handler),
            "codex": FakeAdapter("codex", codex_handler),
        },
        config_updates={"max_failover_attempts": 0},
    )
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        write_codex_config(mesh.home_dir, model="gpt-5-codex")
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="No failover budget from config.",
                fallback_harnesses=["codex"],
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.FAILED
        assert codex_calls["count"] == 0
        assert [assignment.status for assignment in result.assignments] == ["ended_quota"]
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_request_explicit_max_failover_attempts_overrides_mesh_config(tmp_path):
    codex_calls = {"count": 0}

    def claude_handler(adapter, call):
        return quota_failure("HTTP 429 too many requests: rate_limit_exceeded")

    def codex_handler(adapter, call):
        codex_calls["count"] += 1
        return success("codex completed the failover")

    mesh = make_mesh(
        tmp_path / "explicit",
        adapters={
            "claude_code": FakeAdapter("claude_code", claude_handler),
            "codex": FakeAdapter("codex", codex_handler),
        },
        config_updates={"max_failover_attempts": 0},
    )
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        write_codex_config(mesh.home_dir, model="gpt-5-codex")
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="Explicit request override permits one failover.",
                fallback_harnesses=["codex"],
                max_failover_attempts=1,
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED
        assert result.target_harness == "codex"
        assert codex_calls["count"] == 1
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_successful_report_mentioning_quota_or_auth_errors_is_not_misclassified(tmp_path):
    codex_calls = {"count": 0}

    def claude_handler(adapter, call):
        return success("Done. The earlier 429 rate_limit_exceeded and model_not_found errors are fixed.")

    def codex_handler(adapter, call):
        codex_calls["count"] += 1
        return success("codex should never run")

    mesh = make_mesh(
        tmp_path,
        adapters={
            "claude_code": FakeAdapter("claude_code", claude_handler),
            "codex": FakeAdapter("codex", codex_handler),
        },
    )
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        write_codex_config(mesh.home_dir, model="gpt-5-codex")
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="Summarise how the previous 429 incident was fixed.",
                fallback_harnesses=["codex"],
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED
        assert result.failover_count == 0
        assert codex_calls["count"] == 0
        assert [assignment.status for assignment in result.assignments] == ["completed"]
        assert mesh.guardian.list_circuits(harness="claude_code") == []
        assert "claude-opus-9" in [model.model_id for model in mesh.discovery.authorized_models("claude_code")]
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_quota_mid_assignment_fails_over_and_merges_worktree_diff(tmp_path):
    def claude_handler(adapter, call):
        (call.workdir / "alpha.py").write_text("ALPHA = 'written by claude'\n", encoding="utf-8")
        return quota_failure("HTTP 429 rate_limit_exceeded: too many requests; retry-after: 17")

    def codex_handler(adapter, call):
        assert (call.workdir / "alpha.py").exists(), "Assignment #2 must reuse Assignment #1's worktree"
        assert "<continuation_checkpoint>" in call.prompt
        assert "<recovery_note>" in call.prompt
        assert "alpha.py" in call.prompt
        assert "git status --porcelain" in call.prompt
        assert "git diff HEAD" in call.prompt
        (call.workdir / "beta.py").write_text("BETA = 'written by codex'\n", encoding="utf-8")
        return success("codex finished the remaining work", session_id="codex-session-1")

    claude = FakeAdapter("claude_code", claude_handler)
    codex = FakeAdapter("codex", codex_handler)
    mesh = make_mesh(tmp_path, adapters={"claude_code": claude, "codex": codex})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        write_codex_config(mesh.home_dir, model="gpt-5-codex")

        request = TaskRequest(
            target_harness="claude_code",
            objective="Add two deterministic modules to the project.",
            difficulty="medium",
            fallback_harnesses=["codex"],
            max_failover_attempts=3,
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        result = await mesh.router.dispatch(request)

        assert result.status == TaskStatus.COMPLETED
        assert result.initial_harness == "claude_code"
        assert result.target_harness == "codex"
        assert result.failover_count == 1
        assert result.failover_history[0].from_harness == "claude_code"
        assert result.failover_history[0].to_harness == "codex"
        assert "quota" in result.failover_history[0].reason
        assert [assignment.status for assignment in result.assignments] == ["ended_quota", "completed"]
        assert result.assignments[0].error_reason
        assert result.native_session_id == "codex-session-1"

        assert "alpha.py" in result.modified_files
        assert "beta.py" in result.modified_files
        assert "ALPHA" in result.git_diff
        assert "BETA" in result.git_diff

        worktree = mesh.workspace.worktree_path(request.task_id)
        assert (worktree / "alpha.py").exists()
        assert (worktree / "beta.py").exists()

        diff_resource = mesh.router.read_task_resource(request.task_id, "diff")
        assert "ALPHA" in diff_resource["content"]
        assert "BETA" in diff_resource["content"]
        assignments_resource = mesh.router.read_task_resource(request.task_id, "assignments")
        assignments_payload = json.loads(assignments_resource["content"])
        assert len(assignments_payload["assignments"]) == 2
        assert len(assignments_payload["failover_history"]) == 1

        record = mesh.storage.get_task(request.task_id)
        assert record["checkpoint"] is None
        status = mesh.router.get_task_status(request.task_id)
        assert status["blocked_harness"] is None
        assert status["blocked_model"] is None

        circuit = mesh.guardian.get_circuit("claude_code", "claude-opus-9")
        assert circuit is not None
        assert circuit.state == CircuitState.OPEN
        assert circuit.cooldown_until and circuit.exhausted_at
        assert circuit.cooldown_until - circuit.exhausted_at >= 10

        applied = mesh.router.apply_task_patch(request.task_id)
        assert applied["applied_files"]
        assert (mesh.workspace_root / "alpha.py").read_text(encoding="utf-8").startswith("ALPHA")
        assert (mesh.workspace_root / "beta.py").read_text(encoding="utf-8").startswith("BETA")
    finally:
        mesh.storage.close()


def test_effects_queue_is_idempotent_and_rearms_after_completion(tmp_path):
    from harness_mesh.storage import StateStore

    storage = StateStore(tmp_path / "state.db")
    try:
        assert storage.enqueue_effect("resume_waiting", "task-1", {"task_id": "task-1"}) is True
        assert storage.enqueue_effect("resume_waiting", "task-1", {"task_id": "task-1"}) is False
        effects = storage.list_effects()
        assert len(effects) == 1
        assert effects[0]["status"] == "pending"

        storage.mark_effect_done_by_key("resume_waiting", "task-1")
        assert storage.enqueue_effect("resume_waiting", "task-1", {"task_id": "task-1"}) is True
        effect = storage.get_effect("resume_waiting", "task-1")
        assert effect is not None
        assert effect["status"] == "pending"
        assert effect["attempts"] == 0

        storage.mark_effect_running(effect["id"])
        storage.mark_effect_failed(effect["id"], retry_delay=2)
        failed = storage.get_effect("resume_waiting", "task-1")
        assert failed is not None
        assert failed["status"] == "pending"
        assert failed["next_run_at"] > failed["updated_at"]
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_cancel_waiting_task_releases_the_queue(tmp_path):
    def claude_handler(adapter, call):
        return quota_failure("Budget has been exceeded: budget_exceeded", exit_code=1)

    mesh = make_mesh(
        tmp_path,
        adapters={"claude_code": FakeAdapter("claude_code", claude_handler)},
        probe=lambda harness: (False, "still exhausted"),
    )
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        request = TaskRequest(
            target_harness="claude_code",
            objective="This ticket will be cancelled while waiting.",
            on_quota_exhausted=QuotaExhaustedPolicy.WAIT_AND_RESUME,
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=10,
        )
        result = await mesh.router.dispatch(request)
        assert result.status == TaskStatus.WAITING_FOR_QUOTA

        cancelled = await mesh.router.cancel_task(request.task_id)
        assert cancelled["status"] == "cancelled"
        record = mesh.storage.get_task(request.task_id)
        assert record["status"] == "cancelled"
        assert record["result"].status == TaskStatus.CANCELLED
        waiting = await mesh.router.get_quota_status()
        assert waiting["waiting_for_quota"] == []
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_default_probe_ignores_local_models_and_requires_a_provider_probe(tmp_path):
    fake = FakeAdapter("claude_code", lambda adapter, call: success("never executed"))
    mesh = make_mesh(tmp_path, adapters={"claude_code": fake})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        await mesh.discovery.discover_for_harness("claude_code", force_refresh=True)
        assert mesh.discovery.authorized_models("claude_code"), "local discovery must be able to see a model"

        mesh.guardian.record_exhaustion(
            "claude_code",
            "claude-opus-9",
            "rate_limit_429",
            is_account_wide=False,
            cooldown_seconds=1,
        )
        results = await mesh.monitor.check_and_recover_all(force=True)
        assert results and results[0]["success"] is False
        circuit = mesh.guardian.get_circuit("claude_code", "claude-opus-9")
        assert circuit is not None
        assert circuit.state == CircuitState.OPEN, "local config alone must not close a quota circuit"

        async def provider_probe(model=None):
            return True, f"provider responded for {model}"

        fake.probe_quota = provider_probe
        results = await mesh.monitor.check_and_recover_all(force=True)
        assert any(item.get("success") for item in results)
        circuit = mesh.guardian.get_circuit("claude_code", "claude-opus-9")
        assert circuit is not None
        assert circuit.state == CircuitState.CLOSED
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_background_effects_worker_consumes_queue_and_reclaims_stale_rows(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        mesh.storage.enqueue_effect("launch", "manual-task", {"task_id": "manual-task"})
        await mesh.router.start_effects_worker(interval_seconds=0.5)
        try:
            for _ in range(20):
                effect = mesh.storage.get_effect("launch", "manual-task")
                if effect is not None and effect["status"] == "done":
                    break
                await asyncio.sleep(0.05)
            effect = mesh.storage.get_effect("launch", "manual-task")
            assert effect is not None and effect["status"] == "done"
            assert mesh.router.effects_worker_running is True
        finally:
            await mesh.router.stop_effects_worker()
        assert mesh.router.effects_worker_running is False

        mesh.storage.enqueue_effect("quota_probe", "claude_code::*", {})
        pending = mesh.storage.get_effect("quota_probe", "claude_code::*")
        assert pending is not None
        mesh.storage.mark_effect_running(pending["id"])
        reclaimed = mesh.storage.reset_stale_effects(older_than_seconds=-1)
        assert reclaimed == 1
        reclaimed_row = mesh.storage.get_effect("quota_probe", "claude_code::*")
        assert reclaimed_row is not None and reclaimed_row["status"] == "pending"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_wait_and_resume_task_wakes_after_quota_probe(tmp_path):
    state = {"calls": 0}
    probe_calls: list[str] = []

    def claude_handler(adapter, call):
        state["calls"] += 1
        if state["calls"] == 1:
            (call.workdir / "partial.txt").write_text("partial work from assignment 1\n", encoding="utf-8")
            return quota_failure("Budget has been exceeded: budget_exceeded", exit_code=1)
        assert (call.workdir / "partial.txt").exists()
        assert "<continuation_checkpoint>" in call.prompt
        (call.workdir / "final.txt").write_text("resumed work from assignment 2\n", encoding="utf-8")
        return success("resumed and finished")

    async def probe(harness: str):
        probe_calls.append(harness)
        return True, "quota restored"

    claude = FakeAdapter("claude_code", claude_handler)
    mesh = make_mesh(tmp_path, adapters={"claude_code": claude}, probe=probe)
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        request = TaskRequest(
            target_harness="claude_code",
            objective="Produce a partial and then final artefact.",
            difficulty="medium",
            on_quota_exhausted=QuotaExhaustedPolicy.WAIT_AND_RESUME,
            max_failover_attempts=0,  # suspensions must not consume the budget
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        waiting = await mesh.router.dispatch(request)

        assert waiting.status == TaskStatus.WAITING_FOR_QUOTA
        assert state["calls"] == 1
        record = mesh.storage.get_task(request.task_id)
        assert record["status"] == "waiting_for_quota"
        assert record["blocked_harness"] == "claude_code"
        assert record["blocked_model"] == "claude-opus-9"
        assert record["checkpoint"] is not None
        assert record["checkpoint"].partial_modified_files == ["partial.txt"]

        account_circuit = mesh.guardian.get_circuit("claude_code", "*")
        assert account_circuit is not None
        assert account_circuit.state == CircuitState.OPEN

        waiting_effects = [effect for effect in mesh.storage.list_effects() if effect["kind"] == "resume_waiting"]
        assert len(waiting_effects) == 1
        assert waiting_effects[0]["status"] == "pending"

        probe_results = await mesh.monitor.check_and_recover_all(force=True)
        assert any(item.get("success") for item in probe_results)
        assert probe_calls == ["claude_code"]
        assert state["calls"] == 2

        record = mesh.storage.get_task(request.task_id)
        assert record["status"] == "completed"
        completed = record["result"]
        assert completed is not None
        assert completed.status == TaskStatus.COMPLETED
        assert [assignment.status for assignment in completed.assignments] == ["ended_quota", "completed"]
        assert "partial.txt" in completed.modified_files
        assert "final.txt" in completed.modified_files
        assert record["checkpoint"] is None
        assert record["blocked_harness"] is None
        assert record["blocked_model"] is None

        transcript = mesh.storage.get_resource(request.task_id, "transcript") or ""
        assert "Assignment #1" in transcript
        assert "Assignment #2" in transcript
        assert "Budget has been exceeded" in transcript

        account_circuit = mesh.guardian.get_circuit("claude_code", "*")
        assert account_circuit is not None
        assert account_circuit.state == CircuitState.CLOSED

        quota_status = await mesh.router.get_quota_status()
        assert quota_status["waiting_for_quota"] == []
    finally:
        mesh.storage.close()
