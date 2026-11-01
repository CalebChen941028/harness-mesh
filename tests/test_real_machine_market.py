"""Opt-in live HTTP checks for the three market sources.

They never run under the default suite.  Set ``HARNESS_MESH_REAL_MACHINE=1``
to contact OpenRouter and models.dev; the Artificial Analysis check also needs
``ARTIFICIAL_ANALYSIS_API_KEY``.  These tests assert catalogue shape, not a
particular model name or price.
"""

from __future__ import annotations

import os

import pytest

from harness_mesh.config import MarketConfig
from harness_mesh.market import (
    ArtificialAnalysisProvider,
    MarketService,
    ModelsDevProvider,
    OpenRouterProvider,
    match_model,
)
from harness_mesh.storage import StateStore
from tests.real_machine import real_machine_enabled

pytestmark = pytest.mark.skipif(
    not real_machine_enabled(),
    reason="real network tests are opt-in; set HARNESS_MESH_REAL_MACHINE=1",
)

ZCODE_MODEL_IDS = [
    "GLM-5.3",
    "glm-5.3",
    "glm-5.1",
    "glm-5",
    "glm-4.7",
    "kimi-k2.6",
    "kimi-k2.7-code",
    "minimax-m2.7",
    "minimax-m2.5",
    "minimax-m3",
]
FIRST_PARTY_PROVIDERS = [
    "anthropic",
    "openai",
    "google",
    "deepseek",
    "zai",
    "zhipuai",
    "alibaba",
    "minimax",
    "minimax-cn",
    "moonshotai",
    "xai",
    "mistral",
]


@pytest.mark.asyncio
async def test_live_openrouter_catalogue_has_prices_and_flags():
    models = await OpenRouterProvider().fetch()
    assert models
    assert all("/" in model.key for model in models)
    assert any(model.input_per_mtok is not None for model in models)
    assert any(model.reasoning for model in models)
    assert any(model.tool_call for model in models)


@pytest.mark.asyncio
async def test_live_modelsdev_catalogue_has_per_million_prices():
    models = await ModelsDevProvider().fetch()
    assert models
    assert any(model.input_per_mtok is not None for model in models)
    assert any(model.context_window for model in models)


@pytest.mark.asyncio
async def test_live_artificial_analysis_when_a_key_is_configured():
    if not os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY"):
        pytest.skip("ARTIFICIAL_ANALYSIS_API_KEY is not configured")
    models = await ArtificialAnalysisProvider().fetch()
    assert models
    assert any(model.intelligence is not None for model in models)
    assert any(model.intelligence_source == "artificial_analysis" for model in models)


@pytest.mark.asyncio
async def test_live_modelsdev_folding_keeps_one_entry_per_model():
    from harness_mesh.market import normalize_model_identity

    provider = ModelsDevProvider(
        first_party_providers=[
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
    )
    models = await provider.fetch()
    by_key = {model.key: model for model in models}

    assert len(by_key) < 2_000

    for name in (
        "claude-opus-5-5",
        "gemini-4-argon",
        "glm-5.3",
        "glm-5.3-flash",
        "deepseek-v4.1-flash",
    ):
        identity = normalize_model_identity(name)[0]
        matches = [
            model
            for model in models
            if normalize_model_identity(model.key.split("/", 1)[-1])[0] == identity
        ]
        assert len(matches) == 1, (name, [model.key for model in matches])
        assert matches[0].input_per_mtok is not None
        assert matches[0].output_per_mtok is not None


@pytest.mark.asyncio
async def test_live_zcode_models_match_after_a_full_refresh(tmp_path):
    store = StateStore(tmp_path / "state.db")
    service = MarketService(
        store,
        MarketConfig(first_party_providers=FIRST_PARTY_PROVIDERS),
        env={},
    )
    try:
        result = await service.refresh(force=True)
        assert result.catalog.models
        for model_id in ZCODE_MODEL_IDS:
            match = match_model(model_id, result.catalog.models)
            assert match.key is not None, (model_id, match)
            assert match.confidence in {"exact", "normalized"}, (model_id, match)
    finally:
        store.close()

