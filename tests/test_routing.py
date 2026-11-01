"""Task-kind tiers, market-aware routing, and dispatch integration."""

from __future__ import annotations

import json
import time

import pytest
from pydantic import ValidationError

from harness_mesh.config import RoutingConfig, RoutingPinConfig
from harness_mesh.market import MarketCatalog, MarketModel
from harness_mesh.models import (
    DiscoveredModel,
    TaskDifficulty,
    TaskKind,
    TaskRequest,
    TaskStatus,
)
from harness_mesh.routing import (
    RouteCandidate,
    RoutePlanner,
    build_candidates,
    infer_task_kind,
    tier_for,
)
from tests.helpers import FakeAdapter, make_mesh, quota_failure, success

# ---------------------------------------------------------------------------
# task kind and tier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("objective", "expected"),
    [
        ("设计一个数据库迁移方案", TaskKind.PLAN),
        ("Design the module architecture", TaskKind.PLAN),
        ("Audit the authentication flow", TaskKind.AUDIT),
        ("对这一版做安全审计", TaskKind.AUDIT),
        ("Review this patch for regressions", TaskKind.REVIEW),
        ("评审这次发布", TaskKind.REVIEW),
        ("重构路由模块", TaskKind.REFACTOR),
        ("Debug the flaky queue test", TaskKind.DEBUG),
        ("Write tests for the parser", TaskKind.TEST),
        ("Update the README docs", TaskKind.DOCS),
        ("Implement the new CLI flag", TaskKind.IMPLEMENT),
    ],
)
def test_task_kind_keywords(objective, expected):
    assert infer_task_kind(objective, TaskDifficulty.MEDIUM) == expected


def test_task_kind_falls_back_to_difficulty_and_accepts_explicit():
    assert infer_task_kind("do the work", TaskDifficulty.CRITICAL) == TaskKind.AUDIT
    assert infer_task_kind("do the work", TaskDifficulty.HIGH) == TaskKind.REFACTOR
    assert infer_task_kind("do the work", TaskDifficulty.MEDIUM) == TaskKind.IMPLEMENT
    assert infer_task_kind("do the work", TaskDifficulty.LOW, TaskKind.REVIEW) == TaskKind.REVIEW
    assert infer_task_kind("write tests", TaskDifficulty.LOW, TaskKind.AUTO) == TaskKind.TEST


def test_tier_matrix():
    assert tier_for(TaskKind.PLAN, TaskDifficulty.LOW).value == "premium"
    assert tier_for(TaskKind.AUDIT, TaskDifficulty.LOW).value == "premium"
    assert tier_for(TaskKind.REVIEW, TaskDifficulty.LOW).value == "premium"
    assert tier_for(TaskKind.IMPLEMENT, TaskDifficulty.CRITICAL).value == "premium"
    assert tier_for(TaskKind.REFACTOR, TaskDifficulty.LOW).value == "balanced"
    assert tier_for(TaskKind.DEBUG, TaskDifficulty.LOW).value == "balanced"
    assert tier_for(TaskKind.IMPLEMENT, TaskDifficulty.HIGH).value == "balanced"
    assert tier_for(TaskKind.IMPLEMENT, TaskDifficulty.MEDIUM).value == "value"
    assert tier_for(TaskKind.TEST, TaskDifficulty.LOW).value == "value"
    assert tier_for(TaskKind.DOCS, TaskDifficulty.LOW).value == "value"


# ---------------------------------------------------------------------------
# planner
# ---------------------------------------------------------------------------


def _candidate(
    model: str,
    intelligence: float,
    price: float | None,
    *,
    harness: str = "codex",
    effort: str | None = None,
    key: str | None = None,
    source: str = "artificial_analysis",
) -> RouteCandidate:
    return RouteCandidate(
        harness=harness,
        model=model,
        base_model=model,
        effort_variant=effort,
        intelligence=intelligence,
        intelligence_source=source,
        market_key=key or f"vendor/{model}",
        confidence="exact",
        input_per_mtok=price,
        output_per_mtok=price,
        blended_price=price,
    )


