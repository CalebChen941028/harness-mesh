"""Real market fixtures for ZCode: reseller folding and ambiguity resolution.

The three fixtures were cut from a live ``hmesh market refresh``.  They contain
the ten ZCode model ids that used to be unmatched, together with the thirty-odd
models.dev providers (original vendors and resale gateways) that publish them.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from harness_mesh.config import EpochConfig
from harness_mesh.market import (
    MarketModel,
    apply_epoch_intelligence,
    match_model,
    merge_market_models,
    parse_epoch_zip,
    parse_models_dev,
    parse_openrouter,
)

FIXTURES = Path(__file__).parent / "fixtures"
EPOCH_FIXTURES = FIXTURES / "epoch"
MODELSDEV_PAYLOAD = json.loads(
    (FIXTURES / "modelsdev_glm_kimi_minimax.json").read_text(encoding="utf-8")
)
OPENROUTER_PAYLOAD = json.loads(
    (FIXTURES / "openrouter_glm_kimi_minimax.json").read_text(encoding="utf-8")
)

# The original vendors in this fixture.  ``minimax-cn`` is the China endpoint
# of the same vendor, so both entries are first-party and fold into one model.
FIRST_PARTY = ["zai", "zhipuai", "moonshotai", "minimax", "minimax-cn"]

ZCODE_MODELS = [
    ("GLM-5.3", "zai/glm-5-3"),
    ("glm-5.3", "zai/glm-5-3"),
    ("glm-5.1", "zai/glm-5-1"),
    ("glm-5", "zai/glm-5"),
    ("glm-4.7", "zai/glm-4-7"),
    ("kimi-k2.6", "moonshotai/kimi-k2-6"),
    ("kimi-k2.7-code", "moonshotai/kimi-k2-7-code"),
    ("minimax-m2.7", "minimax/minimax-m2-7"),
    ("minimax-m2.5", "minimax/minimax-m2-5"),
    ("minimax-m3", "minimax/minimax-m3"),
]


def _epoch_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted(EPOCH_FIXTURES.glob("*.csv")):
            archive.writestr(path.name, path.read_text(encoding="utf-8"))
    return buffer.getvalue()


@pytest.fixture(scope="module")
def real_catalog() -> list[MarketModel]:
    return apply_epoch_intelligence(
        merge_market_models(
            [
                *parse_openrouter(OPENROUTER_PAYLOAD),
                *parse_models_dev(
                    MODELSDEV_PAYLOAD, first_party_providers=FIRST_PARTY
                ),
                *parse_epoch_zip(_epoch_zip(), EpochConfig()),
            ]
        )
    )


def test_modelsdev_folds_unscoped_resellers_into_the_canonical_model():
    models = parse_models_dev(MODELSDEV_PAYLOAD, first_party_providers=FIRST_PARTY)
    by_key = {model.key: model for model in models}

    # The resale-only rows (no canonical_model_id) used to become separate
    # ``alibabacodingplan/glm-5`` and ``minimax-coding-plan/...`` entries that
    # made every lookup ambiguous.
    assert not any("alibabacodingplan" in key for key in by_key)
    assert len(by_key) == len(models)

    glm = by_key["zai/glm-5-3"]
    assert glm.input_per_mtok == pytest.approx(1.4)
    assert glm.output_per_mtok == pytest.approx(4.4)
    assert glm.price_source == "first_party"

    assert by_key["zai/glm-5-1"].input_per_mtok == pytest.approx(1.4)
    assert by_key["zai/glm-5-1"].output_per_mtok == pytest.approx(4.4)
    assert by_key["zai/glm-5"].input_per_mtok == pytest.approx(1.0)
    assert by_key["zai/glm-5"].output_per_mtok == pytest.approx(3.2)
    assert by_key["zai/glm-4-7"].input_per_mtok == pytest.approx(0.6)
    assert by_key["zai/glm-4-7"].output_per_mtok == pytest.approx(2.2)

    # Two original providers (minimax and minimax-cn) and their resellers fold
    # into one entry, priced from the original vendor rather than a reseller.
    for name in ("minimax-m2-5", "minimax-m2-7", "minimax-m3"):
        model = by_key[f"minimax/{name}"]
        assert model.input_per_mtok == pytest.approx(0.3)
        assert model.output_per_mtok == pytest.approx(1.2)
        assert model.price_source == "first_party"


def test_reseller_only_rows_merge_under_one_unknown_key():
    payload = {
        "chutes": {
            "models": {
                "glm-9": {
                    "id": "glm-9",
                    "name": "GLM-9",
                    "cost": {"input": 1.0, "output": 2.0},
                }
            }
        },
        "other-gateway": {
            "models": {
                "zai/glm-9": {
                    "id": "zai/glm-9",
                    "name": "GLM-9",
                    "cost": {"input": 3.0, "output": 4.0},
                }
            }
        },
    }
    models = parse_models_dev(payload)
    assert [model.key for model in models] == ["unknown/glm-9"]
    assert models[0].input_per_mtok == pytest.approx(2.0)
    assert models[0].output_per_mtok == pytest.approx(3.0)
    assert models[0].price_source == "median"


def test_a_reseller_canonical_id_does_not_override_the_original_vendor():
    payload = {
        "reseller": {
            "models": {
                "kimi-k2.6": {
                    "id": "kimi-k2.6",
                    "name": "Kimi K2.6",
                    "cost": {"input": 9.0, "output": 9.0},
                    "canonical_model_id": "moonshot/kimi-k2.6",
                }
            }
        },
        "moonshotai": {
            "models": {
                "kimi-k2.6": {
                    "id": "kimi-k2.6",
                    "name": "Kimi K2.6",
                    "cost": {"input": 0.95, "output": 4.0},
                    "canonical_model_id": "moonshotai/kimi-k2.6",
                }
            }
        },
    }
    models = parse_models_dev(payload, first_party_providers=["moonshotai"])
    by_key = {model.key: model for model in models}
    assert "moonshotai/kimi-k2-6" in by_key
    assert "moonshot/kimi-k2-6" not in by_key
    assert by_key["moonshotai/kimi-k2-6"].input_per_mtok == pytest.approx(0.95)
    assert by_key["moonshotai/kimi-k2-6"].output_per_mtok == pytest.approx(4.0)
    assert by_key["moonshotai/kimi-k2-6"].price_source == "first_party"


@pytest.mark.parametrize(("model_id", "expected_key"), ZCODE_MODELS)
def test_zcode_models_match_the_canonical_catalog(real_catalog, model_id, expected_key):
    result = match_model(model_id, real_catalog)
    assert result.key == expected_key, (model_id, result)
    assert result.confidence in {"exact", "normalized"}
    assert result.note is None


def test_each_zcode_model_has_exactly_one_canonical_entry(real_catalog):
    keys = [model.key for model in real_catalog]
    assert len(keys) == len(set(keys))

    for _, expected_key in ZCODE_MODELS:
        model = next(item for item in real_catalog if item.key == expected_key)
        assert model.intelligence is not None, expected_key
        assert model.intelligence_source == "epoch_ai", expected_key
        assert model.benchmarks_used, expected_key


def test_ambiguous_candidates_prefer_benchmark_data():
    benchmark = MarketModel(
        key="alpha/shared",
        name="shared",
        aliases=["shared"],
        sources=["epoch"],
        intelligence_source="epoch_ai",
    )
    plain = MarketModel(
        key="beta/shared",
        name="shared",
        aliases=["shared"],
        sources=["models_dev"],
        input_per_mtok=2.0,
        output_per_mtok=4.0,
    )
    result = match_model("shared", [plain, benchmark])
    assert result.key == "alpha/shared"
    assert result.confidence == "normalized"
    assert result.note and "benchmark" in result.note


def test_ambiguous_candidates_prefer_openrouter_or_a_first_party_price():
    openrouter = MarketModel(
        key="alpha/shared",
        name="shared",
        aliases=["shared"],
        sources=["openrouter"],
        input_per_mtok=1.0,
        output_per_mtok=2.0,
    )
    plain = MarketModel(
        key="beta/shared",
        name="shared",
        aliases=["shared"],
        sources=["models_dev"],
        input_per_mtok=1.0,
        output_per_mtok=2.0,
    )
    result = match_model("shared", [plain, openrouter])
    assert result.key == "alpha/shared"
    assert result.confidence == "normalized"
    assert result.note and "openrouter" in result.note.lower()


def test_ambiguous_candidates_prefer_a_first_party_price_source():
    first_party = MarketModel(
        key="alpha/shared",
        name="shared",
        aliases=["shared"],
        sources=["models_dev"],
        price_source="first_party",
        input_per_mtok=1.0,
        output_per_mtok=2.0,
    )
    plain = MarketModel(
        key="beta/shared",
        name="shared",
        aliases=["shared"],
        sources=["models_dev"],
        input_per_mtok=1.0,
        output_per_mtok=2.0,
    )
    result = match_model("shared", [plain, first_party])
    assert result.key == "alpha/shared"
    assert result.confidence == "normalized"
    assert result.note and "first-party" in result.note.lower()


def test_ambiguous_candidates_prefer_the_priced_entry():
    priced = MarketModel(
        key="alpha/shared",
        name="shared",
        aliases=["shared"],
        sources=["models_dev"],
        input_per_mtok=1.0,
        output_per_mtok=2.0,
    )
    unpriced = MarketModel(
        key="beta/shared",
        name="shared",
        aliases=["shared"],
        sources=["models_dev"],
    )
    result = match_model("shared", [unpriced, priced])
    assert result.key == "alpha/shared"
    assert result.confidence == "normalized"
    assert result.note and "price" in result.note.lower()


def test_ambiguous_candidates_without_distinguishing_data_still_return_none():
    first = MarketModel(
        key="alpha/shared",
        name="shared",
        aliases=["shared"],
        sources=["models_dev"],
    )
    second = MarketModel(
        key="beta/shared",
        name="shared",
        aliases=["shared"],
        sources=["models_dev"],
    )
    result = match_model("shared", [first, second])
    assert result.key is None
    assert result.confidence == "none"
    assert result.note is None
