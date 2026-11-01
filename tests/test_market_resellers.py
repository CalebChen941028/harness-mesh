"""models.dev reseller folding, canonical keys and unmatched ledger queries."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import harness_mesh.cli as cli_module
from harness_mesh.market import (
    MarketModel,
    merge_market_models,
    parse_models_dev,
    parse_openrouter,
)
from harness_mesh.models import DiscoveredModel
from tests.helpers import make_mesh

FIXTURES = Path(__file__).parent / "fixtures"
MODELSDEV_RESELLERS = json.loads(
    (FIXTURES / "modelsdev_resellers.json").read_text(encoding="utf-8")
)
OPENROUTER_PAYLOAD = json.loads((FIXTURES / "openrouter_models.json").read_text(encoding="utf-8"))

FIRST_PARTY = [
    "anthropic",
    "openai",
    "google",
    "deepseek",
    "zai",
    "zhipuai",
    "alibaba",
    "moonshotai",
    "xai",
    "mistral",
]


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _invoke(monkeypatch, factory, *args: str):
    monkeypatch.setattr(cli_module, "_runtime", lambda ctx: factory())
    return CliRunner().invoke(cli_module.app, list(args), env={"FORCE_COLOR": "1"})


def _reseller_rows() -> int:
    return sum(len(provider["models"]) for provider in MODELSDEV_RESELLERS.values())


def _discovered(harness: str, model_id: str, score: float = 40.0) -> DiscoveredModel:
    return DiscoveredModel(
        harness=harness,
        model_id=model_id,
        source="local_config",
        capability_score=score,
        discovered_at=time.time(),
    )


def _seed_market(mesh, models: list[MarketModel]) -> None:
    now = time.time()
    mesh.storage.save_market_snapshot(
        "artificial_analysis",
        now,
        {"models": [model.model_dump(mode="json") for model in models], "fetched_at": now},
    )


def _market_model(key: str, input_price: float, output_price: float) -> MarketModel:
    return MarketModel(
        key=key,
        vendor=key.split("/", 1)[0],
        name=key.split("/", 1)[1],
        input_per_mtok=input_price,
        output_per_mtok=output_price,
        intelligence=50.0,
        intelligence_source="artificial_analysis",
        sources=["artificial_analysis"],
    )


# ---------------------------------------------------------------------------
# models.dev folding
# ---------------------------------------------------------------------------


def test_modelsdev_folds_resellers_into_one_canonical_model():
    models = parse_models_dev(MODELSDEV_RESELLERS, first_party_providers=FIRST_PARTY)
    by_key = {model.key: model for model in models}

    assert "zai/glm-5-3" in by_key
    glm = by_key["zai/glm-5-3"]
    # The original vendor (zai) wins; no reseller price leaks through.
    assert glm.input_per_mtok == pytest.approx(1.4)
    assert glm.output_per_mtok == pytest.approx(4.4)
    assert glm.price_source == "first_party"

    # The same model appears exactly once, under no reseller vendor.
    assert len(
        [model for model in models if model.key.split("/", 1)[-1] == "glm-5-3"]
    ) == 1

    # Zero-priced plan entries (zai-coding-plan, volcengine, amd) are ignored.
    assert all(
        not (model.input_per_mtok == 0 and model.output_per_mtok == 0) for model in models
    )

    # Folding collapses dozens of rows into a handful of canonical models.
    assert len(models) < _reseller_rows() / 5
    assert len(models) == 6


def test_modelsdev_uses_the_anthropic_price_for_claude_opus():
    models = parse_models_dev(MODELSDEV_RESELLERS, first_party_providers=FIRST_PARTY)
    by_key = {model.key: model for model in models}

    opus = by_key["anthropic/claude-opus-5-5"]
    assert opus.input_per_mtok == pytest.approx(4.0)
    assert opus.output_per_mtok == pytest.approx(20.0)
    assert opus.release_date == "2026-09-22"
    assert opus.context_window == 1_000_000

    gemini = by_key["google/gemini-4-argon"]
    assert gemini.input_per_mtok == pytest.approx(2.0)
    assert gemini.output_per_mtok == pytest.approx(10.0)


def test_modelsdev_without_a_first_party_list_still_folds_by_canonical_name():
    models = parse_models_dev(MODELSDEV_RESELLERS)
    by_key = {model.key: model for model in models}

    assert len(by_key) == 6
    glm = by_key["zai/glm-5-3"]
    # No first-party provider configured: the median of the known offers is
    # used, never a reseller-specific or zero price.
    assert glm.price_source == "median"
    assert glm.input_per_mtok == pytest.approx(1.4)
    assert glm.output_per_mtok == pytest.approx(4.4)


def test_modelsdev_folds_into_openrouter_keys():
    modelsdev = parse_models_dev(MODELSDEV_RESELLERS, first_party_providers=FIRST_PARTY)
    openrouter = parse_openrouter(OPENROUTER_PAYLOAD)
    merged = merge_market_models([*openrouter, *modelsdev])

    matches = [model for model in merged if model.key == "zai/glm-5-3"]
    assert len(matches) == 1
    glm = matches[0]
    assert set(glm.sources) == {"openrouter", "models_dev"}
    assert glm.input_per_mtok == pytest.approx(1.4)
    assert glm.output_per_mtok == pytest.approx(4.4)

    opus = [model for model in merged if model.key == "anthropic/claude-opus-5-5"]
    assert len(opus) == 1
    assert set(opus[0].sources) == {"openrouter", "models_dev"}
    assert opus[0].input_per_mtok == pytest.approx(4.0)
    assert opus[0].output_per_mtok == pytest.approx(20.0)


def test_openrouter_fills_prices_when_modelsdev_has_no_first_party_offer():
    modelsdev = parse_models_dev(MODELSDEV_RESELLERS)
    openrouter = parse_openrouter(OPENROUTER_PAYLOAD)
    merged = {model.key: model for model in merge_market_models([*openrouter, *modelsdev])}

    glm = merged["zai/glm-5-3"]
    assert set(glm.sources) == {"openrouter", "models_dev"}
    assert glm.input_per_mtok == pytest.approx(0.0699)
    assert glm.output_per_mtok == pytest.approx(1.4)


# ---------------------------------------------------------------------------
# CLI queries
# ---------------------------------------------------------------------------


def test_market_show_model_treats_dots_and_hyphens_as_equivalent(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    _seed_market(
        mesh,
        [
            _market_model("anthropic/claude-opus-5-5", 4, 20),
            _market_model("zai/glm-5-3", 1.4, 4.4),
        ],
    )
    try:
        result = _invoke(
            monkeypatch,
            lambda: make_mesh(tmp_path),
            "market",
            "show",
            "--model",
            "claude-opus-5.5",
            "--json",
        )
        output = _plain(result.output)
        assert result.exit_code == 0, output
        payload = json.loads(output)
        assert [item["key"] for item in payload["models"]] == ["anthropic/claude-opus-5-5"]
    finally:
        mesh.storage.close()


def test_market_show_reports_a_glob_miss_separately_from_an_empty_cache(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    _seed_market(mesh, [_market_model("anthropic/claude-opus-5-5", 4, 20)])
    try:
        text = _plain(
            _invoke(
                monkeypatch,
                lambda: make_mesh(tmp_path),
                "market",
                "show",
                "--model",
                "no-such-model*",
            ).output
        )
        assert "no model matches no-such-model*" in text
        assert "No market data is cached" not in text
    finally:
        mesh.storage.close()

    empty = make_mesh(tmp_path / "empty")
    try:
        text = _plain(
            _invoke(
                monkeypatch,
                lambda: make_mesh(tmp_path / "empty"),
                "market",
                "show",
                "--model",
                "anything",
            ).output
        )
        assert "No market data is cached" in text
    finally:
        empty.storage.close()


def test_market_show_unmatched_uses_the_discovery_ledger(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    _seed_market(mesh, [_market_model("anthropic/claude-opus-5-5", 4, 20)])
    mesh.storage.save_discovered_models(
        [
            _discovered("claude_code", "claude-opus-5-5"),
            _discovered("claude_code", "claude-fable-5-1"),
            _discovered("codex", "codex-auto-review"),
        ]
    )
    try:
        result = _invoke(
            monkeypatch,
            lambda: make_mesh(tmp_path),
            "market",
            "show",
            "--unmatched",
            "--json",
        )
        payload = json.loads(_plain(result.output))
        assert payload["unmatched"] == [
            {
                "harness": "claude_code",
                "model_id": "claude-fable-5-1",
                "effort_variant": "",
                "confidence": "none",
            }
        ]
    finally:
        mesh.storage.close()


def test_market_show_unmatched_without_a_ledger_asks_for_discovery(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    _seed_market(mesh, [_market_model("anthropic/claude-opus-5-5", 4, 20)])
    try:
        text = _plain(
            _invoke(
                monkeypatch,
                lambda: make_mesh(tmp_path),
                "market",
                "show",
                "--unmatched",
            ).output
        )
        assert "hmesh models --refresh" in text
    finally:
        mesh.storage.close()