def _planner(**kwargs) -> RoutePlanner:
    return RoutePlanner(**kwargs)


def test_plan_tier_selects_the_highest_intelligence():
    decision = _planner().plan(
        [
            _candidate("smart", 90, 20),
            _candidate("cheap", 60, 1),
        ],
        task_kind=TaskKind.PLAN,
        difficulty=TaskDifficulty.LOW,
    )
    assert decision.tier == "premium"
    assert decision.chosen is not None
    assert decision.chosen.model == "smart"
    assert decision.task_kind == "plan"
    assert decision.mode == "auto"
    assert len(decision.alternatives) == 1
    assert decision.data.confidence == "exact"


def test_premium_ties_break_on_the_lower_blended_price():
    decision = _planner().plan(
        [
            _candidate("pricey", 80, 10),
            _candidate("cheaper", 80, 2),
        ],
        task_kind=TaskKind.AUDIT,
        difficulty=TaskDifficulty.MEDIUM,
    )
    assert decision.chosen is not None
    assert decision.chosen.model == "cheaper"


def test_value_tier_picks_the_cheapest_candidate_above_the_floor():
    candidates = [
        _candidate("best", 80, 10),
        _candidate("balanced", 60, 1),
        _candidate("too-small-medium", 45, 0.1),
        _candidate("too-small-low", 40, 0.05),
    ]
    medium = _planner().plan(
        candidates, task_kind=TaskKind.IMPLEMENT, difficulty=TaskDifficulty.MEDIUM
    )
    low = _planner().plan(
        candidates, task_kind=TaskKind.IMPLEMENT, difficulty=TaskDifficulty.LOW
    )
    assert medium.chosen is not None and medium.chosen.model == "balanced"
    # 45 >= 80 * 0.55 = 44, so the low floor admits the small candidate.
    assert low.chosen is not None and low.chosen.model == "too-small-medium"


def test_balanced_tier_uses_efficiency_above_eighty_five_percent():
    decision = _planner().plan(
        [
            _candidate("huge", 100, 10),
            _candidate("efficient", 88, 1),
            _candidate("too-small", 80, 0.1),
        ],
        task_kind=TaskKind.REFACTOR,
        difficulty=TaskDifficulty.MEDIUM,
    )
    assert decision.tier == "balanced"
    assert decision.chosen is not None
    assert decision.chosen.model == "efficient"


def test_unknown_price_is_not_ranked_as_zero():
    decision = _planner().plan(
        [
            _candidate("unknown", 95, None),
            _candidate("priced", 60, 2),
        ],
        task_kind=TaskKind.IMPLEMENT,
        difficulty=TaskDifficulty.MEDIUM,
    )
    assert decision.chosen is not None and decision.chosen.model == "priced"
    assert any("known price" in reason for reason in decision.reasons)

    only_unknown = _planner().plan(
        [_candidate("unknown", 95, None), _candidate("unknown-two", 70, None)],
        task_kind=TaskKind.IMPLEMENT,
        difficulty=TaskDifficulty.MEDIUM,
    )
    assert only_unknown.chosen is not None and only_unknown.chosen.model == "unknown"


def test_strict_value_hints_do_not_mix_glm_variants():
    candidates = [
        _candidate("glm-5.3", 80, 10, harness="zcode", key="zai/glm-5-3"),
        _candidate("glm-5.3-flash", 70, 0.2, harness="zcode", key="zai/glm-5-3-flash"),
    ]
    plain = _planner(
        value_hints=["glm-5.3"], strict_hints=True
    ).plan(candidates, task_kind=TaskKind.IMPLEMENT, difficulty=TaskDifficulty.MEDIUM)
    flash = _planner(
        value_hints=["glm-5.3-flash"], strict_hints=True
    ).plan(candidates, task_kind=TaskKind.IMPLEMENT, difficulty=TaskDifficulty.MEDIUM)

    assert plain.chosen is not None and plain.chosen.model == "glm-5.3"
    assert flash.chosen is not None and flash.chosen.model == "glm-5.3-flash"


