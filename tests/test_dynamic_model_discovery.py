"""Dynamic model discovery, tiering and runtime unauthorised-model self-healing."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from harness_mesh.models import DiscoveredModel, HarnessStatus, TaskRequest, TaskStatus
from tests.helpers import (
    FakeAdapter,
    failure,
    make_mesh,
    success,
    write_claude_settings,
    write_codex_config,
    write_dsh_config,
    write_gemini_settings,
    write_json,
    write_zcode_config,
)


@pytest.mark.asyncio
async def test_local_config_discovers_single_model_and_binds_all_tiers(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9-1")

        models = await mesh.discovery.discover_for_harness("claude_code", force_refresh=True)

        assert len(models) == 1
        model = models[0]
        assert model.model_id == "claude-opus-9-1"
        assert model.source == "local_config"
        assert model.authorized is True
        assert set(model.assigned_tiers) == {"low", "medium", "high", "critical"}
        assert model.capability_score > 20

        tier_map = mesh.discovery.get_tier_models("claude_code")
        assert set(tier_map) == {"low", "medium", "high", "critical"}
        assert set(tier_map.values()) == {"claude-opus-9-1"}
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_multi_model_discovery_from_claude_codex_and_dsh_configs(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        write_claude_settings(
            mesh.home_dir,
            model="claude-sonnet-4-5",
            env={"ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4-5"},
        )
        write_codex_config(
            mesh.home_dir,
            model="gpt-5-codex",
            profiles={"fast": "gpt-5-mini"},
        )
        write_dsh_config(
            mesh.home_dir,
            models=["deepseek-chat", "deepseek-reasoner"],
            default_model="deepseek-reasoner",
        )

        claude_models = await mesh.discovery.discover_for_harness("claude_code", force_refresh=True)
        codex_models = await mesh.discovery.discover_for_harness("codex", force_refresh=True)
        dsh_models = await mesh.discovery.discover_for_harness("deepseek", force_refresh=True)

        assert {model.model_id for model in claude_models} == {
            "claude-sonnet-4-5",
            "claude-haiku-4-5",
        }
        assert {model.model_id for model in codex_models} == {"gpt-5-codex", "gpt-5-mini"}
        assert {model.model_id for model in dsh_models} == {"deepseek-chat", "deepseek-reasoner"}

        for harness, models in (
            ("claude_code", claude_models),
            ("codex", codex_models),
            ("deepseek", dsh_models),
        ):
            tier_map = mesh.discovery.get_tier_models(harness)
            assert set(tier_map) == {"low", "medium", "high", "critical"}
            ranked = sorted(models, key=lambda item: item.capability_score, reverse=True)
            assert tier_map["critical"] == ranked[0].model_id
            assert tier_map["low"] == ranked[-1].model_id
            assert len({model.capability_score for model in models}) == 2

        claude_by_id = {model.model_id: model for model in claude_models}
        assert claude_by_id["claude-haiku-4-5"].source == "local_config"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_live_v1_models_endpoint_is_merged_into_inventory(tmp_path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path.endswith("/v1/models")
        assert request.headers.get("x-api-key") == "test-key"
        return httpx.Response(200, json={"data": [{"id": "claude-live-9"}, {"id": "claude-live-mini"}]})

    mesh = make_mesh(
        tmp_path,
        env={"ANTHROPIC_API_KEY": "test-key"},
        http_transport=httpx.MockTransport(handler),
    )
    try:
        models = await mesh.discovery.discover_for_harness("claude_code", force_refresh=True)
        by_id = {model.model_id: model for model in models}

        assert "claude-live-9" in by_id
        assert "claude-live-mini" in by_id
        assert by_id["claude-live-9"].source == "api_models"
        assert by_id["claude-live-9"].authorized is True
        assert requests and requests[0].url.host == "api.anthropic.com"

        tier_map = mesh.discovery.get_tier_models("claude_code")
        assert tier_map["critical"] == "claude-live-9"
        assert tier_map["low"] == "claude-live-mini"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_gemini_models_endpoint_filters_non_generate_content_models(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/v1beta/models" in request.url.path
        assert request.headers.get("x-goog-api-key") == "gemini-key"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-3-pro-preview",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/text-embedding-004",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
        )

    mesh = make_mesh(
        tmp_path,
        env={"GEMINI_API_KEY": "gemini-key"},
        http_transport=httpx.MockTransport(handler),
    )
    try:
        write_gemini_settings(mesh.home_dir, model="gemini-3-pro-local")
        models = await mesh.discovery.discover_for_harness("antigravity", force_refresh=True)
        ids = {model.model_id for model in models}

        assert "gemini-3-pro-local" in ids
        assert "gemini-3-pro-preview" in ids
        assert "text-embedding-004" not in ids
        assert "models/gemini-3-pro-preview" not in ids
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_model_alias_resolution_favours_the_requested_difficulty(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        write_claude_settings(
            mesh.home_dir,
            model="claude-opus-9",
            env={"ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-9"},
        )
        await mesh.discovery.discover_for_harness("claude_code", force_refresh=True)

        critical = mesh.discovery.resolve_model("claude_code", "critical")
        low = mesh.discovery.resolve_model("claude_code", "low")
        assert critical is not None and critical.model_id == "claude-opus-9"
        assert low is not None and low.model_id == "claude-haiku-9"
    finally:
        mesh.storage.close()


def test_models_allow_spec_shaped_status_and_require_discovery_source():
    status = HarnessStatus(available=False)
    assert status.harness == ""
    with pytest.raises(ValidationError):
        DiscoveredModel(harness="claude_code", model_id="some-model")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_antigravity_sdk_only_installation_gets_active_default(tmp_path):
    mesh = make_mesh(
        tmp_path,
        module_available=lambda name: name == "google.antigravity",
    )
    try:
        models = await mesh.discovery.discover_for_harness("antigravity", force_refresh=True)
        assert len(models) == 1
        placeholder = models[0]
        assert placeholder.model_id == "<cli_active_default>"
        assert placeholder.source == "cli_active_default"
        assert placeholder.is_active_default is True
        assert set(placeholder.assigned_tiers) == {"low", "medium", "high", "critical"}
        assert set(mesh.discovery.get_tier_models("antigravity").values()) == {"<cli_active_default>"}
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_antigravity_and_zcode_local_configs_are_discovered(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        write_gemini_settings(mesh.home_dir, model="gemini-local-pro")
        write_zcode_config(mesh.home_dir, model="zcode-local-pro")

        antigravity = await mesh.discovery.discover_for_harness("antigravity", force_refresh=True)
        zcode = await mesh.discovery.discover_for_harness("zcode", force_refresh=True)

        assert {model.model_id for model in antigravity} == {"gemini-local-pro"}
        assert {model.model_id for model in zcode} == {"zcode-local-pro"}
        assert antigravity[0].source == "local_config"
        assert zcode[0].source == "local_config"
        assert mesh.discovery.get_tier_models("antigravity")["critical"] == "gemini-local-pro"
        assert mesh.discovery.get_tier_models("zcode")["critical"] == "zcode-local-pro"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_claude_catalog_metadata_subscription_tiers_and_extra_cache(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        catalog = {
            "catalog": {
                "config": {
                    "models": [
                        {
                            "id": "demo-opus-5-5",
                            "runtime": {"family": "opus", "max_input_tokens": 1000000},
                            "thinking": {"type": "effort"},
                        },
                        {
                            "id": "demo-sonnet-5-5",
                            "runtime": {"family": "sonnet", "max_input_tokens": 1000000},
                            "thinking": {"type": "effort"},
                        },
                        {
                            "id": "demo-haiku-5-5",
                            "runtime": {"family": "haiku", "max_input_tokens": 1000000},
                            "thinking": {"type": "effort"},
                        },
                        {
                            "id": "demo-fable-5-1",
                            "runtime": {"family": "fable", "max_input_tokens": 1000000},
                            "thinking": {"type": "effort"},
                            "badge": {"message": "Requires usage credits"},
                        },
                        {
                            "id": "demo-opus-4-8",
                            "runtime": {"family": "opus", "max_input_tokens": 1000000},
                            "thinking": {"type": "effort"},
                        },
                    ]
                },
                "state": {"model": "demo-sonnet-5-5"},
            }
        }
        write_json(mesh.home_dir / ".claude" / "cache" / "model-catalog" / "tok-test-ccd.json", catalog)
        write_json(
            mesh.home_dir / ".claude.json",
            {
                "oauthAccount": {"hasExtraUsageEnabled": False},
                "additionalModelOptionsCache": [{"value": "demo-fable-5-1[1m]", "label": "Fable"}],
            },
        )

        models = await mesh.discovery.discover_for_harness("claude_code", force_refresh=True)
        by_id = {model.model_id: model for model in models}
        assert {"demo-opus-5-5", "demo-sonnet-5-5", "demo-haiku-5-5"} <= set(by_id)
        assert "demo-fable-5-1[1m]" in by_id
        assert by_id["demo-sonnet-5-5"].is_active_default is True
        assert by_id["demo-opus-5-5"].context_window == 1_000_000
        assert by_id["demo-opus-5-5"].supports_reasoning is True
        assert by_id["demo-opus-5-5"].capability_score > by_id["demo-fable-5-1"].capability_score

        tiers = mesh.discovery.get_tier_models("claude_code")
        assert tiers["critical"] == "demo-opus-5-5"
        assert tiers["high"] == "demo-opus-5-5"
        assert tiers["medium"] == "demo-sonnet-5-5"
        assert tiers["low"] == "demo-haiku-5-5"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_codex_slug_and_zcode_v2_fields_are_discovered(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        write_json(
            mesh.home_dir / ".codex" / "models_cache.json",
            {
                "models": [
                    {"slug": "demo-gpt-6.1-sol", "display_name": "GPT-6.1-Sol"},
                    {"slug": "demo-gpt-6-astra", "display_name": "GPT-6 Astra"},
                ]
            },
        )
        write_json(
            mesh.home_dir / ".zcode" / "v2" / "config.json",
            {
                "provider": {
                    "builtin:bigmodel": {
                        "models": {
                            "demo-glm-5.3": {"limit": {"context": 1000000}},
                            "demo-glm-5.3-flash": {"limit": {"context": 1000000}},
                        }
                    }
                }
            },
        )
        write_json(
            mesh.home_dir / ".zcode" / "v2" / "provider_config.json",
            {
                "config": {
                    "providerConfigRules": {
                        "providerRules": [
                            {
                                "providerId": "opencode-go-chat",
                                "config": {
                                    "personalModelIds": ["demo-deepseek-4.1-flash"],
                                    "modelOrder": ["demo-gpt-5.6-luna", "demo-grok-4.6"],
                                },
                            }
                        ]
                    }
                }
            },
        )

        codex = {model.model_id for model in await mesh.discovery.discover_for_harness("codex", force_refresh=True)}
        assert {"demo-gpt-6.1-sol", "demo-gpt-6-astra"} <= codex
        zcode = {model.model_id for model in await mesh.discovery.discover_for_harness("zcode", force_refresh=True)}
        assert {
            "demo-glm-5.3",
            "demo-glm-5.3-flash",
            "demo-deepseek-4.1-flash",
            "demo-gpt-5.6-luna",
            "demo-grok-4.6",
        } <= zcode
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_agy_spinner_frames_are_cleaned_from_cli_models(tmp_path):
    async def spinner_runner(args, cwd, timeout):
        return (
            0,
            "Fetching available models...\n"
            "\r\u2839 Fetching available models...\r\u28b9 Fetching available models..."
            "\ndemo-gemini-4-argon-high\tGemini 4 Argon (High)\n"
            "demo-oss-120b-medium\tGPT OSS 120B (Medium)\n",
            "",
        )

    mesh = make_mesh(
        tmp_path,
        which=lambda name: "/fake/agy" if name == "agy" else None,
        command_runner=spinner_runner,
    )
    try:
        models = await mesh.discovery.discover_for_harness("antigravity", force_refresh=True)
        ids = {model.model_id for model in models}
        assert ids == {"demo-gemini-4-argon-high", "demo-oss-120b-medium"}
        assert not any("fetching" in model_id.lower() for model_id in ids)
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_cli_enumeration_and_active_default_fallback(tmp_path):
    async def enumerating_runner(args, cwd, timeout):
        return 0, "claude-opus-9\nclaude-haiku-9\n", ""

    mesh = make_mesh(
        tmp_path / "enumerated",
        which=lambda name: "/fake/claude" if name == "claude" else None,
        command_runner=enumerating_runner,
    )
    try:
        models = await mesh.discovery.discover_for_harness("claude_code", force_refresh=True)
        by_id = {model.model_id: model for model in models}
        assert set(by_id) == {"claude-opus-9", "claude-haiku-9"}
        assert all(model.source == "cli_probe" for model in models)
        assert "<cli_active_default>" not in by_id
    finally:
        mesh.storage.close()

    async def silent_runner(args, cwd, timeout):
        return 0, "", ""

    inactive_mesh = make_mesh(
        tmp_path / "active-default",
        which=lambda name: "/fake/claude" if name == "claude" else None,
        command_runner=silent_runner,
    )
    try:
        models = await inactive_mesh.discovery.discover_for_harness("claude_code", force_refresh=True)
        assert len(models) == 1
        placeholder = models[0]
        assert placeholder.model_id == "<cli_active_default>"
        assert placeholder.source == "cli_active_default"
        assert placeholder.is_active_default is True
        assert set(placeholder.assigned_tiers) == {"low", "medium", "high", "critical"}
    finally:
        inactive_mesh.storage.close()

    inventory_mesh = make_mesh(
        tmp_path / "inventory",
        which=lambda name: "/fake/claude" if name == "claude" else None,
        command_runner=silent_runner,
    )
    try:
        write_claude_settings(inventory_mesh.home_dir, model="claude-opus-9")
        models = await inventory_mesh.discovery.discover_for_harness("claude_code", force_refresh=True)
        assert {model.model_id for model in models} == {"claude-opus-9"}
        assert all(not model.is_active_default for model in models)
    finally:
        inventory_mesh.storage.close()


@pytest.mark.asyncio
async def test_unauthorized_model_self_heals_to_next_authorized_model(tmp_path):
    def handler(adapter, call):
        if call.model == "claude-opus-9":
            return failure("model_not_found: 403 the model does not exist", exit_code=1)
        return success("completed with fallback model")

    fake = FakeAdapter("claude_code", handler)
    mesh = make_mesh(tmp_path, adapters={"claude_code": fake})
    try:
        write_claude_settings(
            mesh.home_dir,
            model="claude-opus-9",
            env={"ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-9"},
        )
        request = TaskRequest(
            target_harness="claude_code",
            objective="Implement a small deterministic helper function.",
            difficulty="critical",
            workspace_root=str(mesh.workspace_root),
        )
        result = await mesh.router.dispatch(request)

        assert result.status == TaskStatus.COMPLETED
        assert result.resolved_model == "claude-haiku-9"
        assert result.initial_harness == "claude_code"
        assert result.target_harness == "claude_code"
        assert result.failover_count == 1
        assert "model_unauthorized" in result.failover_history[0].reason
        assert [assignment.status for assignment in result.assignments] == ["ended_error", "completed"]
        assert len(fake.calls) == 2
        assert "claude-opus-9" not in [model.model_id for model in mesh.discovery.authorized_models("claude_code")]

        status = mesh.router.get_task_status(request.task_id)
        assert status["status"] == "completed"
        assert len(status["assignments"]) == 2
    finally:
        mesh.storage.close()
