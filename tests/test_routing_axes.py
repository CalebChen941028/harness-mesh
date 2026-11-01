"""Task-kind axis weights in routing: reasoning versus coding benchmarks."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from harness_mesh.config import EpochConfig, RoutingConfig
from harness_mesh.market import (
    MarketModel,
    apply_epoch_intelligence,
    merge_market_models,
    parse_epoch_zip,
)
from harness_mesh.models import DiscoveredModel, TaskKind
from harness_mesh.routing import RouteCandidate, RoutePlanner, build_candidates

FIXTURES = Path(__file__).parent / "fixtures"

PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "gpt-6-astra": (10.0, 50.0),
    "gemini-4-argon": (2.0, 10.0),
    "deepseek-v4.1-flash-1017": (0.3, 1.2),
    "glm-5.3": (1.4, 4.4),
    "glm-5.3-flash": (0.15, 0.5),
}


def fixture_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted((FIXTURES / "epoch").glob("*.csv")):
            archive.writestr(path.name, path.read_text(encoding="utf-8"))
    return buffer.getvalue()


def _catalog() -> list[MarketModel]:
    epoch_models = parse_epoch_zip(fixture_zip(), EpochConfig())
    priced = [
        MarketModel(
            key=model.key,
            vendor=model.vendor,
            name=model.name,
            input_per_mtok=PRICES.get(model.name, (1.0, 3.0))[0],
            output_per_mtok=PRICES.get(model.name, (1.0, 3.0))[1],
            sources=["openrouter"],
        )
        for model in epoch_models
    ]
    return apply_epoch_intelligence(merge_market_models([*priced, *epoch_models]))


def _discovered(catalog: list[MarketModel], harness: str = "codex") -> list[DiscoveredModel]:
    return [
        DiscoveredModel(
            harness=harness,
            model_id=model.name,
            source="local_config",
            capability_score=20.0,
        )
        for model in catalog
    ]


def _names(items) -> list[str]:
    return [item.name for item in items]


@pytest.fixture(scope="module")
def catalog() -> list[MarketModel]:
    return _catalog()


# ---------------------------------------------------------------------------
# weights
# ---------------------------------------------------------------------------


def test_axis_weights_follow_the_task_kind():
    planner = RoutePlanner.from_config(RoutingConfig())

    for kind in ("plan", "audit", "review"):
        assert planner.weights_for(kind) == {"reasoning": 0.7, "coding": 0.3}
    for kind in ("implement", "refactor", "debug", "test"):
        assert planner.weights_for(kind) == {"reasoning": 0.3, "coding": 0.7}
    assert planner.weights_for("docs") == {"reasoning": 1.0, "coding": 0.0}
    # Unknown kinds keep the balanced default instead of failing.
    assert planner.weights_for("something-new") == {"reasoning": 0.5, "coding": 0.5}


def test_axis_weights_are_configurable():
    config = RoutingConfig(axis_weights={"docs": {"reasoning": 0.2, "coding": 0.8}})
    planner = RoutePlanner.from_config(config)
    assert planner.weights_for("docs") == {"reasoning": 0.2, "coding": 0.8}


def test_missing_axis_takes_the_other_axis_full_weight():
    planner = RoutePlanner()
    reasoning_only = RouteCandidate(
        harness="codex",
        model="reasoning-only",
        intelligence=50.0,
        intelligence_reasoning=90.0,
    )
    coding_only = RouteCandidate(
        harness="codex",
        model="coding-only",
        intelligence=50.0,
        intelligence_coding=80.0,
    )
    docs = {"reasoning": 1.0, "coding": 0.0}

    assert planner.task_intelligence(reasoning_only, docs) == pytest.approx(90.0)
    assert planner.task_intelligence(coding_only, docs) == pytest.approx(80.0)

    decision = planner.plan(
        [reasoning_only, coding_only],
        task_kind="docs",
        difficulty="low",
        tier="premium",
    )
    assert decision.chosen is not None
    assert decision.chosen.model == "reasoning-only"
    assert decision.axis_scores["coding"]["score"] is None


def test_task_weighted_score_mixes_both_axes():
    planner = RoutePlanner()
    candidate = RouteCandidate(
        harness="codex",
        model="mixed",
        intelligence=50.0,
        intelligence_reasoning=100.0,
        intelligence_coding=0.0,
    )
    plan = {"reasoning": 0.7, "coding": 0.3}
    implement = {"reasoning": 0.3, "coding": 0.7}
    assert planner.task_intelligence(candidate, plan) == pytest.approx(70.0)
    assert planner.task_intelligence(candidate, implement) == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# decision record
# ---------------------------------------------------------------------------


def test_decision_records_axis_scores_weights_and_benchmarks(catalog):
    candidates = build_candidates(
        _discovered(catalog), catalog, tier="premium"
    )
    decision = RoutePlanner().plan(
        candidates, task_kind="plan", difficulty="high", tier="premium"
    )

    assert decision.chosen is not None
    assert set(decision.axis_scores) == {"reasoning", "coding"}
    assert decision.axis_scores["reasoning"]["weight"] == pytest.approx(0.7)
    assert decision.axis_scores["coding"]["weight"] == pytest.approx(0.3)
    assert decision.axis_scores["reasoning"]["score"] is not None
    assert decision.benchmarks_used
    assert any("reasoning axis" in reason and "coding axis" in reason for reason in decision.reasons)
    assert decision.chosen.intelligence_reasoning is not None
    assert decision.chosen.intelligence_coding is not None


# ---------------------------------------------------------------------------
# fixture behaviour
# ---------------------------------------------------------------------------


def test_plan_picks_the_best_reasoning_weighted_model(catalog):
    candidates = build_candidates(_discovered(catalog), catalog, tier="premium")
    decision = RoutePlanner().plan(
        candidates, task_kind="plan", difficulty="high", tier="premium"
    )
    assert decision.chosen is not None
    assert decision.chosen.model == "gpt-6-astra"
    assert "claude-opus-5-5" in {candidate.model for candidate in candidates}


def test_implement_picks_the_cheapest_model_above_the_coding_floor(catalog):
    candidates = build_candidates(_discovered(catalog), catalog, tier="value")
    decision = RoutePlanner().plan(
        candidates, task_kind="implement", difficulty="low", tier="value"
    )
    assert decision.chosen is not None
    assert decision.chosen.model == "deepseek-v4.1-flash-1017"
    assert decision.chosen.model != "glm-5.3-flash"
    assert decision.chosen.blended_price == pytest.approx((3 * 0.3 + 1.2) / 4)


def test_docs_routing_uses_the_reasoning_axis_alone(catalog):
    candidates = build_candidates(_discovered(catalog), catalog, tier="premium")
    decision = RoutePlanner().plan(
        candidates, task_kind="docs", difficulty="low", tier="premium"
    )
    assert decision.chosen is not None
    assert decision.chosen.model == "gpt-6-astra"
    assert decision.axis_scores["coding"]["weight"] == pytest.approx(0.0)


def test_build_candidates_carries_the_axis_scores(catalog):
    candidates = {candidate.model: candidate for candidate in build_candidates(
        _discovered(catalog), catalog, tier="premium"
    )}
    opus = candidates["claude-opus-5-5"]
    assert opus.intelligence_reasoning is not None
    assert opus.intelligence_coding is not None
    assert opus.benchmarks_used
    assert opus.benchmark_data_date == "2026-10-26"
    assert opus.intelligence_source == "epoch_ai"


def test_task_kind_enum_weights_match_the_string_form():
    planner = RoutePlanner()
    assert planner.weights_for(TaskKind.IMPLEMENT) == planner.weights_for("implement")
