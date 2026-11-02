"""Discovery and market egress safety tests (audit CR-03, M-08..M-10).

Covers the audit findings CR-03 (workspace-controlled base_url / key
exfiltration), M-08 (offline mode must skip the API layer), M-09/M-10 (proxy
trust, redirect handling, total fetch timeout) and the CR-02 input side
(provider labels never accept nested data).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import harness_mesh.discovery as discovery_module
import harness_mesh.market as market_module
from harness_mesh.config import MarketConfig, default_config
from harness_mesh.discovery import ModelDiscoveryEngine, _sanitize_remote_url
from harness_mesh.market import MarketProvider, MarketService, fetch_bytes
from harness_mesh.storage import StateStore
from tests.helpers import make_mesh, write_json


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ----------------------------------------------------------------------
# A2: base_url / api_key only from trusted sources
# ----------------------------------------------------------------------
def test_workspace_settings_base_url_and_key_are_ignored(tmp_path):
    mesh = make_mesh(tmp_path, env={"ANTHROPIC_API_KEY": "test-key"})
    write_json(
        mesh.workspace_root / ".claude" / "settings.json",
        {"base_url": "https://attacker.example", "api_key": "attacker-key"},
    )
    try:
        auth = mesh.discovery._resolve_auth(
            "claude_code", mesh.config.harnesses["claude_code"], mesh.workspace_root
        )
        assert auth["base_url"] == "https://api.anthropic.com"
        assert auth["api_key"] == "test-key"
    finally:
        mesh.storage.close()


def test_workspace_credentials_are_not_used_when_env_is_empty(tmp_path):
    mesh = make_mesh(tmp_path)
    write_json(
        mesh.workspace_root / ".claude" / "settings.json",
        {"api_key": "attacker-key"},
    )
    try:
        auth = mesh.discovery._resolve_auth(
            "claude_code", mesh.config.harnesses["claude_code"], mesh.workspace_root
        )
        assert auth["api_key"] is None
    finally:
        mesh.storage.close()


def test_user_level_config_base_url_is_used(tmp_path):
    mesh = make_mesh(tmp_path, env={"ANTHROPIC_API_KEY": "test-key"})
    write_json(mesh.home_dir / ".claude.json", {"base_url": "https://gateway.example"})
    try:
        auth = mesh.discovery._resolve_auth(
            "claude_code", mesh.config.harnesses["claude_code"], mesh.workspace_root
        )
        assert auth["base_url"] == "https://gateway.example"
    finally:
        mesh.storage.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8123",
        "http://localhost:8123",
        "http://[::1]:8123",
        "https://api.anthropic.com",
    ],
)
def test_safe_urls_are_accepted(url):
    assert _sanitize_remote_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://gateway.example",
        "ftp://127.0.0.1:8123",
        "https://user:pass@api.anthropic.com",
        "https://",
        "",
        None,
    ],
)
def test_unsafe_urls_are_rejected(url):
    assert _sanitize_remote_url(url) is None


@pytest.mark.asyncio
async def test_non_https_base_url_blocks_the_request(tmp_path):
    requests = []

    def handler(request: httpx.Request):  # pragma: no cover - must not run
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    mesh = make_mesh(
        tmp_path,
        env={"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_BASE_URL": "http://evil.example"},
        http_transport=httpx.MockTransport(handler),
    )
    try:
        models = await mesh.discovery._discover_from_api(
            "claude_code", mesh.config.harnesses["claude_code"], mesh.workspace_root
        )
        assert models == []
        assert requests == []
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_loopback_http_gateway_is_allowed(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "claude-local-1"}]})

    mesh = make_mesh(
        tmp_path,
        env={"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_BASE_URL": "http://127.0.0.1:8123"},
        http_transport=httpx.MockTransport(handler),
    )
    try:
        models = await mesh.discovery._discover_from_api(
            "claude_code", mesh.config.harnesses["claude_code"], mesh.workspace_root
        )
        assert [model.model_id for model in models] == ["claude-local-1"]
        assert seen and seen[0].url.host == "127.0.0.1"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_full_url_models_endpoint_must_still_pass_the_url_gate(tmp_path):
    requests = []

    def handler(request: httpx.Request):  # pragma: no cover - must not run
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    mesh = make_mesh(
        tmp_path,
        env={"ANTHROPIC_API_KEY": "test-key"},
        http_transport=httpx.MockTransport(handler),
    )
    harness_config = mesh.config.harnesses["claude_code"]
    harness_config.models_endpoint = "http://evil.example/v1/models"
    try:
        models = await mesh.discovery._discover_from_api(
            "claude_code", harness_config, mesh.workspace_root
        )
        assert models == []
        assert requests == []
    finally:
        mesh.storage.close()


# ----------------------------------------------------------------------
# A2: redirects are never followed
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cross_host_redirect_is_refused(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    mesh = make_mesh(
        tmp_path,
        env={"ANTHROPIC_API_KEY": "test-key"},
        http_transport=httpx.MockTransport(handler),
    )
    try:
        models = await mesh.discovery._discover_from_api(
            "claude_code", mesh.config.harnesses["claude_code"], mesh.workspace_root
        )
        assert models == []
        assert len(seen) == 1
        errors = mesh.discovery.last_errors.get("claude_code") or []
        assert any("refused redirect" in error for error in errors)
    finally:
        mesh.storage.close()


# ----------------------------------------------------------------------
# A3: offline mode skips the API layer unless a test transport is injected
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_offline_skips_api_probe_without_injected_transport(tmp_path, monkeypatch):
    mesh = make_mesh(
        tmp_path,
        env={"ANTHROPIC_API_KEY": "test-key", "HARNESS_MESH_OFFLINE": "1"},
    )
    created: list[str] = []

    class ExplodingClient:  # pragma: no cover - must not be created
        def __init__(self, *args, **kwargs):
            created.append("client")

        async def get(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("offline discovery must not build an HTTP client")

    monkeypatch.setattr(discovery_module.httpx, "AsyncClient", ExplodingClient)
    try:
        models = await mesh.discovery._discover_from_api(
            "claude_code", mesh.config.harnesses["claude_code"], mesh.workspace_root
        )
        assert models == []
        assert created == []
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_offline_still_allows_an_injected_test_transport(tmp_path):
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"data": [{"id": "claude-live-offline-test"}]})

    mesh = make_mesh(
        tmp_path,
        env={"ANTHROPIC_API_KEY": "test-key", "HARNESS_MESH_OFFLINE": "1"},
        http_transport=httpx.MockTransport(handler),
    )
    try:
        models = await mesh.discovery._discover_from_api(
            "claude_code", mesh.config.harnesses["claude_code"], mesh.workspace_root
        )
        assert [model.model_id for model in models] == ["claude-live-offline-test"]
    finally:
        mesh.storage.close()


# ----------------------------------------------------------------------
# A5: no environment proxy trust, no redirect following in the client
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_discovery_client_ignores_environment_proxy_settings(tmp_path, monkeypatch):
    captured: dict[str, object] = {}
    real_client = httpx.AsyncClient

    class RecordingClient(real_client):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    def handler(request: httpx.Request):
        return httpx.Response(200, json={"data": [{"id": "claude-live-1"}]})

    mesh = make_mesh(
        tmp_path,
        env={"ANTHROPIC_API_KEY": "test-key"},
        http_transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(discovery_module.httpx, "AsyncClient", RecordingClient)
    try:
        models = await mesh.discovery._discover_from_api(
            "claude_code", mesh.config.harnesses["claude_code"], mesh.workspace_root
        )
        assert [model.model_id for model in models] == ["claude-live-1"]
        assert captured.get("trust_env") is False
        assert captured.get("follow_redirects") is False
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_market_client_ignores_environment_proxy_settings(monkeypatch):
    captured: dict[str, object] = {}
    real_client = httpx.AsyncClient

    class RecordingClient(real_client):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(market_module.httpx, "AsyncClient", RecordingClient)
    body = await fetch_bytes(
        "https://openrouter.ai/api/v1/models",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'{"ok": true}')),
    )
    assert body == b'{"ok": true}'
    assert captured.get("trust_env") is False


# ----------------------------------------------------------------------
# A4: provider labels are short scalars only (CR-02 input side)
# ----------------------------------------------------------------------
def _engine(tmp_path, cfg, home, env):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = StateStore(tmp_path / "custom-state" / "state.db")
    return ModelDiscoveryEngine(
        cfg,
        store,
        workspace,
        home_dir=home,
        env=env,
    )


def test_codex_provider_dict_falls_back_to_openai(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config_file = home / "codex.json"
    write_json(config_file, {"model_provider": {"nested": "attacker"}, "model": "gpt-5-codex"})
    cfg = default_config()
    cfg.harnesses["codex"].config_files = [str(config_file)]
    engine = _engine(tmp_path, cfg, home, {"HOME": str(home)})
    try:
        models = engine._discover_codex(cfg.harnesses["codex"], tmp_path / "workspace")
        assert models
        assert {model.provider for model in models} == {"openai"}
    finally:
        engine.storage.close()


def test_codex_provider_keeps_a_short_scalar(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config_file = home / "codex.json"
    write_json(config_file, {"model_provider": "openrouter", "model": "gpt-5-codex"})
    cfg = default_config()
    cfg.harnesses["codex"].config_files = [str(config_file)]
    engine = _engine(tmp_path, cfg, home, {"HOME": str(home)})
    try:
        models = engine._discover_codex(cfg.harnesses["codex"], tmp_path / "workspace")
        assert models
        assert {model.provider for model in models} == {"openrouter"}
    finally:
        engine.storage.close()


def test_zcode_provider_dict_and_long_value_fall_back_to_harness_name(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cfg = default_config()
    engine = _engine(tmp_path, cfg, home, {"HOME": str(home)})
    workspace = tmp_path / "workspace"
    try:
        for payload in (
            {"provider": {"nested": "attacker"}, "model": "zcode-local-1"},
            {"provider": "x" * 100, "model": "zcode-local-2"},
        ):
            config_file = home / "zcode.json"
            write_json(config_file, payload)
            cfg.harnesses["zcode"].config_files = [str(config_file)]
            models = engine._discover_zcode(cfg.harnesses["zcode"], workspace)
            assert models
            assert {model.provider for model in models} == {"zcode"}
    finally:
        engine.storage.close()


def test_workspace_model_catalogue_still_contributes_models(tmp_path):
    """Workspace files are data for model *discovery*; only credentials are banned."""

    mesh = make_mesh(tmp_path)
    write_json(mesh.workspace_root / ".claude" / "settings.json", {"model": "claude-workspace-model"})
    try:
        models = mesh.discovery._discover_claude(
            mesh.config.harnesses["claude_code"], mesh.workspace_root
        )
        assert "claude-workspace-model" in {model.model_id for model in models}
    finally:
        mesh.storage.close()


# ----------------------------------------------------------------------
# A5: total fetch timeout for market providers
# ----------------------------------------------------------------------
class _SlowProvider(MarketProvider):
    name = "openrouter"

    async def fetch(self):
        await asyncio.sleep(0.2)
        return []


@pytest.mark.asyncio
async def test_market_total_timeout_is_enforced(tmp_path):
    store = StateStore(tmp_path / "market-state" / "state.db")
    service = MarketService(
        store,
        MarketConfig(sources=["openrouter"], total_timeout_seconds=0.01),
        env={},
        providers={"openrouter": _SlowProvider()},
    )
    try:
        result = await service.refresh(force=True)
        assert result.statuses
        assert result.statuses[0].status == "failed"
        assert "total timeout" in (result.statuses[0].error or "")
    finally:
        store.close()


def test_market_total_timeout_default_is_60_seconds():
    assert default_config().market.total_timeout_seconds == 60.0


@pytest.mark.asyncio
async def test_offline_mode_keeps_local_cli_enumeration(tmp_path):
    calls: list[list[str]] = []

    async def runner(args, cwd, timeout):
        calls.append(list(args))
        return 0, "cli-model-1\ncli-model-2\n", ""

    mesh = make_mesh(
        tmp_path,
        env={"HARNESS_MESH_OFFLINE": "1"},
        which=lambda name: f"/usr/bin/{name}",
        command_runner=runner,
    )
    try:
        models = await mesh.discovery._discover_from_cli(
            "claude_code", mesh.config.harnesses["claude_code"], mesh.workspace_root
        )
        assert {model.model_id for model in models} == {"cli-model-1", "cli-model-2"}
        assert calls
    finally:
        mesh.storage.close()
