"""Hands-off automation: auto refresh, auto routing and market-backed tiers."""

from __future__ import annotations

import asyncio
import json
import re
import time

import pytest

import harness_mesh.cli as cli_module
from harness_mesh.market import MarketModel, MarketProvider
from harness_mesh.mcp_server import MESH_INSTRUCTIONS, create_server
from harness_mesh.models import DiscoveredModel, TaskRequest, TaskStatus
from tests.helpers import FakeAdapter, make_mesh, success


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _tool_payload(result):
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def _model(key: str, *, intelligence: float | None = 80.0, price=(1.0, 3.0)) -> MarketModel:
    return MarketModel(
        key=key,
        vendor=key.split("/", 1)[0],
        name=key.split("/", 1)[1],
        input_per_mtok=price[0],
        output_per_mtok=price[1],
        intelligence=intelligence,
        intelligence_source="epoch_ai" if intelligence is not None else None,
        intelligence_reasoning=intelligence,
        intelligence_coding=intelligence,
        benchmarks_used=["gpqa_diamond"],
        benchmark_data_date="2026-10-26",
        sources=["epoch"],
        release_date="2026-09-01",
    )


def _discovered(harness: str, model_id: str, score: float = 20.0) -> DiscoveredModel:
    return DiscoveredModel(
        harness=harness,
        model_id=model_id,
        source="local_config",
        capability_score=score,
        discovered_at=time.time(),
    )


class _ImmediateProvider(MarketProvider):
    name = "epoch"

    def __init__(self, models: list[MarketModel]):
        self.models = models
        self.calls = 0

    async def fetch(self) -> list[MarketModel]:
        self.calls += 1
        return list(self.models)


class _BlockingProvider(MarketProvider):
    name = "epoch"

    def __init__(self, models: list[MarketModel]):
        self.models = models
        self.calls = 0
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def fetch(self) -> list[MarketModel]:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return list(self.models)


# ---------------------------------------------------------------------------
# dispatch without a harness or model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_dispatch_needs_only_an_objective(tmp_path):
    adapter = FakeAdapter("claude_code", lambda adapter, call: success("auto done"))
    mesh = make_mesh(tmp_path, adapters={"claude_code": adapter})
    mesh.storage.save_market_snapshot(
        "epoch",
        time.time(),
        {
            "models": [
                _model("anthropic/claude-opus-5-5", intelligence=90.0).model_dump(mode="json"),
                _model("openai/gpt-6-astra", intelligence=60.0).model_dump(mode="json"),
            ],
            "fetched_at": time.time(),
        },
    )
    mesh.storage.save_discovered_models(
        [
            _discovered("claude_code", "claude-opus-5-5"),
            _discovered("codex", "gpt-6-astra"),
        ]
    )
    try:
        server = create_server(mesh.router)
        payload = _tool_payload(
            await server.call_tool(
                "dispatch_task",
                {
                    "objective": "Implement the routing flag",
                    "task_kind": "implement",
                    "workspace_root": str(mesh.workspace_root),
                },
            )
        )
        assert payload["status"] == TaskStatus.COMPLETED.value, payload
        assert payload["target_harness"] != "auto"
        assert payload["routing"]["chosen"] is not None
        assert payload["routing"]["chosen"]["model"] == "claude-opus-5-5"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_dispatch_defaults_target_harness_to_auto(tmp_path):
    request = TaskRequest(objective="Implement the flag")
    assert request.target_harness == "auto"


