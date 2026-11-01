"""Harness model-id to market-key matching."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_mesh.market import (
    MarketModel,
    best_match,
    list_unmatched,
    match_model,
    merge_market_models,
    normalize_model_identity,
    parse_models_dev,
    parse_openrouter,
)

FIXTURES = Path(__file__).parent / "fixtures"
OPENROUTER_PAYLOAD = json.loads((FIXTURES / "openrouter_models.json").read_text(encoding="utf-8"))
MODELSDEV_PAYLOAD = json.loads((FIXTURES / "modelsdev_api.json").read_text(encoding="utf-8"))
FIRST_PARTY_PROVIDERS = [
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


def catalog() -> list[MarketModel]:
    return merge_market_models(
        [
            *parse_openrouter(OPENROUTER_PAYLOAD),
            *parse_models_dev(
                MODELSDEV_PAYLOAD, first_party_providers=FIRST_PARTY_PROVIDERS
            ),
        ]
    )


@pytest.fixture(scope="module")
def models() -> list[MarketModel]:
    return catalog()


@pytest.mark.parametrize(
    ("model_id", "expected_key", "effort"),
    [
        ("claude-opus-5-5", "anthropic/claude-opus-5-5", None),
        ("claude-opus-5", "anthropic/claude-opus-5", None),
        ("claude-opus-4-8", "anthropic/claude-opus-4-8", None),
        ("claude-sonnet-5-5", "anthropic/claude-sonnet-5-5", None),
        ("claude-haiku-5-5", "anthropic/claude-haiku-5-5", None),
        ("claude-haiku-4-5-20251001", "anthropic/claude-haiku-4-5", None),
        ("claude-opus-5-5[1m]", "anthropic/claude-opus-5-5", None),
        ("gpt-6-astra", "openai/gpt-6-astra", None),
        ("gpt-6-sol", "openai/gpt-6-sol", None),
        ("gpt-6.1-sol", "openai/gpt-6-1-sol", None),
        ("gpt-6.1-terra", "openai/gpt-6-1-terra", None),
        ("gpt-5.6-sol", "openai/gpt-5-6-sol", None),
        ("gpt-5.6-luna", "openai/gpt-5-6-luna", None),
        ("deepseek-v4.1-flash", "deepseek/deepseek-v4-1-flash", None),
        ("deepseek-v4-pro", "deepseek/deepseek-v4-pro", None),
        ("GLM-5.3", "zai/glm-5-3", None),
        ("glm-5.3", "zai/glm-5-3", None),
        ("GLM-5.3-Flash", "zai/glm-5-3-flash", None),
        ("glm-5.3-flashx", "zai/glm-5-3-flashx", None),
        ("qwen3.8-max", "alibaba/qwen3-8-max", None),
        ("gemini-4-argon-high", "google/gemini-4-argon", "high"),
        ("gemini-4-argon-medium", "google/gemini-4-argon", "medium"),
        ("gemini-4-argon-low", "google/gemini-4-argon", "low"),
        ("gemini-3.8-flash-high", "google/gemini-3-8-flash", "high"),
        ("claude-opus-5-5-high", "anthropic/claude-opus-5-5", "high"),
        ("claude-opus-5-5-medium", "anthropic/claude-opus-5-5", "medium"),
        ("claude-opus-5-5-low", "anthropic/claude-opus-5-5", "low"),
        ("gpt-oss-120b-medium", None, "medium"),
        ("claude-fable-5-1", None, None),
        ("claude-fable-5-1[1m]", None, None),
        ("codex-auto-review", None, None),
        ("gpt-reserve", None, None),
        ("<cli_active_default>", None, None),
    ],
)
def test_real_harness_model_ids_match_the_expected_market_entry(models, model_id, expected_key, effort):
    result = match_model(model_id, models)
    assert result.key == expected_key, (model_id, result)
    assert result.effort_variant == effort


def test_gemini_argon_high_matches_google_argon_and_keeps_the_effort_variant(models):
    result = match_model("gemini-4-argon-high", models)
    assert result.key == "google/gemini-4-argon"
    assert result.effort_variant == "high"
    assert result.confidence in {"exact", "normalized"}


def test_glm_family_variants_never_cross_match(models):
    plain = match_model("glm-5.3", models)
    flash = match_model("glm-5.3-flash", models)
    flashx = match_model("glm-5.3-flashx", models)

    assert plain.key == "zai/glm-5-3"
    assert flash.key == "zai/glm-5-3-flash"
    assert flashx.key == "zai/glm-5-3-flashx"
    assert len({plain.key, flash.key, flashx.key}) == 3


def test_deepseek_flash_does_not_cross_into_other_families(models):
    result = match_model("deepseek-v4.1-flash", models)
    assert result.key == "deepseek/deepseek-v4-1-flash"
    assert result.key not in {
        "deepseek/deepseek-v4-flash-0731",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
    }
    assert match_model("deepseek-v4.1-flash-1017", models).key == "deepseek/deepseek-v4-1-flash-1017"


def test_same_name_from_two_providers_folds_to_one_entry(models):
    # models.dev lists ``kimi-k3`` under both alibaba and moonshotai, and the
    # canonical id folds them into one entry instead of two ambiguous ones.
    matches = [model for model in models if model.key.endswith("/kimi-k3")]
    assert len(matches) == 1
    assert match_model("kimi-k3", models).key == matches[0].key


def test_ambiguous_names_resolve_to_none():
    first = MarketModel(key="alpha/shared", name="shared", aliases=["shared"], sources=["openrouter"])
    second = MarketModel(key="beta/shared", name="shared", aliases=["shared"], sources=["openrouter"])
    result = match_model("shared", [first, second])
    assert result.key is None
    assert result.confidence == "none"


def test_alias_table_overrides_normal_matching(models):
    aliases = {
        "claude-fable-5-1": "anthropic/claude-opus-5-5",
        "internal-*": "google/gemini-4-argon",
    }
    fable = match_model("claude-fable-5-1", models, aliases=aliases)
    assert fable.key == "anthropic/claude-opus-5-5"
    assert fable.confidence == "alias"

    internal = match_model("internal-model-x", models, aliases=aliases)
    assert internal.key == "google/gemini-4-argon"
    assert internal.confidence == "alias"

    missing = match_model("claude-fable-5-1", models, aliases={"claude-fable-5-1": "nope/none"})
    assert missing.key is None


def test_short_snapshot_suffix_folds_only_when_unique():
    dated = MarketModel(
        key="vendor/model-1017",
        name="model 1017",
        aliases=["model-1017"],
        sources=["openrouter"],
    )
    result = match_model("model", [dated])
    assert result.key == "vendor/model-1017"
    assert result.confidence == "normalized"

    second = MarketModel(
        key="vendor/model-0731",
        name="model 0731",
        aliases=["model-0731"],
        sources=["openrouter"],
    )
    assert match_model("model", [dated, second]).key is None


def test_eight_digit_date_is_ignored_after_normalisation():
    name, effort = normalize_model_identity("claude-haiku-4-5-20251001")
    assert (name, effort) == ("claude-haiku-4-5", None)
    assert normalize_model_identity("Gemini-4-Argon-HIGH") == ("gemini-4-argon", "high")
    assert normalize_model_identity("claude-opus-5.5") == ("claude-opus-5-5", None)


def test_best_match_prefers_the_strongest_variant(models):
    result = best_match(["claude-fable-5-1[1m]", "claude-opus-5-5[1m]"], models)
    assert result.key == "anthropic/claude-opus-5-5"
    assert result.confidence in {"exact", "normalized"}


def test_unmatched_listing_skips_non_model_roles(models):
    harness_models = {
        "claude_code": ["claude-opus-5-5", "claude-fable-5-1"],
        "codex": ["gpt-6-astra", "codex-auto-review", "gpt-reserve"],
        "zcode": ["GLM-5.3", "gpt-oss-120b-medium"],
        "deepseek": ["<cli_active_default>"],
    }
    unmatched = list_unmatched(harness_models, models)
    pairs = {(item["harness"], item["model_id"]) for item in unmatched}
    assert pairs == {
        ("claude_code", "claude-fable-5-1"),
        ("zcode", "gpt-oss-120b-medium"),
    }
    assert all(item["effort_variant"] == "" for item in unmatched if item["model_id"] == "claude-fable-5-1")


def test_vendor_prefixed_harness_ids_still_match(models):
    result = match_model("anthropic/claude-opus-5-5", models)
    assert result.key == "anthropic/claude-opus-5-5"
    assert result.confidence == "exact"