def test_strict_hints_with_no_match_return_no_choice():
    decision = _planner(premium_hints=["claude-opus*"], strict_hints=True).plan(
        [_candidate("gpt-6-sol", 90, 5)],
        task_kind=TaskKind.PLAN,
        difficulty=TaskDifficulty.LOW,
    )
    assert decision.chosen is None
    assert any("strict hints" in reason for reason in decision.reasons)


def test_soft_hints_add_weight_without_filtering():
    decision = _planner(premium_hints=["claude-opus*"]).plan(
        [
            _candidate("claude-sonnet-5", 80, 10),
            _candidate("claude-opus-5-5", 77, 10),
        ],
        task_kind=TaskKind.PLAN,
        difficulty=TaskDifficulty.LOW,
    )
    assert decision.chosen is not None
    assert decision.chosen.model == "claude-opus-5-5"


def test_pins_override_the_ranking():
    pin = RoutingPinConfig(harness="claude_code", model="claude-opus-5-5")
    decision = _planner(pins={"plan": pin}).plan(
        [
            _candidate("gpt-6-astra", 95, 10, harness="codex"),
            _candidate("claude-opus-5-5", 70, 20, harness="claude_code"),
        ],
        task_kind=TaskKind.PLAN,
        difficulty=TaskDifficulty.LOW,
    )
    assert decision.chosen is not None
    assert (decision.chosen.harness, decision.chosen.model) == (
        "claude_code",
        "claude-opus-5-5",
    )


def test_constraints_and_explicit_tier():
    decision = _planner().plan(
        [
            _candidate("expensive", 90, 20),
            _candidate("cheap", 60, 1),
        ],
        task_kind=TaskKind.PLAN,
        difficulty=TaskDifficulty.LOW,
        max_price_per_mtok=5,
    )
    assert decision.chosen is not None and decision.chosen.model == "cheap"

    forced = _planner().plan(
        [_candidate("smart", 90, 20), _candidate("cheap", 60, 1)],
        task_kind=TaskKind.PLAN,
        difficulty=TaskDifficulty.LOW,
        tier="value",
    )
    assert forced.tier == "value"
    assert forced.chosen is not None and forced.chosen.model == "cheap"


def test_decision_contains_no_secret_and_has_every_documented_field():
    catalog = MarketCatalog(
        sources=["openrouter", "artificial_analysis"],
        fetched_at=123.0,
        stale=True,
    )
    decision = _planner().plan(
        [_candidate("alpha", 80, 5), _candidate("beta", 70, 2)],
        task_kind=TaskKind.REVIEW,
        difficulty=TaskDifficulty.LOW,
        catalog=catalog,
    )
    payload = decision.model_dump(mode="json")
    assert set(payload) == {
        "mode",
        "task_kind",
        "tier",
        "chosen",
        "alternatives",
        "reasons",
        "axis_scores",
        "benchmarks_used",
        "data",
    }
    assert set(payload["chosen"]) == {
        "harness",
        "model",
        "effort",
        "intelligence",
        "intelligence_source",
        "intelligence_reasoning",
        "intelligence_coding",
        "input_per_mtok",
        "output_per_mtok",
        "blended_price",
        "market_key",
        "confidence",
    }
    assert payload["data"]["sources"] == ["openrouter", "artificial_analysis"]
    assert payload["data"]["stale"] is True
    assert payload["data"]["matched_key"] == "vendor/alpha"
    serialized = json.dumps(payload).lower()
    assert "api_key" not in serialized and "secret" not in serialized


# ---------------------------------------------------------------------------
# candidate building
# ---------------------------------------------------------------------------


def _market(key: str, intelligence: float, input_price: float, output_price: float) -> MarketModel:
    return MarketModel(
        key=key,
        vendor=key.split("/", 1)[0],
        name=key.split("/", 1)[1],
        input_per_mtok=input_price,
        output_per_mtok=output_price,
        intelligence=intelligence,
        intelligence_source="artificial_analysis",
        reasoning=True,
        sources=["artificial_analysis"],
    )


