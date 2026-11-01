"""Intelligence precedence, blended price and the heuristic fallback score."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from harness_mesh.config import MarketConfig, MarketOverrideConfig
from harness_mesh.discovery import ModelDiscoveryEngine, score_model_id
from harness_mesh.market import (
    MarketModel,
    MarketService,
    apply_heuristic_intelligence,
    apply_overrides,
    blended_price,
)
from harness_mesh.storage import StateStore


def _model(key: str, **overrides) -> MarketModel:
    payload = {
        "key": key,
        "vendor": key.split("/", 1)[0],
        "name": key.split("/", 1)[1],
        "sources": ["openrouter"],
        "fetched_at": 1_000.0,
    }
    payload.update(overrides)
    return MarketModel(**payload)


def _snapshot(store: StateStore, source: str, models: list[MarketModel]) -> None:
    store.save_market_snapshot(
        source,
        1_000.0,
        {"models": [model.model_dump(mode="json") for model in models], "fetched_at": 1_000.0},
    )


def _service(tmp_path, *, overrides=None, scorer=None, now: float = 1_000.0) -> MarketService:
    store = StateStore(tmp_path / "state.db")
    return MarketService(
        store,
        MarketConfig(
            sources=["openrouter", "artificial_analysis"],
            overrides=overrides or {},
        ),
        env={},
        providers={},
        scorer=scorer,
        now_fn=lambda: now,
    )


def test_intelligence_prefers_override_then_artificial_analysis_then_heuristic(tmp_path):
    service = _service(
        tmp_path,
        overrides={
            "vendor/a": MarketOverrideConfig(intelligence=80.0),
            "vendor/b": MarketOverrideConfig(intelligence=55.0),
        },
        scorer=lambda key: {"vendor/c": 40.0}.get(key, 20.0),
    )
    _snapshot(
        service.storage,
        "openrouter",
        [
            _model("vendor/a", input_per_mtok=1.0, output_per_mtok=2.0),
            _model("vendor/b", input_per_mtok=1.0, output_per_mtok=2.0),
            _model("vendor/c", input_per_mtok=1.0, output_per_mtok=2.0),
        ],
    )
    _snapshot(
        service.storage,
        "artificial_analysis",
        [
            _model(
                "vendor/a",
                intelligence=70.0,
                intelligence_source="artificial_analysis",
                sources=["artificial_analysis"],
            ),
            _model(
                "vendor/d",
                intelligence=70.0,
                intelligence_source="artificial_analysis",
                sources=["artificial_analysis"],
                input_per_mtok=1.0,
                output_per_mtok=2.0,
            ),
        ],
    )

    catalog = service.catalog()
    by_key = {model.key: model for model in catalog.models}

    assert by_key["vendor/a"].intelligence == 80.0
    assert by_key["vendor/a"].intelligence_source == "override"
    assert by_key["vendor/b"].intelligence == 55.0
    assert by_key["vendor/b"].intelligence_source == "override"
    assert by_key["vendor/c"].intelligence is not None
    assert by_key["vendor/c"].intelligence_source == "heuristic"
    assert by_key["vendor/d"].intelligence == 70.0
    assert by_key["vendor/d"].intelligence_source == "artificial_analysis"


def test_blended_price_uses_three_to_one_weighting_and_rejects_partial_data():
    assert blended_price(_model("v/a", input_per_mtok=4.0, output_per_mtok=20.0)) == pytest.approx(8.0)
    assert blended_price(_model("v/a", input_per_mtok=1.0, output_per_mtok=3.0)) == pytest.approx(1.5)
    assert blended_price(_model("v/a", input_per_mtok=0.0, output_per_mtok=0.0)) == 0.0
    assert blended_price(_model("v/a", input_per_mtok=4.0, output_per_mtok=None)) is None
    assert blended_price(_model("v/a", input_per_mtok=None, output_per_mtok=3.0)) is None
    assert blended_price(None) is None


def test_heuristic_combines_capability_price_reasoning_and_recency():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
    old = _model(
        "alpha/one",
        input_per_mtok=1.0,
        output_per_mtok=1.0,
        release_date="2016-01-01",
    )
    fresh = _model(
        "beta/two",
        input_per_mtok=9.0,
        output_per_mtok=9.0,
        release_date="2026-10-01",
        reasoning=True,
    )
    scores = {"alpha/one": 60.0, "beta/two": 30.0}
    scored = apply_heuristic_intelligence([old, fresh], lambda key: scores[key], now=now)
    by_key = {model.key: model for model in scored}

    assert by_key["alpha/one"].intelligence == pytest.approx(60.0)
    # 30 + expensive-price bonus 8 + reasoning bonus 4 + recent release ~5.9
    assert 45.0 < by_key["beta/two"].intelligence < 50.0
    assert by_key["beta/two"].intelligence > by_key["alpha/one"].intelligence - 20
    assert by_key["alpha/one"].intelligence_source == "heuristic"
    assert by_key["beta/two"].intelligence_source == "heuristic"


def test_heuristic_never_overwrites_an_existing_score():
    evaluated = _model(
        "vendor/a",
        intelligence=70.0,
        intelligence_source="artificial_analysis",
        sources=["artificial_analysis"],
    )
    overridden = _model("vendor/b", intelligence=55.0, intelligence_source="override")
    result = apply_heuristic_intelligence([evaluated, overridden], lambda key: 100.0)
    assert [(model.intelligence, model.intelligence_source) for model in result] == [
        (70.0, "artificial_analysis"),
        (55.0, "override"),
    ]


def test_unknown_prices_are_never_treated_as_free():
    model = _model("vendor/a", input_per_mtok=None, output_per_mtok=None)
    assert blended_price(model) is None
    scored = apply_heuristic_intelligence([model], lambda key: 40.0)
    assert scored[0].intelligence is not None
    assert scored[0].intelligence_source == "heuristic"


def test_override_globs_apply_intelligence_and_prices():
    models = [
        _model("zai/glm-5-3", input_per_mtok=1.4, output_per_mtok=4.4),
        _model("zai/glm-5-3-flash", input_per_mtok=0.15, output_per_mtok=0.5),
        _model("other/model", input_per_mtok=2.0, output_per_mtok=8.0),
    ]
    overrides = {
        "zai/glm-5-3*": MarketOverrideConfig(intelligence=66.0),
        "other/model": MarketOverrideConfig(input_per_mtok=0.5, output_per_mtok=1.0),
    }
    result = {model.key: model for model in apply_overrides(models, overrides)}

    assert result["zai/glm-5-3"].intelligence == 66.0
    assert result["zai/glm-5-3"].intelligence_source == "override"
    assert result["zai/glm-5-3-flash"].intelligence == 66.0
    assert result["other/model"].intelligence is None
    assert result["other/model"].input_per_mtok == 0.5
    assert result["other/model"].output_per_mtok == 1.0


def test_discovery_score_model_delegates_to_the_shared_heuristic():
    engine_score = ModelDiscoveryEngine.score_model(None, "claude-opus-5-5")  # type: ignore[arg-type]
    assert engine_score == score_model_id("claude-opus-5-5")
    assert score_model_id("gemini-4-argon") == 28.4


def test_catalog_is_empty_without_any_market_snapshot(tmp_path):
    service = _service(tmp_path, scorer=lambda key: 42.0)
    catalog = service.catalog()
    assert catalog.models == []
    assert catalog.sources == []
    assert catalog.enabled is True
