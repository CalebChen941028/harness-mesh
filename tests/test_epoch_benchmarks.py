"""Epoch AI benchmark provider: zip safety, parsing, percentiles and caching."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

import harness_mesh.market as market_module
from harness_mesh.config import EpochConfig, MarketConfig
from harness_mesh.market import (
    EpochProvider,
    MarketError,
    MarketModel,
    MarketService,
    apply_epoch_intelligence,
    fetch_bytes,
    merge_market_models,
    parse_epoch_zip,
)
from harness_mesh.storage import StateStore

FIXTURES = Path(__file__).parent / "fixtures"
EPOCH_FIXTURES = FIXTURES / "epoch"


def epoch_zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def fixture_zip() -> bytes:
    files = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(EPOCH_FIXTURES.glob("*.csv"))
    }
    files["README.md"] = (
        "Epoch AI, 'Capabilities & benchmarking', https://epoch.ai/benchmarks, CC BY 4.0\n"
    )
    return epoch_zip_bytes(files)


def _epoch_transport(payload: bytes, requests: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, content=payload)

    return httpx.MockTransport(handler)


def _store(tmp_path: Path) -> StateStore:
    directory = tmp_path / "state"
    directory.mkdir(parents=True, exist_ok=True)
    return StateStore(directory / "state.db")


# ---------------------------------------------------------------------------
# zip reading and parsing
# ---------------------------------------------------------------------------


def test_epoch_zip_parses_fixture_benchmarks_into_two_axes():
    models = parse_epoch_zip(fixture_zip(), EpochConfig())
    by_name = {model.name: model for model in models}

    opus = by_name["claude-opus-5-5"]
    assert opus.sources == ["epoch"]
    assert opus.intelligence_reasoning is not None
    assert opus.intelligence_coding is not None
    assert 0 <= opus.intelligence_reasoning <= 100
    assert "gpqa_diamond" in opus.benchmarks_used
    assert "cursorbench_external" in opus.benchmarks_used
    assert "README" not in opus.benchmarks_used
    assert opus.benchmark_data_date is not None

    flash = by_name["glm-5.3-flash"]
    assert opus.intelligence_coding > flash.intelligence_coding

    gemini = by_name["gemini-4-argon"]
    deepseek = by_name["deepseek-v4.1-flash"]
    assert gemini.intelligence_reasoning >= deepseek.intelligence_reasoning


def test_epoch_merges_effort_variants_and_strips_date_suffixes():
    rows = [
        "Model version,Score,Release date,Organization",
        "alpha_low,0.10,2026-01-01,Vendor",
        "alpha_max,0.90,2026-01-01,Vendor",
        "beta-2026-02-03_high,0.50,2026-02-03,Vendor",
        "gamma_thinking,0.30,2026-02-01,Vendor",
        "gamma_non-thinking,0.40,2026-02-01,Vendor",
        "delta,,2026-02-01,Vendor",
        "epsilon,not-a-number,2026-02-01,Vendor",
    ]
    payload = epoch_zip_bytes(
        {
            "bench_one.csv": "\n".join(rows) + "\n",
            "bench_two.csv": "\n".join(rows) + "\n",
            "bench_three.csv": "\n".join(rows) + "\n",
        }
    )
    config = EpochConfig(
        reasoning_benchmarks=["bench_one", "bench_two", "bench_three"],
        coding_benchmarks=[],
        min_benchmarks=3,
    )
    models = parse_epoch_zip(payload, config)
    by_name = {model.name: model for model in models}

    assert "alpha" in by_name
    assert "beta" in by_name
    assert "beta-2026-02-03" not in by_name
    assert "gamma" in by_name
    assert "delta" not in by_name
    assert "epsilon" not in by_name
    # alpha's max effort row (0.90) beats beta's 0.50 and gamma's 0.40.
    assert by_name["alpha"].intelligence_reasoning == pytest.approx(100.0)
    assert by_name["beta"].intelligence_reasoning == pytest.approx(50.0)
    assert by_name["gamma"].intelligence_reasoning == pytest.approx(0.0)


@pytest.mark.parametrize(
    "column",
    [
        "Best score (across scorers)",
        "mean_score",
        "Score",
        "Accuracy",
        "Performance",
        "Main score",
        "Arena Score",
        "Accuracy mean",
        "Pass@1",
    ],
)
def test_epoch_reads_every_documented_score_column(column):
    rows = [
        f"Model version,{column},Release date,Organization",
        "alpha,0.90,2026-01-01,Vendor",
        "beta,0.10,2026-01-02,Vendor",
    ]
    payload = epoch_zip_bytes({"bench_one.csv": "\n".join(rows) + "\n"})
    config = EpochConfig(
        reasoning_benchmarks=["bench_one"],
        coding_benchmarks=[],
        min_benchmarks=1,
    )
    models = parse_epoch_zip(payload, config)
    by_name = {model.name: model for model in models}
    assert by_name["alpha"].intelligence_reasoning == pytest.approx(100.0)
    assert by_name["beta"].intelligence_reasoning == pytest.approx(0.0)


def test_epoch_reference_window_excludes_old_models_from_the_percentile():
    rows = [
        "Model version,Score,Release date,Organization",
        "ancient,1.0,2024-01-01,Vendor",
        "recent-low,0.2,2026-01-01,Vendor",
        "recent-mid,0.4,2026-01-02,Vendor",
        "recent-high,0.6,2026-01-03,Vendor",
    ]
    payload = epoch_zip_bytes({"bench_one.csv": "\n".join(rows) + "\n"})
    config = EpochConfig(
        reasoning_benchmarks=["bench_one"],
        coding_benchmarks=[],
        min_benchmarks=1,
        reference_window_days=540,
    )
    models = parse_epoch_zip(payload, config)
    by_name = {model.name: model for model in models}

    assert "ancient" not in by_name
    assert by_name["recent-high"].intelligence_reasoning == pytest.approx(100.0)
    assert by_name["recent-mid"].intelligence_reasoning == pytest.approx(50.0)
    assert by_name["recent-low"].intelligence_reasoning == pytest.approx(0.0)


def test_epoch_min_benchmarks_keeps_a_thin_axis_empty():
    rows = "Model version,Score,Release date,Organization\nalpha,0.9,2026-01-01,Vendor\n"
    payload = epoch_zip_bytes({"bench_one.csv": rows})
    strict = EpochConfig(
        reasoning_benchmarks=["bench_one"],
        coding_benchmarks=[],
        min_benchmarks=3,
    )
    models = parse_epoch_zip(payload, strict)
    assert models == []

    lenient = EpochConfig(
        reasoning_benchmarks=["bench_one"],
        coding_benchmarks=[],
        min_benchmarks=1,
    )
    models = parse_epoch_zip(payload, lenient)
    assert models[0].intelligence_reasoning == pytest.approx(100.0)


def test_epoch_rejects_zip_slip_entries():
    payload = epoch_zip_bytes({"../evil.csv": "Model version,Score\nx,1\n"})
    with pytest.raises(MarketError):
        parse_epoch_zip(payload, EpochConfig())

    payload = epoch_zip_bytes({"/absolute/evil.csv": "Model version,Score\nx,1\n"})
    with pytest.raises(MarketError):
        parse_epoch_zip(payload, EpochConfig())


def test_epoch_rejects_an_oversized_payload():
    payload = fixture_zip()
    with pytest.raises(MarketError):
        parse_epoch_zip(payload, EpochConfig(), max_total_bytes=1024)


def test_epoch_ignores_unlisted_benchmark_files():
    rows = "Model version,Score,Release date,Organization\nalpha,0.9,2026-01-01,Vendor\n"
    payload = epoch_zip_bytes(
        {
            "bench_one.csv": rows,
            "not_configured.csv": rows,
            "notes.txt": "ignore me",
        }
    )
    config = EpochConfig(
        reasoning_benchmarks=["bench_one"],
        coding_benchmarks=[],
        min_benchmarks=1,
    )
    models = parse_epoch_zip(payload, config)
    assert models[0].benchmarks_used == ["bench_one"]


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_epoch_provider_downloads_the_zip_from_epoch_ai():
    requests: list[httpx.Request] = []
    provider = EpochProvider(transport=_epoch_transport(fixture_zip(), requests))
    models = await provider.fetch()

    assert models
    assert requests[0].url.host == "epoch.ai"
    assert str(requests[0].url).endswith("/data/benchmark_data.zip")


@pytest.mark.asyncio
async def test_epoch_provider_follows_a_same_host_redirect():
    payload = fixture_zip()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/data/benchmark_data.zip":
            return httpx.Response(
                302, headers={"location": "https://epoch.ai/data/v2/benchmark_data.zip"}
            )
        return httpx.Response(200, content=payload)

    provider = EpochProvider(transport=httpx.MockTransport(handler))
    models = await provider.fetch()
    assert models


@pytest.mark.asyncio
async def test_epoch_provider_rejects_a_redirect_off_the_allowlisted_host():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/data.zip"})

    provider = EpochProvider(transport=httpx.MockTransport(handler))
    with pytest.raises(MarketError):
        await provider.fetch()


@pytest.mark.asyncio
async def test_fetch_bytes_enforces_the_response_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 4096)

    with pytest.raises(MarketError):
        await fetch_bytes(
            "https://epoch.ai/data/benchmark_data.zip",
            transport=httpx.MockTransport(handler),
            max_bytes=1024,
        )


# ---------------------------------------------------------------------------
# merge, intelligence precedence and caching
# ---------------------------------------------------------------------------


def _price_model(key: str = "zai/glm-5-3", **overrides) -> MarketModel:
    payload = {
        "key": key,
        "vendor": key.split("/", 1)[0],
        "name": key.split("/", 1)[1],
        "input_per_mtok": 1.4,
        "output_per_mtok": 4.4,
        "sources": ["openrouter"],
        "fetched_at": 100.0,
    }
    payload.update(overrides)
    return MarketModel(**payload)


def test_epoch_scores_attach_to_the_matching_price_entry():
    epoch_model = MarketModel(
        key="zhipuai/glm-5-3",
        vendor="zhipuai",
        name="glm-5.3",
        intelligence_reasoning=80.0,
        intelligence_coding=60.0,
        benchmarks_used=["gpqa_diamond"],
        benchmark_data_date="2026-10-26",
        sources=["epoch"],
        fetched_at=100.0,
    )
    merged = merge_market_models([_price_model(), epoch_model])
    assert len(merged) == 1
    glm = merged[0]
    assert glm.sources == ["openrouter", "epoch"]
    assert glm.intelligence_reasoning == 80.0
    assert glm.intelligence_coding == 60.0
    assert glm.benchmarks_used == ["gpqa_diamond"]
    assert glm.benchmark_data_date == "2026-10-26"

    scored = apply_epoch_intelligence(merged)
    assert scored[0].intelligence == pytest.approx(70.0)
    assert scored[0].intelligence_source == "epoch_ai"


def test_artificial_analysis_wins_over_epoch_and_overrides_win_over_both():
    epoch_model = MarketModel(
        key="v/x",
        vendor="v",
        name="x",
        intelligence_reasoning=90.0,
        intelligence_coding=90.0,
        sources=["epoch"],
    )
    aa_model = MarketModel(
        key="v/x",
        vendor="v",
        name="x",
        intelligence=42.0,
        intelligence_source="artificial_analysis",
        sources=["artificial_analysis"],
    )
    merged = apply_epoch_intelligence(merge_market_models([epoch_model, aa_model]))
    assert merged[0].intelligence == 42.0
    assert merged[0].intelligence_source == "artificial_analysis"

    override = apply_epoch_intelligence(
        [merged[0].model_copy(update={"intelligence": 77.0, "intelligence_source": "override"})]
    )
    assert override[0].intelligence == 77.0


@pytest.mark.asyncio
async def test_epoch_refresh_caches_scores_and_falls_back_when_stale(tmp_path):
    clock = {"now": 10_000.0}
    transport = _epoch_transport(fixture_zip())
    config = MarketConfig(sources=["epoch"], epoch=EpochConfig())
    store = _store(tmp_path)
    try:
        service = MarketService(
            store,
            config=config,
            env={},
            providers={"epoch": EpochProvider(config=config.epoch, transport=transport)},
            now_fn=lambda: clock["now"],
        )
        first = await service.refresh()
        assert first.statuses[0].status == "fetched"
        assert first.catalog.models
        assert all(
            model.intelligence_source == "epoch_ai"
            for model in first.catalog.models
            if model.intelligence is not None
        )

        clock["now"] += config.epoch.ttl_seconds + 1
        service.providers["epoch"] = EpochProvider(
            config=config.epoch,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(503, content=b"down")
            ),
        )
        refreshed = await service.refresh()
        assert refreshed.statuses[0].status == "stale"
        assert refreshed.catalog.stale is True
        assert refreshed.catalog.models
    finally:
        store.close()


@pytest.mark.asyncio
async def test_epoch_refresh_respects_the_offline_switch(tmp_path):
    store = _store(tmp_path)
    try:
        service = MarketService(
            store,
            config=MarketConfig(sources=["epoch"]),
            env={"HARNESS_MESH_OFFLINE": "1"},
            providers={"epoch": EpochProvider(transport=_epoch_transport(fixture_zip()))},
        )
        result = await service.refresh()
        assert result.statuses[0].status == "offline"
        assert result.catalog.models == []
    finally:
        store.close()


def test_epoch_ttl_defaults_to_twelve_hours():
    config = MarketConfig()
    assert config.epoch.ttl_seconds == 43_200
    assert "epoch" in market_module.SOURCE_ORDER
    assert market_module.EPOCH_ATTRIBUTION.endswith("(CC BY 4.0)")


def test_epoch_axis_scores_are_stored_on_the_catalog_model(tmp_path):
    models = parse_epoch_zip(fixture_zip(), EpochConfig())
    payload = {
        "models": [model.model_dump(mode="json") for model in models],
        "fetched_at": 1.0,
    }
    store = _store(tmp_path)
    try:
        store.save_market_snapshot("epoch", 1.0, payload)
        service = MarketService(
            store,
            config=MarketConfig(sources=["epoch"]),
            providers={},
            now_fn=lambda: 2.0,
        )
        catalog = service.catalog()
        by_name = {model.key.split("/", 1)[-1]: model for model in catalog.models}
        assert by_name["claude-opus-5-5"].intelligence_coding is not None
        assert by_name["claude-opus-5-5"].intelligence == pytest.approx(
            round(
                (
                    by_name["claude-opus-5-5"].intelligence_reasoning
                    + by_name["claude-opus-5-5"].intelligence_coding
                )
                / 2,
                2,
            )
        )
    finally:
        store.close()