def _discovered(harness: str, model_id: str, capability: float = 30.0) -> DiscoveredModel:
    return DiscoveredModel(
        harness=harness,
        model_id=model_id,
        source="local_config",
        capability_score=capability,
        discovered_at=time.time(),
    )


def _routing_models() -> list[MarketModel]:
    return [
        _market("google/gemini-4-argon", 80, 2, 10),
        _market("zai/glm-5-3", 70, 0.7, 2.8),
        _market("zai/glm-5-3-flash", 60, 0.01, 0.02),
        _market("zai/glm-5-3-flashx", 62, 0.37, 1.25),
        _market("deepseek/deepseek-v4-1-flash", 50, 0.045, 0.5),
        _market("alibaba/qwen3-8-max", 65, 2, 6),
    ]


def _variant_models() -> list[DiscoveredModel]:
    return [
        _discovered("antigravity", "gemini-4-argon-high"),
        _discovered("antigravity", "gemini-4-argon-medium"),
        _discovered("antigravity", "gemini-4-argon-low"),
        _discovered("zcode", "GLM-5.3"),
        _discovered("zcode", "GLM-5.3-Flash"),
        _discovered("zcode", "glm-5.3-flashx"),
        _discovered("zcode", "qwen3.8-max"),
        _discovered("deepseek", "deepseek-v4.1-flash"),
    ]


def test_build_candidates_collapses_effort_variants_per_tier():
    models = _variant_models()
    catalog = _routing_models()

    premium = {
        (candidate.harness, candidate.model): candidate
        for candidate in build_candidates(models, catalog, tier="premium")
    }
    balanced = {
        (candidate.harness, candidate.model): candidate
        for candidate in build_candidates(models, catalog, tier="balanced")
    }
    value = {
        (candidate.harness, candidate.model): candidate
        for candidate in build_candidates(models, catalog, tier="value")
    }

    assert premium[("antigravity", "gemini-4-argon-high")].effort_variant == "high"
    assert balanced[("antigravity", "gemini-4-argon-medium")].effort_variant == "medium"
    assert value[("antigravity", "gemini-4-argon-low")].effort_variant == "low"
    assert len([key for key in premium if key[0] == "antigravity"]) == 1

    # A real ``-max`` model name is not an effort variant.
    qwen = next(candidate for candidate in premium.values() if candidate.model == "qwen3.8-max")
    assert qwen.market_key == "alibaba/qwen3-8-max"
    assert qwen.effort_variant is None


def test_build_candidates_respects_explicit_effort_and_circuits():
    models = _variant_models()
    catalog = _routing_models()
    preferred = {
        candidate.model: candidate
        for candidate in build_candidates(
            models, catalog, tier="premium", preferred_effort="low"
        )
    }
    assert preferred["gemini-4-argon-low"].effort_variant == "low"

    available = {
        candidate.model: candidate
        for candidate in build_candidates(
            models,
            catalog,
            tier="premium",
            available=lambda harness, model: model != "gemini-4-argon-high",
        )
    }
    assert "gemini-4-argon-medium" in available
    assert "gemini-4-argon-high" not in available


# ---------------------------------------------------------------------------
# router integration
# ---------------------------------------------------------------------------


def _seed(mesh, discovered: list[DiscoveredModel], market_models: list[MarketModel]) -> None:
    mesh.storage.save_discovered_models(discovered)
    now = time.time()
    mesh.storage.save_market_snapshot(
        "artificial_analysis",
        now,
        {
            "models": [model.model_dump(mode="json") for model in market_models],
            "fetched_at": now,
        },
    )


def _two_harnesses(tmp_path, *, claude_handler=None, codex_handler=None):
    claude = FakeAdapter("claude_code", claude_handler or (lambda adapter, call: success("claude done")))
    codex = FakeAdapter("codex", codex_handler or (lambda adapter, call: success("codex done")))
    return make_mesh(tmp_path, adapters={"claude_code": claude, "codex": codex}), claude, codex


