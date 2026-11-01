"""Market data sources: parsing, transport safety, caching and merging."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

import harness_mesh.market as market_module
from harness_mesh.config import MarketConfig
from harness_mesh.market import (
    ArtificialAnalysisProvider,
    MarketError,
    MarketModel,
    MarketProvider,
    MarketService,
    OpenRouterProvider,
    canonical_model_key,
    fetch_json,
    merge_market_models,
    parse_models_dev,
    parse_openrouter,
)
from harness_mesh.storage import StateStore

FIXTURES = Path(__file__).parent / "fixtures"
OPENROUTER_PAYLOAD = json.loads((FIXTURES / "openrouter_models.json").read_text(encoding="utf-8"))
MODELSDEV_PAYLOAD = json.loads((FIXTURES / "modelsdev_api.json").read_text(encoding="utf-8"))
ARTIFICIAL_ANALYSIS_PAYLOAD = json.loads(
    (FIXTURES / "artificial_analysis_models.json").read_text(encoding="utf-8")
)


def _fixture_transport(requests: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        host = request.url.host
        if host == "openrouter.ai":
            return httpx.Response(200, json=OPENROUTER_PAYLOAD)
        if host == "models.dev":
            return httpx.Response(200, json=MODELSDEV_PAYLOAD)
        if host == "artificialanalysis.ai":
            assert request.headers.get("x-api-key") == "aa-secret"
            return httpx.Response(200, json=ARTIFICIAL_ANALYSIS_PAYLOAD)
        raise AssertionError(f"unexpected host {host}")

    return httpx.MockTransport(handler)


class FakeProvider(MarketProvider):
    def __init__(self, name: str, models: list[MarketModel], *, error: MarketError | None = None):
        self.name = name
        self.models = models
        self.error = error
        self.calls = 0

    async def fetch(self) -> list[MarketModel]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [model.model_copy(deep=True) for model in self.models]


def _model(key: str, **overrides) -> MarketModel:
    payload = {
        "key": key,
        "vendor": key.split("/", 1)[0],
        "name": key.split("/", 1)[1],
        "input_per_mtok": 1.0,
        "output_per_mtok": 2.0,
        "sources": ["openrouter"],
        "fetched_at": 100.0,
    }
    payload.update(overrides)
    return MarketModel(**payload)


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_parsing_converts_prices_filters_batch_and_folds_latest():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=OPENROUTER_PAYLOAD)
    )
    provider = OpenRouterProvider(transport=transport)
    models = await provider.fetch()
    by_key = {model.key: model for model in models}

    assert by_key, "fixture must produce models"
    assert not [model for model in models if ":batch" in model.key]

    flash = by_key["deepseek/deepseek-v4-1-flash"]
    assert flash.input_per_mtok == pytest.approx(0.045)
    assert flash.output_per_mtok == pytest.approx(0.5)
    assert flash.cache_read_per_mtok == pytest.approx(0.006)
    assert flash.context_window == 1_048_576
    assert flash.reasoning is True
    assert flash.tool_call is True
    assert flash.release_date == "2026-09-10"
    assert flash.sources == ["openrouter"]
    assert "deepseek/deepseek-v4.1-flash" in flash.aliases

    # The ``~vendor/model-latest`` alias folds onto the base model instead of
    # appearing as a second market entry.
    latest = by_key["deepseek/deepseek-v4-flash"]
    assert latest.input_per_mtok == pytest.approx(0.0434)
    assert not [model for model in models if model.key.startswith("~")]
    assert not [model for model in models if model.key.endswith("-latest")]

    gemini = by_key["google/gemini-4-argon"]
    assert gemini.vendor == "google"
    assert gemini.context_window == 1_048_576
    assert gemini.reasoning is True


@pytest.mark.asyncio
async def test_missing_or_invalid_prices_are_unknown_not_zero():
    payload = {
        "data": [
            {"id": "vendor/missing", "pricing": {}},
            {"id": "vendor/invalid", "pricing": {"prompt": "not-a-number", "completion": None}},
            {"id": "vendor/negative", "pricing": {"prompt": "-1", "completion": "0"}},
            {"id": "vendor/bool", "pricing": {"prompt": True, "completion": "1"}},
        ]
    }
    models = {model.key: model for model in parse_openrouter(payload)}
    assert models["vendor/missing"].input_per_mtok is None
    assert models["vendor/missing"].output_per_mtok is None
    assert models["vendor/invalid"].input_per_mtok is None
    assert models["vendor/invalid"].output_per_mtok is None
    assert models["vendor/negative"].input_per_mtok is None
    assert models["vendor/negative"].output_per_mtok == 0.0
    assert models["vendor/bool"].input_per_mtok is None


# ---------------------------------------------------------------------------
# models.dev
# ---------------------------------------------------------------------------


def test_modelsdev_parsing_uses_per_million_costs_and_keeps_vendors_distinct():
    models = parse_models_dev(MODELSDEV_PAYLOAD)
    by_key = {model.key: model for model in models}

    opus = by_key["anthropic/claude-opus-5-5"]
    assert opus.input_per_mtok == 4.0
    assert opus.output_per_mtok == 20.0
    assert opus.cache_read_per_mtok == 0.2
    assert opus.context_window == 1_000_000
    assert opus.reasoning is True
    assert opus.tool_call is True
    assert opus.release_date == "2026-09-22"
    assert opus.sources == ["models.dev"]

    flash = by_key["zai/glm-5-3-flash"]
    assert flash.input_per_mtok == 0.15
    assert flash.output_per_mtok == 0.5
    assert flash.reasoning is True

    # The same model name under two providers stays two market entries.
    assert "alibaba/kimi-k3" in by_key
    assert "moonshotai/kimi-k3" in by_key


def test_canonical_keys_treat_dots_hyphens_and_vendor_punctuation_equally():
    assert canonical_model_key("z-ai", "glm-5.3") == canonical_model_key("zai", "GLM-5.3")
    assert canonical_model_key("anthropic", "claude-opus-5.5") == canonical_model_key(
        "anthropic", "claude-opus-5-5"
    )
    assert canonical_model_key("anthropic", "claude-haiku-4-5-20251001") == canonical_model_key(
        "anthropic", "claude-haiku-4-5"
    )


# ---------------------------------------------------------------------------
# Artificial Analysis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artificial_analysis_parses_official_free_shape():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=ARTIFICIAL_ANALYSIS_PAYLOAD)

    provider = ArtificialAnalysisProvider(api_key="aa-secret", transport=httpx.MockTransport(handler))
    models = await provider.fetch()

    assert len(models) == 1
    model = models[0]
    assert model.key == "zai/glm-5-3"
    assert model.intelligence == pytest.approx(31.7)
    assert model.intelligence_source == "artificial_analysis"
    assert model.input_per_mtok == pytest.approx(1.4)
    assert model.output_per_mtok == pytest.approx(4.4)
    assert model.cache_read_per_mtok == pytest.approx(0.26)
    assert model.speed_tps == pytest.approx(80.07)
    assert model.reasoning is True
    assert model.release_date == "2026-08-18"
    assert model.sources == ["artificial_analysis"]

    assert len(requests) == 1
    assert str(requests[0].url).startswith("https://artificialanalysis.ai/api/v2/language/models/free")
    assert requests[0].headers["x-api-key"] == "aa-secret"
    assert requests[0].headers["user-agent"].startswith("harness-mesh/")


@pytest.mark.asyncio
async def test_artificial_analysis_without_key_makes_no_request():
    called = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        called.append(request)
        return httpx.Response(200, json=ARTIFICIAL_ANALYSIS_PAYLOAD)

    provider = ArtificialAnalysisProvider(
        env={}, transport=httpx.MockTransport(handler), key_env="ARTIFICIAL_ANALYSIS_API_KEY"
    )
    assert await provider.fetch() == []
    assert called == []


# ---------------------------------------------------------------------------
# transport safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_https_and_unknown_hosts_are_rejected_before_any_request():
    called = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        called.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(MarketError):
        await fetch_json("http://openrouter.ai/api/v1/models", transport=httpx.MockTransport(handler))
    with pytest.raises(MarketError):
        await fetch_json("https://example.com/models", transport=httpx.MockTransport(handler))
    assert called == []


@pytest.mark.asyncio
async def test_redirects_may_not_leave_the_allowlist():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    with pytest.raises(MarketError, match="allowlist"):
        await fetch_json(
            "https://openrouter.ai/api/v1/models", transport=httpx.MockTransport(handler)
        )


@pytest.mark.asyncio
async def test_a_market_redirect_may_not_switch_to_another_allowlisted_host():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://models.dev/api.json"})

    with pytest.raises(MarketError, match="same allowlisted host"):
        await fetch_json(
            "https://artificialanalysis.ai/api/v2/language/models/free",
            headers={"x-api-key": "aa-secret"},
            transport=httpx.MockTransport(handler),
        )


@pytest.mark.asyncio
async def test_redirects_inside_the_allowlist_are_followed():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v1/models":
            return httpx.Response(302, headers={"location": "/api/v1/models/v2"})
        return httpx.Response(200, json={"ok": True})

    payload = await fetch_json(
        "https://openrouter.ai/api/v1/models", transport=httpx.MockTransport(handler)
    )
    assert payload == {"ok": True}
    assert seen == [
        "https://openrouter.ai/api/v1/models",
        "https://openrouter.ai/api/v1/models/v2",
    ]


@pytest.mark.asyncio
async def test_invalid_json_and_oversized_bodies_are_rejected(monkeypatch):
    with pytest.raises(MarketError, match="JSON"):
        await fetch_json(
            "https://models.dev/api.json",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>")),
        )

    monkeypatch.setattr(market_module, "MAX_RESPONSE_BYTES", 64)
    with pytest.raises(MarketError, match="size|large|limit"):
        await fetch_json(
            "https://models.dev/api.json",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"{" + b" " * 128 + b"}")
            ),
        )
    with pytest.raises(MarketError, match="size|large|limit"):
        await fetch_json(
            "https://models.dev/api.json",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=b"{}", headers={"content-length": "999999"}
                )
            ),
        )


@pytest.mark.asyncio
async def test_provider_requests_carry_only_the_user_agent_and_the_aa_key():
    requests: list[httpx.Request] = []
    service = MarketService(
        _store(),
        config=MarketConfig(sources=["openrouter", "models_dev", "artificial_analysis"]),
        env={"ARTIFICIAL_ANALYSIS_API_KEY": "aa-secret"},
        transport=_fixture_transport(requests),
        now_fn=lambda: 1_000.0,
    )
    result = await service.refresh(force=True)
    assert {status.source for status in result.statuses} == {
        "openrouter",
        "models_dev",
        "artificial_analysis",
    }
    assert all(status.status == "fetched" for status in result.statuses)

    for request in requests:
        assert request.url.scheme == "https"
        assert request.url.query == b""
        assert request.headers.get("user-agent", "").startswith("harness-mesh/")
        if request.url.host == "artificialanalysis.ai":
            assert request.headers.get("x-api-key") == "aa-secret"
        else:
            assert "x-api-key" not in request.headers

    # The key must never reach the snapshot payload.
    snapshots = service.storage.list_market_snapshots()
    for snapshot in snapshots:
        assert "aa-secret" not in json.dumps(snapshot["payload"])


def _store() -> StateStore:
    # A unique temporary store per test keeps cache assertions independent.
    import tempfile

    directory = Path(tempfile.mkdtemp(prefix="hmesh-market-"))
    return StateStore(directory / "state.db")


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------


def test_merge_prefers_openrouter_prices_and_artificial_analysis_intelligence():
    openrouter = _model(
        "zai/glm-5-3",
        name="GLM 5.3",
        input_per_mtok=1.4,
        output_per_mtok=4.4,
        sources=["openrouter"],
        aliases=["z-ai/glm-5.3"],
    )
    modelsdev = _model(
        "zai/glm-5-3",
        name="GLM-5.3",
        input_per_mtok=9.9,
        output_per_mtok=9.9,
        context_window=1_000_000,
        reasoning=True,
        sources=["models_dev"],
    )
    artificial = _model(
        "zai/glm-5-3",
        name="GLM-5.3 (Max)",
        input_per_mtok=0.1,
        output_per_mtok=0.1,
        intelligence=31.7,
        intelligence_source="artificial_analysis",
        speed_tps=80.07,
        sources=["artificial_analysis"],
    )

    merged = merge_market_models([artificial, modelsdev, openrouter])
    assert len(merged) == 1
    model = merged[0]
    assert model.input_per_mtok == 1.4
    assert model.output_per_mtok == 4.4
    assert model.context_window == 1_000_000
    assert model.reasoning is True
    assert model.intelligence == 31.7
    assert model.intelligence_source == "artificial_analysis"
    assert model.speed_tps == 80.07
    assert model.sources == ["openrouter", "models_dev", "artificial_analysis"]
    assert "z-ai/glm-5.3" in model.aliases


def test_merge_fills_each_missing_price_from_the_next_source():
    openrouter = _model("v/x", input_per_mtok=None, output_per_mtok=2.0, sources=["openrouter"])
    modelsdev = _model(
        "v/x",
        input_per_mtok=1.0,
        output_per_mtok=9.0,
        cache_read_per_mtok=0.5,
        sources=["models_dev"],
    )
    merged = merge_market_models([openrouter, modelsdev])[0]
    assert merged.input_per_mtok == 1.0
    assert merged.output_per_mtok == 2.0
    assert merged.cache_read_per_mtok == 0.5
    assert merged.sources == ["openrouter", "models_dev"]


# ---------------------------------------------------------------------------
# cache, TTL and offline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hits_ttl_and_force_refresh():
    clock = {"now": 10_000.0}
    provider = FakeProvider("openrouter", [_model("v/a")])
    service = MarketService(
        _store(),
        config=MarketConfig(sources=["openrouter"], ttl_seconds=3600),
        providers={"openrouter": provider},
        now_fn=lambda: clock["now"],
    )
    first = await service.refresh()
    assert first.statuses[0].status == "fetched"
    assert provider.calls == 1

    clock["now"] += 100
    second = await service.refresh()
    assert second.statuses[0].status == "cached"
    assert provider.calls == 1

    await service.refresh(force=True)
    assert provider.calls == 2
    assert service.storage.get_market_snapshot("openrouter")["fetched_at"] == clock["now"]


@pytest.mark.asyncio
async def test_failed_fetch_falls_back_to_stale_cache():
    clock = {"now": 10_000.0}
    provider = FakeProvider("openrouter", [_model("v/a")])
    service = MarketService(
        _store(),
        config=MarketConfig(sources=["openrouter"], ttl_seconds=60),
        providers={"openrouter": provider},
        now_fn=lambda: clock["now"],
    )
    await service.refresh()
    clock["now"] += 120
    provider.error = MarketError("network down")

    result = await service.refresh()
    assert provider.calls == 2
    assert result.statuses[0].status == "stale"
    catalog = service.catalog()
    assert [model.key for model in catalog.models] == ["v/a"]
    assert catalog.stale is True
    assert catalog.expired is False
    assert catalog.warnings


@pytest.mark.asyncio
async def test_cache_older_than_seven_days_is_expired_but_still_usable():
    clock = {"now": 10_000.0}
    provider = FakeProvider("openrouter", [_model("v/a")])
    service = MarketService(
        _store(),
        config=MarketConfig(sources=["openrouter"], ttl_seconds=60),
        providers={"openrouter": provider},
        now_fn=lambda: clock["now"],
    )
    await service.refresh()
    clock["now"] += 8 * 86_400
    provider.error = MarketError("network down")
    await service.refresh()

    catalog = service.catalog()
    assert [model.key for model in catalog.models] == ["v/a"]
    assert catalog.stale is True
    assert catalog.expired is True


@pytest.mark.asyncio
async def test_offline_switch_makes_no_network_calls():
    provider = FakeProvider("openrouter", [_model("v/a")])
    disabled = MarketService(
        _store(),
        config=MarketConfig(enabled=False, sources=["openrouter"]),
        providers={"openrouter": provider},
        now_fn=lambda: 50.0,
    )
    result = await disabled.refresh(force=True)
    assert provider.calls == 0
    assert result.statuses[0].status == "disabled"
    assert disabled.catalog().models == []

    env_provider = FakeProvider("openrouter", [_model("v/a")])
    offline = MarketService(
        _store(),
        config=MarketConfig(sources=["openrouter"]),
        env={"HARNESS_MESH_OFFLINE": "1"},
        providers={"openrouter": env_provider},
        now_fn=lambda: 50.0,
    )
    await offline.refresh(force=True)
    assert env_provider.calls == 0


@pytest.mark.asyncio
async def test_artificial_analysis_without_key_keeps_the_previous_snapshot():
    clock = {"now": 10_000.0}
    service = MarketService(
        _store(),
        config=MarketConfig(sources=["artificial_analysis"]),
        env={},
        now_fn=lambda: clock["now"],
    )
    service.storage.save_market_snapshot(
        "artificial_analysis",
        clock["now"],
        {
            "models": [
                _model(
                    "v/a", intelligence=42.0, intelligence_source="artificial_analysis"
                ).model_dump(mode="json")
            ]
        },
    )
    clock["now"] += 100_000
    result = await service.refresh(force=True)
    assert result.statuses[0].status == "skipped"
    catalog = service.catalog()
    assert [model.key for model in catalog.models] == ["v/a"]
    assert catalog.models[0].intelligence == 42.0


@pytest.mark.asyncio
async def test_artificial_analysis_honours_minimum_twelve_hour_ttl():
    clock = {"now": 10_000.0}
    provider = FakeProvider(
        "artificial_analysis",
        [_model("v/a", intelligence=42.0, intelligence_source="artificial_analysis")],
    )
    service = MarketService(
        _store(),
        config=MarketConfig(sources=["artificial_analysis"], ttl_seconds=60),
        env={"ARTIFICIAL_ANALYSIS_API_KEY": "aa-secret"},
        providers={"artificial_analysis": provider},
        now_fn=lambda: clock["now"],
    )
    await service.refresh()
    assert provider.calls == 1
    clock["now"] += 120
    second = await service.refresh()
    assert second.statuses[0].status == "cached"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_artificial_analysis_daily_request_cap():
    clock = {"now": 10_000.0}
    provider = FakeProvider(
        "artificial_analysis",
        [_model("v/a", intelligence=42.0, intelligence_source="artificial_analysis")],
    )
    service = MarketService(
        _store(),
        config=MarketConfig(sources=["artificial_analysis"], artificial_analysis_daily_limit=2),
        env={"ARTIFICIAL_ANALYSIS_API_KEY": "aa-secret"},
        providers={"artificial_analysis": provider},
        now_fn=lambda: clock["now"],
    )
    await service.refresh(force=True)
    await service.refresh(force=True)
    third = await service.refresh(force=True)
    assert provider.calls == 2
    assert third.statuses[0].status == "rate_limited"
    assert service.catalog().models


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def test_market_snapshot_table_is_added_to_an_existing_database(tmp_path):
    db = tmp_path / "state.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    store = StateStore(db)
    try:
        store.save_market_snapshot("openrouter", 12.5, {"models": [{"key": "v/a"}]})
        snapshot = store.get_market_snapshot("openrouter")
        assert snapshot is not None
        assert snapshot["fetched_at"] == 12.5
        assert snapshot["payload"]["models"] == [{"key": "v/a"}]
        assert "openrouter" in {item["source"] for item in store.list_market_snapshots()}
    finally:
        store.close()
