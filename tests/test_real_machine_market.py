"""Opt-in live HTTP checks for the three market sources.

They never run under the default suite.  Set ``HARNESS_MESH_REAL_MACHINE=1``
to contact OpenRouter and models.dev; the Artificial Analysis check also needs
``ARTIFICIAL_ANALYSIS_API_KEY``.  These tests assert catalogue shape, not a
particular model name or price.
"""

from __future__ import annotations

import os

import pytest

from harness_mesh.market import (
    ArtificialAnalysisProvider,
    ModelsDevProvider,
    OpenRouterProvider,
)
from tests.real_machine import real_machine_enabled

pytestmark = pytest.mark.skipif(
    not real_machine_enabled(),
    reason="real network tests are opt-in; set HARNESS_MESH_REAL_MACHINE=1",
)


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