def _standard_seed(mesh, *, opus_intelligence: float = 80.0, astra_intelligence: float = 90.0):
    _seed(
        mesh,
        [
            _discovered("claude_code", "claude-opus-5-5", 40.0),
            _discovered("codex", "gpt-6-astra", 35.0),
        ],
        [
            _market("anthropic/claude-opus-5-5", opus_intelligence, 4, 20),
            _market("openai/gpt-6-astra", astra_intelligence, 10, 50),
        ],
    )


@pytest.mark.asyncio
async def test_plan_routes_to_the_highest_intelligence_across_harnesses(tmp_path):
    mesh, claude, codex = _two_harnesses(tmp_path)
    try:
        _standard_seed(mesh)
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="auto",
                objective="Design the new routing architecture",
                difficulty="high",
                task_kind="plan",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED, result.summary
        assert result.resolved_model == "gpt-6-astra"
        assert result.target_harness == "codex"
        assert result.routing is not None
        assert result.routing["tier"] == "premium"
        assert result.routing["chosen"]["model"] == "gpt-6-astra"
        assert result.routing["chosen"]["intelligence"] == 90.0
        assert result.routing["data"]["confidence"] == "exact"
        assert codex.calls and codex.calls[0].model == "gpt-6-astra"
        assert not claude.calls
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_implement_routes_to_the_cheapest_candidate_above_the_floor(tmp_path):
    mesh, claude, codex = _two_harnesses(tmp_path)
    try:
        _standard_seed(mesh)
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="auto",
                objective="Implement the new CLI flag",
                difficulty="medium",
                task_kind="implement",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED, result.summary
        assert result.routing is not None
        assert result.routing["tier"] == "value"
        assert result.resolved_model == "claude-opus-5-5"
        assert claude.calls and claude.calls[0].model == "claude-opus-5-5"
        assert not codex.calls
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_specified_harness_routing_stays_inside_that_harness(tmp_path):
    mesh, claude, codex = _two_harnesses(tmp_path)
    try:
        _standard_seed(mesh)
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="Design the release plan",
                difficulty="high",
                task_kind="plan",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED, result.summary
        assert result.resolved_model == "claude-opus-5-5"
        assert result.target_harness == "claude_code"
        assert not codex.calls
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_open_circuit_models_are_not_selected(tmp_path):
    mesh, claude, codex = _two_harnesses(tmp_path)
    try:
        _standard_seed(mesh)
        mesh.guardian.record_exhaustion("codex", "gpt-6-astra", "quota", retry_after_seconds=3600)
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="auto",
                objective="Audit the design",
                difficulty="high",
                task_kind="audit",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED, result.summary
        assert result.resolved_model == "claude-opus-5-5"
        assert claude.calls and not codex.calls
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_explicit_model_bypasses_routing(tmp_path):
    mesh, claude, codex = _two_harnesses(tmp_path)
    try:
        _standard_seed(mesh, opus_intelligence=95.0, astra_intelligence=50.0)
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="codex",
                objective="Design the release plan",
                difficulty="high",
                task_kind="plan",
                model="gpt-6-astra",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED, result.summary
        assert result.resolved_model == "gpt-6-astra"
        assert result.routing is not None
        assert result.routing["mode"] == "manual"
        assert result.routing["chosen"] is None
        assert codex.calls and codex.calls[0].model == "gpt-6-astra"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_routing_off_uses_the_existing_difficulty_tier(tmp_path):
    mesh, claude, _codex = _two_harnesses(tmp_path)
    try:
        _standard_seed(mesh)
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="Design the release plan",
                difficulty="critical",
                task_kind="plan",
                routing_mode="off",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED, result.summary
        assert result.routing is not None
        assert result.routing["mode"] == "off"
        expected = mesh.discovery.resolve_model("claude_code", "critical")
        assert expected is not None
        assert result.resolved_model == expected.model_id
        assert claude.calls and claude.calls[0].model == expected.model_id
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_no_market_data_falls_back_to_the_existing_heuristic(tmp_path):
    mesh, claude, _codex = _two_harnesses(tmp_path)
    try:
        mesh.storage.save_discovered_models(
            [
                _discovered("claude_code", "claude-opus-5-5", 55.3),
                _discovered("claude_code", "claude-sonnet-5-5", 40.0),
            ]
        )
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="Design the release plan",
                difficulty="critical",
                task_kind="plan",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        expected = mesh.discovery.resolve_model("claude_code", "critical")
        assert expected is not None
        assert result.status == TaskStatus.COMPLETED, result.summary
        assert result.resolved_model == expected.model_id
        assert result.routing is not None
        assert result.routing["chosen"] is None
        assert any("no market data" in reason for reason in result.routing["reasons"])
        assert claude.calls
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_effort_variants_set_reasoning_effort(tmp_path):
    adapter = FakeAdapter("antigravity", lambda adapter, call: success("antigravity done"))
    mesh = make_mesh(tmp_path, adapters={"antigravity": adapter})
    try:
        _seed(
            mesh,
            [
                _discovered("antigravity", "gemini-4-argon-high", 30.0),
                _discovered("antigravity", "gemini-4-argon-medium", 30.0),
                _discovered("antigravity", "gemini-4-argon-low", 30.0),
            ],
            [_market("google/gemini-4-argon", 80, 2, 10)],
        )
        plan = await mesh.router.dispatch(
            TaskRequest(
                target_harness="antigravity",
                objective="Design the migration plan",
                difficulty="high",
                task_kind="plan",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert plan.status == TaskStatus.COMPLETED, plan.summary
        assert plan.resolved_model == "gemini-4-argon-high"
        assert plan.resolved_reasoning_effort == "high"
        assert adapter.calls[-1].model == "gemini-4-argon-high"
        assert adapter.calls[-1].reasoning_effort == "high"

        value = await mesh.router.dispatch(
            TaskRequest(
                target_harness="antigravity",
                objective="Implement the migration script",
                difficulty="medium",
                task_kind="implement",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert value.status == TaskStatus.COMPLETED, value.summary
        assert value.resolved_model == "gemini-4-argon-low"
        assert value.resolved_reasoning_effort == "low"

        explicit = await mesh.router.dispatch(
            TaskRequest(
                target_harness="antigravity",
                objective="Design the migration plan",
                difficulty="high",
                task_kind="plan",
                reasoning_effort="low",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert explicit.status == TaskStatus.COMPLETED, explicit.summary
        assert explicit.resolved_model == "gemini-4-argon-low"
        assert explicit.resolved_reasoning_effort == "low"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_failover_reuses_the_same_tier_ordering(tmp_path):
    claude = FakeAdapter("claude_code", lambda adapter, call: success("claude fallback"))
    codex = FakeAdapter("codex", lambda adapter, call: quota_failure())
    mesh = make_mesh(tmp_path, adapters={"claude_code": claude, "codex": codex})
    try:
        _seed(
            mesh,
            [
                _discovered("claude_code", "claude-opus-5-5", 40.0),
                _discovered("codex", "gpt-6-astra", 35.0),
                _discovered("deepseek", "deepseek-v4.1-flash", 20.0),
            ],
            [
                _market("anthropic/claude-opus-5-5", 80, 4, 20),
                _market("openai/gpt-6-astra", 90, 10, 50),
                _market("deepseek/deepseek-v4-1-flash", 50, 0.045, 0.5),
            ],
        )
        mesh.router.adapters["deepseek"] = FakeAdapter(
            "deepseek", lambda adapter, call: success("deepseek should not run")
        )
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="auto",
                objective="Design the new module",
                difficulty="high",
                task_kind="plan",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
                max_failover_attempts=1,
            )
        )
        assert result.status == TaskStatus.COMPLETED, result.summary
        assert result.resolved_model == "claude-opus-5-5"
        assert result.failover_count == 1
        hop = result.failover_history[0]
        assert (hop.from_harness, hop.from_model) == ("codex", "gpt-6-astra")
        assert (hop.to_harness, hop.to_model) == ("claude_code", "claude-opus-5-5")
        assert codex.calls and claude.calls
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_routing_record_is_persisted_and_rendered_in_the_transcript(tmp_path):
    mesh, _claude, _codex = _two_harnesses(tmp_path)
    try:
        _standard_seed(mesh)
        request = TaskRequest(
            target_harness="auto",
            objective="Design the release plan",
            difficulty="high",
            task_kind="plan",
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        result = await mesh.router.dispatch(request)
        assert result.status == TaskStatus.COMPLETED, result.summary
        stored = mesh.storage.get_task(request.task_id)
        assert stored["result"] is not None
        assert stored["result"].routing is not None
        transcript = mesh.router.read_task_resource(request.task_id, "transcript")
        assert "=== Routing ===" in transcript["content"]
        assert "gpt-6-astra" in transcript["content"]
        assert "prices are API list prices" in transcript["content"]
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_routing_never_echoes_the_artificial_analysis_key(tmp_path):
    mesh, _claude, _codex = _two_harnesses(
        tmp_path, claude_handler=lambda adapter, call: success("done")
    )
    try:
        _standard_seed(mesh)
        mesh.router.market.env["ARTIFICIAL_ANALYSIS_API_KEY"] = "top-secret-key"
        request = TaskRequest(
            target_harness="auto",
            objective="Design the release plan",
            difficulty="high",
            task_kind="plan",
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        result = await mesh.router.dispatch(request)
        assert result.status == TaskStatus.COMPLETED, result.summary
        blob = json.dumps(result.model_dump(mode="json"))
        assert "top-secret-key" not in blob
        transcript = mesh.router.read_task_resource(request.task_id, "transcript")
        assert "top-secret-key" not in transcript["content"]
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_preview_routing_does_not_create_a_ticket_or_run_an_adapter(tmp_path):
    mesh, claude, _codex = _two_harnesses(tmp_path)
    try:
        _standard_seed(mesh)
        preview = await mesh.router.preview_routing(
            "Audit the authentication design", task_kind="auto", difficulty="high"
        )
        assert preview["task_kind"] == "audit"
        assert preview["tier"] == "premium"
        assert preview["market_models"] == 2
        assert preview["routing"]["chosen"]["model"] == "gpt-6-astra"
        assert mesh.storage.list_tasks() == []
        assert not claude.calls
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_environment_mode_overrides_the_configured_default(tmp_path):
    mesh, _claude, _codex = _two_harnesses(
        tmp_path, claude_handler=lambda adapter, call: success("done")
    )
    try:
        _standard_seed(mesh)
        mesh.config.routing.mode = "off"
        mesh.router.env["HARNESS_MESH_ROUTING"] = "auto"
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="Design the release plan",
                difficulty="high",
                task_kind="plan",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED, result.summary
        assert result.routing is not None
        assert result.routing["mode"] == "auto"
    finally:
        mesh.storage.close()


def test_request_validates_task_kind_and_constraints():
    with pytest.raises(ValidationError):
        TaskRequest(target_harness="codex", objective="x", task_kind="nonsense")
    with pytest.raises(ValidationError):
        TaskRequest(target_harness="codex", objective="x", min_intelligence=101)
    with pytest.raises(ValidationError):
        TaskRequest(target_harness="codex", objective="x", max_price_per_mtok=-1)


def test_routing_config_parses_pins_and_floors(tmp_path):
    config = RoutingConfig(
        mode="auto",
        floors={"low": 0.6, "medium": 0.8},
        premium_hints=["claude-opus*"],
        value_hints=["deepseek-v4.1-flash*"],
        strict_hints=True,
        pins={"plan": {"harness": "claude_code", "model": "claude-opus-5-5"}},
    )
    assert config.pins["plan"].harness == "claude_code"
    assert config.floors["medium"] == 0.8
    assert config.strict_hints is True


def test_planner_honours_custom_floors():
    planner = RoutePlanner(floors={"low": 0.9, "medium": 0.9})
    decision = planner.plan(
        [
            _candidate("best", 100, 10),
            _candidate("cheap", 80, 1),
        ],
        task_kind=TaskKind.IMPLEMENT,
        difficulty=TaskDifficulty.LOW,
    )
    assert decision.chosen is not None
    assert decision.chosen.model == "best"