def test_parallel_dispatch_accepts_tasks_without_a_target(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        server = create_server(mesh.router)
        assert "target_harness" not in (
            server._tool_manager.get_tool("parallel_dispatch").parameters.get("required") or []
        )
    finally:
        mesh.storage.close()


# ---------------------------------------------------------------------------
# MCP instructions
# ---------------------------------------------------------------------------


def test_mcp_initialize_carries_short_instructions(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        server = create_server(mesh.router)
        options = server._mcp_server.create_initialization_options()
        assert options.instructions == MESH_INSTRUCTIONS
        assert "do not pass model" in MESH_INSTRUCTIONS.lower()
        assert "task_kind" in MESH_INSTRUCTIONS
        assert "get_task_status" in MESH_INSTRUCTIONS
        assert "apply_task_patch" in MESH_INSTRUCTIONS
        assert "/Users/" not in MESH_INSTRUCTIONS
        assert "objective" not in MESH_INSTRUCTIONS.lower() or "数据" not in MESH_INSTRUCTIONS
    finally:
        mesh.storage.close()


# ---------------------------------------------------------------------------
# automatic refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_dispatch_refreshes_the_empty_cache(tmp_path):
    adapter = FakeAdapter("claude_code", lambda adapter, call: success("ok"))
    mesh = make_mesh(tmp_path, adapters={"claude_code": adapter})
    provider = _ImmediateProvider([_model("anthropic/claude-opus-5-5", intelligence=90.0)])
    mesh.router.market.config.sources = ["epoch"]
    mesh.router.market.config.wait_seconds = 0.5
    mesh.router.market.providers = {"epoch": provider}
    mesh.storage.save_discovered_models([_discovered("claude_code", "claude-opus-5-5")])
    try:
        result = await mesh.router.dispatch(
            TaskRequest(
                objective="Implement the flag",
                task_kind="implement",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED.value, result.summary
        assert provider.calls == 1
        assert result.routing is not None
        assert result.routing["chosen"] is not None
        assert result.routing["chosen"]["intelligence_source"] == "epoch_ai"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_dispatch_times_out_to_the_heuristic_then_uses_the_finished_refresh(tmp_path):
    adapter = FakeAdapter("claude_code", lambda adapter, call: success("ok"))
    mesh = make_mesh(tmp_path, adapters={"claude_code": adapter})
    provider = _BlockingProvider([_model("anthropic/claude-opus-5-5", intelligence=90.0)])
    mesh.router.market.config.sources = ["epoch"]
    mesh.router.market.config.wait_seconds = 0.05
    mesh.router.market.providers = {"epoch": provider}
    mesh.storage.save_discovered_models([_discovered("claude_code", "claude-opus-5-5")])
    try:
        started = time.monotonic()
        first = await mesh.router.dispatch(
            TaskRequest(
                objective="Implement the flag",
                task_kind="implement",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        elapsed = time.monotonic() - started
        assert first.status == TaskStatus.COMPLETED, first.summary
        assert elapsed < 3.0
        assert first.routing is not None
        assert first.routing["chosen"] is None
        assert provider.calls == 1
        assert provider.started.is_set()

        provider.release.set()
        for _ in range(200):
            if mesh.router.market.catalog().models:
                break
            await asyncio.sleep(0.01)
        assert mesh.router.market.catalog().models

        second = await mesh.router.dispatch(
            TaskRequest(
                objective="Implement the flag",
                task_kind="implement",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert second.status == TaskStatus.COMPLETED, second.summary
        assert second.routing is not None
        assert second.routing["chosen"] is not None
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_market_disabled_or_offline_never_refreshes(tmp_path):
    mesh = make_mesh(tmp_path, env={"HARNESS_MESH_OFFLINE": "1"})
    provider = _ImmediateProvider([_model("v/x")])
    mesh.router.market.providers = {"epoch": provider}
    try:
        await mesh.router.market.ensure_fresh(timeout=0.05)
        assert provider.calls == 0

        mesh.config.market.enabled = False
        mesh.router.market.config.enabled = False
        await mesh.router.market.ensure_fresh(timeout=0.05)
        assert provider.calls == 0
    finally:
        mesh.storage.close()


def test_one_shot_cli_commands_are_bounded_by_the_configured_timeout(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    provider = _BlockingProvider([_model("v/x")])
    mesh.router.market.config.sources = ["epoch"]
    mesh.router.market.config.wait_seconds = 0.05
    mesh.router.market.providers = {"epoch": provider}
    mesh.storage.save_discovered_models([_discovered("claude_code", "claude-opus-5-5")])

    def factory():
        runtime = make_mesh(tmp_path)
        runtime.router.market.config.sources = ["epoch"]
        runtime.router.market.config.wait_seconds = 0.05
        runtime.router.market.providers = {"epoch": provider}
        return runtime

    monkeypatch.setattr(cli_module, "_runtime", lambda ctx: factory())
    from typer.testing import CliRunner

    try:
        started = time.monotonic()
        result = CliRunner().invoke(
            cli_module.app, ["route", "--objective", "Implement the flag", "--json"]
        )
        elapsed = time.monotonic() - started
        assert result.exit_code == 0, result.output
        assert elapsed < 3.0
        provider.release.set()
    finally:
        mesh.storage.close()


# ---------------------------------------------------------------------------
# discovery and market scores
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_harnesses_uses_market_scores_for_tiers(tmp_path):
    mesh = make_mesh(tmp_path)
    mesh.storage.save_market_snapshot(
        "epoch",
        time.time(),
        {
            "models": [
                _model("vendor/high", intelligence=95.0, price=(10.0, 40.0)).model_dump(mode="json"),
                _model("vendor/low", intelligence=30.0, price=(1.0, 3.0)).model_dump(mode="json"),
            ],
            "fetched_at": time.time(),
        },
    )
    mesh.storage.save_discovered_models(
        [
            _discovered("claude_code", "high", score=10.0),
            _discovered("claude_code", "low", score=99.0),
        ]
    )
    try:
        statuses = await mesh.router.list_harnesses()
        status = next(item for item in statuses if item.harness == "claude_code")
        assert status.model_details
        by_id = {item["model_id"]: item for item in status.model_details}
        assert by_id["high"]["intelligence"] == 95.0
        assert by_id["high"]["intelligence_source"] == "epoch_ai"
        assert by_id["high"]["blended_price"] == pytest.approx((3 * 10 + 40) / 4)
        # The tier map follows the market score, not the discovery heuristic.
        assert status.tier_models["critical"] == "high"
    finally:
        mesh.storage.close()


def test_models_and_status_show_market_scores(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    mesh.storage.save_market_snapshot(
        "epoch",
        time.time(),
        {
            "models": [
                _model("vendor/high", intelligence=95.0, price=(10.0, 40.0)).model_dump(mode="json")
            ],
            "fetched_at": time.time(),
        },
    )
    mesh.storage.save_discovered_models([_discovered("claude_code", "high", score=10.0)])

    def factory():
        runtime = make_mesh(tmp_path)
        return runtime

    monkeypatch.setattr(cli_module, "_runtime", lambda ctx: factory())
    from typer.testing import CliRunner

    try:
        models_out = _plain(
            CliRunner().invoke(cli_module.app, ["models", "--harness", "claude_code"]).output
        )
        assert "intel=95.0" in models_out
        assert "intel_source=epoch_ai" in models_out
        assert "blended=17.5" in models_out

        status_out = _plain(
            CliRunner().invoke(cli_module.app, ["status", "--json"]).output
        )
        payload = json.loads(status_out)
        claude = next(item for item in payload if item["harness"] == "claude_code")
        assert claude["model_details"][0]["intelligence"] == 95.0
    finally:
        mesh.storage.close()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_reports_market_health_without_writing(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    mesh.storage.save_market_snapshot(
        "epoch",
        time.time(),
        {
            "models": [_model("vendor/high").model_dump(mode="json")],
            "fetched_at": time.time(),
        },
    )
    mesh.storage.save_discovered_models(
        [_discovered("claude_code", "high"), _discovered("claude_code", "unknown-model")]
    )
    mesh.storage.close()
    from typer.testing import CliRunner

    try:
        result = CliRunner().invoke(
            cli_module.app,
            ["--workspace", str(mesh.workspace_root), "doctor", "--json"],
        )
        assert result.exit_code in {0, 1}, result.output
        payload = json.loads(_plain(result.output))
        market = next(item for item in payload if item["harness"] == "market")
        assert market["sources"] == ["epoch"]
        assert market["last_refresh"] is not None
        assert market["stale"] is False
        assert market["matched"] == 1
        assert market["models"] == 2
        assert market["unmatched"] == ["unknown-model"]
    finally:
        pass


# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------


def test_defaults_are_on_and_the_example_config_keeps_them():
    from pathlib import Path

    import yaml

    from harness_mesh.config import default_config

    config = default_config()
    assert config.market.enabled is True
    assert config.routing.mode == "auto"

    example = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "harnesses.example.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert example["market"]["enabled"] is True
    assert example["routing"]["mode"] == "auto"
    assert example["routing"]["premium_hints"]
    assert example["routing"]["value_hints"]
