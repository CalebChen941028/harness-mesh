"""Market CLI, MCP tools and non-blocking refresh."""

from __future__ import annotations

import asyncio
import json
import re
import time

import pytest
from typer.testing import CliRunner

import harness_mesh.cli as cli_module
from harness_mesh.market import MarketError, MarketModel, MarketProvider
from harness_mesh.mcp_server import TOOL_NAMES, create_server
from harness_mesh.models import DiscoveredModel, TaskStatus
from harness_mesh.storage import StateStore
from tests.helpers import FakeAdapter, make_mesh, success


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _invoke(monkeypatch, factory, *args: str):
    """Run the CLI against a fresh runtime so each command owns its connection."""

    monkeypatch.setattr(cli_module, "_runtime", lambda ctx: factory())
    return CliRunner().invoke(cli_module.app, list(args), env={"FORCE_COLOR": "1"})


def _model(
    key: str,
    *,
    intelligence: float | None = 42.0,
    input_price: float = 1.0,
    output_price: float = 2.0,
    source: str = "artificial_analysis",
) -> MarketModel:
    return MarketModel(
        key=key,
        vendor=key.split("/", 1)[0],
        name=key.split("/", 1)[1],
        input_per_mtok=input_price,
        output_per_mtok=output_price,
        intelligence=intelligence,
        intelligence_source=source if intelligence is not None else None,
        sources=[source],
        release_date="2026-10-01",
    )


def _seed_market(mesh, models: list[MarketModel]) -> None:
    now = time.time()
    mesh.storage.save_market_snapshot(
        "artificial_analysis",
        now,
        {
            "models": [model.model_dump(mode="json") for model in models],
            "fetched_at": now,
        },
    )


def _seed_models(mesh, models: list[DiscoveredModel]) -> None:
    mesh.storage.save_discovered_models(models)


def _discovered(harness: str, model_id: str, score: float = 40.0) -> DiscoveredModel:
    return DiscoveredModel(
        harness=harness,
        model_id=model_id,
        source="local_config",
        capability_score=score,
        discovered_at=time.time(),
    )


class _FakeProvider(MarketProvider):
    name = "openrouter"

    def __init__(self, models: list[MarketModel]):
        self.models = models
        self.calls = 0

    async def fetch(self) -> list[MarketModel]:
        self.calls += 1
        return list(self.models)


class _FailingProvider(MarketProvider):
    name = "openrouter"

    def __init__(self):
        self.calls = 0

    async def fetch(self) -> list[MarketModel]:
        self.calls += 1
        raise MarketError("network down")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_market_show_json_and_text(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    _seed_market(
        mesh,
        [
            _model("anthropic/claude-opus-5-5", intelligence=88.0, input_price=4, output_price=20),
            _model("zai/glm-5-3-flash", intelligence=None, input_price=0.15, output_price=0.5),
        ],
    )
    try:
        def factory():
            return make_mesh(tmp_path)

        result = _invoke(monkeypatch, factory, "market", "show", "--json")
        output = _plain(result.output)
        assert result.exit_code == 0, output
        payload = json.loads(output)
        assert {item["key"] for item in payload["models"]} == {
            "anthropic/claude-opus-5-5",
            "zai/glm-5-3-flash",
        }
        assert payload["sources"] == ["artificial_analysis"]
        assert payload["stale"] is False
        by_key = {item["key"]: item for item in payload["models"]}
        assert by_key["anthropic/claude-opus-5-5"]["intelligence_source"] == "artificial_analysis"
        # Models without an evaluation keep the heuristic label rather than an empty source.
        assert by_key["zai/glm-5-3-flash"]["intelligence_source"] == "heuristic"

        text = _plain(_invoke(monkeypatch, factory, "market", "show").output)
        assert "anthropic/claude-opus-5-5" in text
        assert "source=artificial_analysis" in text
    finally:
        mesh.storage.close()


def test_market_show_filters_by_model_glob(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    _seed_market(
        mesh,
        [
            _model("vendor/alpha", input_price=1, output_price=2),
            _model("other/beta", input_price=3, output_price=4),
        ],
    )
    try:
        result = _invoke(
            monkeypatch,
            lambda: make_mesh(tmp_path),
            "market",
            "show",
            "--model",
            "vendor/*",
            "--json",
        )
        payload = json.loads(_plain(result.output))
        assert [item["key"] for item in payload["models"]] == ["vendor/alpha"]
    finally:
        mesh.storage.close()


def test_market_show_unmatched_lists_only_real_models(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    _seed_market(mesh, [_model("anthropic/claude-opus-5-5")])
    _seed_models(
        mesh,
        [
            _discovered("claude_code", "claude-opus-5-5"),
            _discovered("claude_code", "claude-fable-5-1"),
            _discovered("codex", "codex-auto-review"),
            _discovered("codex", "gpt-reserve"),
        ],
    )
    try:
        def factory():
            return make_mesh(tmp_path)

        result = _invoke(monkeypatch, factory, "market", "show", "--unmatched", "--json")
        output = _plain(result.output)
        assert result.exit_code == 0, output
        payload = json.loads(output)
        assert payload["unmatched"] == [
            {
                "harness": "claude_code",
                "model_id": "claude-fable-5-1",
                "effort_variant": "",
                "confidence": "none",
            }
        ]
        text = _plain(_invoke(monkeypatch, factory, "market", "show", "--unmatched").output)
        assert "claude-fable-5-1" in text
        assert "market.aliases" in text
        assert "codex-auto-review" not in text
    finally:
        mesh.storage.close()


def test_market_refresh_writes_a_snapshot_and_reports_sources(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    provider = _FakeProvider([_model("vendor/refreshed", input_price=0.5, output_price=1.5)])
    mesh.router.market.config.sources = ["openrouter"]
    mesh.router.market.providers = {"openrouter": provider}
    def factory():
        runtime = make_mesh(tmp_path)
        runtime.router.market.config.sources = ["openrouter"]
        runtime.router.market.providers = {"openrouter": provider}
        return runtime

    try:
        result = _invoke(monkeypatch, factory, "market", "refresh", "--json")
        output = _plain(result.output)
        assert result.exit_code == 0, output
        payload = json.loads(output)
        assert provider.calls == 1
        assert payload["statuses"][0]["source"] == "openrouter"
        assert payload["statuses"][0]["status"] == "fetched"
        assert payload["catalog"]["models"][0]["key"] == "vendor/refreshed"
    finally:
        mesh.storage.close()

    store = StateStore(tmp_path / "workspace" / ".harness-mesh" / "state.db")
    try:
        snapshot = store.get_market_snapshot("openrouter")
        assert snapshot is not None
        assert snapshot["payload"]["models"][0]["key"] == "vendor/refreshed"
    finally:
        store.close()


def test_market_refresh_respects_the_offline_switch(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path, env={"HARNESS_MESH_OFFLINE": "1"})
    provider = _FakeProvider([_model("vendor/nope")])
    mesh.router.market.config.sources = ["openrouter"]
    mesh.router.market.providers = {"openrouter": provider}
    def factory():
        runtime = make_mesh(tmp_path, env={"HARNESS_MESH_OFFLINE": "1"})
        runtime.router.market.config.sources = ["openrouter"]
        runtime.router.market.providers = {"openrouter": provider}
        return runtime

    try:
        result = _invoke(monkeypatch, factory, "market", "refresh", "--json")
        payload = json.loads(_plain(result.output))
        assert provider.calls == 0
        assert payload["statuses"][0]["status"] == "offline"
    finally:
        mesh.storage.close()


def test_route_previews_without_dispatching(tmp_path, monkeypatch):
    adapter = FakeAdapter("claude_code", lambda adapter, call: success("should not run"))
    mesh = make_mesh(tmp_path, adapters={"claude_code": adapter})
    _seed_market(mesh, [_model("anthropic/claude-opus-5-5", intelligence=88.0)])
    _seed_models(mesh, [_discovered("claude_code", "claude-opus-5-5")])
    try:
        def factory():
            return make_mesh(tmp_path, adapters={"claude_code": adapter})

        result = _invoke(
            monkeypatch,
            factory,
            "route",
            "--objective",
            "Audit the authentication design",
            "--difficulty",
            "high",
            "--json",
        )
        output = _plain(result.output)
        assert result.exit_code == 0, output
        payload = json.loads(output)
        assert payload["task_kind"] == "audit"
        assert payload["tier"] == "premium"
        assert payload["routing"]["chosen"]["model"] == "claude-opus-5-5"
        assert mesh.storage.list_tasks() == []
        assert not adapter.calls

        text = _plain(
            _invoke(
                monkeypatch,
                factory,
                "route",
                "--objective",
                "Design the release plan",
                "--kind",
                "plan",
                "--harness",
                "claude_code",
            ).output
        )
        assert "tier=premium" in text
        assert "route: claude_code/claude-opus-5-5" in text
    finally:
        mesh.storage.close()


def test_dispatch_cli_exposes_kind_and_routing_mode(tmp_path, monkeypatch):
    adapter = FakeAdapter("claude_code", lambda adapter, call: success("cli dispatch done"))
    mesh = make_mesh(tmp_path, adapters={"claude_code": adapter})
    _seed_market(mesh, [_model("anthropic/claude-opus-5-5", intelligence=88.0)])
    _seed_models(mesh, [_discovered("claude_code", "claude-opus-5-5")])
    try:
        result = _invoke(
            monkeypatch,
            lambda: make_mesh(tmp_path, adapters={"claude_code": adapter}),
            "dispatch",
            "--objective",
            "Implement the flag",
            "--harness",
            "claude_code",
            "--difficulty",
            "medium",
            "--kind",
            "implement",
            "--routing-mode",
            "off",
            "--json",
        )
        output = _plain(result.output)
        assert result.exit_code == 0, output
        payload = json.loads(output)
        assert payload["status"] == TaskStatus.COMPLETED.value
        assert payload["routing"]["mode"] == "off"
        assert payload["routing"]["task_kind"] == "implement"
    finally:
        mesh.storage.close()


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


def _tool_payload(result):
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


@pytest.mark.asyncio
async def test_mcp_exposes_fifteen_tools_with_market_and_routing(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        server = create_server(mesh.router)
        tools = await server.list_tools()
        names = {tool.name for tool in tools}
        assert names == set(TOOL_NAMES)
        assert len(names) == 15
        assert {"get_model_market", "preview_routing"} <= names

        by_name = {tool.name: tool for tool in tools}
        dispatch_schema = by_name["dispatch_task"].inputSchema
        assert {
            "task_kind",
            "routing_mode",
            "max_price_per_mtok",
            "min_intelligence",
        } <= set(dispatch_schema["properties"])
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_mcp_get_model_market_reads_the_cache(tmp_path):
    mesh = make_mesh(tmp_path)
    _seed_market(
        mesh,
        [
            _model("anthropic/claude-opus-5-5", intelligence=88.0),
            _model("zai/glm-5-3-flash", intelligence=55.0),
        ],
    )
    try:
        server = create_server(mesh.router)
        payload = _tool_payload(
            await server.call_tool("get_model_market", {"query": "glm", "limit": 10})
        )
        assert payload["returned"] == 1
        assert payload["models"][0]["key"] == "zai/glm-5-3-flash"
        assert payload["models"][0]["intelligence_source"] == "artificial_analysis"
        assert "api_key" not in json.dumps(payload).lower()
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_mcp_preview_routing_matches_the_planner(tmp_path):
    mesh = make_mesh(tmp_path, adapters={"claude_code": FakeAdapter("claude_code", lambda a, c: success())})
    _seed_market(mesh, [_model("anthropic/claude-opus-5-5", intelligence=88.0)])
    _seed_models(mesh, [_discovered("claude_code", "claude-opus-5-5")])
    try:
        server = create_server(mesh.router)
        payload = _tool_payload(
            await server.call_tool(
                "preview_routing",
                {"objective": "Design the architecture", "task_kind": "plan", "difficulty": "high"},
            )
        )
        assert payload["task_kind"] == "plan"
        assert payload["tier"] == "premium"
        assert payload["routing"]["chosen"]["model"] == "claude-opus-5-5"
        assert mesh.storage.list_tasks() == []
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_mcp_dispatch_accepts_kind_and_routing_mode(tmp_path):
    adapter = FakeAdapter("claude_code", lambda adapter, call: success("mcp dispatch done"))
    mesh = make_mesh(tmp_path, adapters={"claude_code": adapter})
    _seed_market(mesh, [_model("anthropic/claude-opus-5-5", intelligence=88.0)])
    _seed_models(mesh, [_discovered("claude_code", "claude-opus-5-5")])
    try:
        server = create_server(mesh.router)
        payload = _tool_payload(
            await server.call_tool(
                "dispatch_task",
                {
                    "objective": "Implement the flag",
                    "target_harness": "claude_code",
                    "difficulty": "medium",
                    "task_kind": "implement",
                    "routing_mode": "off",
                },
            )
        )
        assert payload["status"] == TaskStatus.COMPLETED.value
        assert payload["routing"]["mode"] == "off"
        assert payload["routing"]["task_kind"] == "implement"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_mcp_parallel_dispatch_accepts_routing_fields(tmp_path):
    adapter = FakeAdapter("claude_code", lambda adapter, call: success("parallel done"))
    mesh = make_mesh(tmp_path, adapters={"claude_code": adapter})
    _seed_market(mesh, [_model("anthropic/claude-opus-5-5", intelligence=88.0)])
    _seed_models(mesh, [_discovered("claude_code", "claude-opus-5-5")])
    try:
        server = create_server(mesh.router)
        payload = _tool_payload(
            await server.call_tool(
                "parallel_dispatch",
                {
                    "tasks": [
                        {
                            "objective": "Implement the flag",
                            "target_harness": "claude_code",
                            "difficulty": "medium",
                            "task_kind": "implement",
                            "routing_mode": "off",
                            "workspace_root": str(mesh.workspace_root),
                        }
                    ]
                },
            )
        )
        result = payload["results"][0]
        assert result["status"] == TaskStatus.COMPLETED.value
        assert result["routing"]["mode"] == "off"
        assert result["routing"]["task_kind"] == "implement"
    finally:
        mesh.storage.close()


# ---------------------------------------------------------------------------
# background refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_background_refresh_failure_never_blocks_dispatch(tmp_path):
    adapter = FakeAdapter("claude_code", lambda adapter, call: success("dispatch survived"))
    mesh = make_mesh(tmp_path, adapters={"claude_code": adapter})
    _seed_models(mesh, [_discovered("claude_code", "claude-opus-5-5", 55.3)])
    failing = _FailingProvider()
    mesh.router.market.config.sources = ["openrouter"]
    mesh.router.market.providers = {"openrouter": failing}
    task = mesh.router.market.start_background_refresh(interval_seconds=3600)
    try:
        assert task is not None
        for _ in range(100):
            if failing.calls:
                break
            await asyncio.sleep(0.01)
        assert failing.calls >= 1

        from harness_mesh.models import TaskRequest

        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="Design the release plan",
                difficulty="high",
                task_kind="plan",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED, result.summary
        assert result.routing is not None
        assert result.routing["chosen"] is None
    finally:
        await mesh.router.market.stop_background_refresh()
        assert mesh.router.market._background_task is None
        mesh.storage.close()


@pytest.mark.asyncio
async def test_background_refresh_is_disabled_offline(tmp_path):
    mesh = make_mesh(tmp_path, env={"HARNESS_MESH_OFFLINE": "1"})
    try:
        assert mesh.router.market.start_background_refresh() is None
    finally:
        mesh.storage.close()


# ---------------------------------------------------------------------------
# axis columns and attribution
# ---------------------------------------------------------------------------


def test_market_show_prints_the_epoch_attribution(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    model = _model("anthropic/claude-opus-5-5", intelligence=88.0)
    model.benchmarks_used = ["gpqa_diamond", "cursorbench_external"]
    model.benchmark_data_date = "2026-10-26"
    _seed_market(mesh, [model])
    try:
        text = _plain(
            _invoke(monkeypatch, lambda: make_mesh(tmp_path), "market", "show").output
        )
        assert (
            "Benchmark data: Epoch AI, 'Capabilities & benchmarking', "
            "https://epoch.ai/benchmarks (CC BY 4.0)" in text
        )
        assert "bench=2" in text
        assert "data=2026-10-26" in text

        payload = json.loads(
            _plain(
                _invoke(monkeypatch, lambda: make_mesh(tmp_path), "market", "show", "--json").output
            )
        )
        assert payload["attribution"].startswith("Benchmark data: Epoch AI")
    finally:
        mesh.storage.close()


def test_market_show_axis_sorts_and_labels_the_selected_axis(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    now = time.time()
    models = [
        _model("vendor/weak", intelligence=60.0),
        _model("vendor/strong", intelligence=70.0),
    ]
    models[0].intelligence_reasoning = 10.0
    models[0].intelligence_coding = 90.0
    models[1].intelligence_reasoning = 80.0
    models[1].intelligence_coding = 20.0
    mesh.storage.save_market_snapshot(
        "artificial_analysis",
        now,
        {"models": [model.model_dump(mode="json") for model in models], "fetched_at": now},
    )
    try:
        def factory():
            return make_mesh(tmp_path)

        payload = json.loads(
            _plain(
                _invoke(
                    monkeypatch, factory, "market", "show", "--axis", "reasoning", "--json"
                ).output
            )
        )
        assert [item["key"] for item in payload["models"]] == [
            "vendor/strong",
            "vendor/weak",
        ]
        assert payload["axis"] == "reasoning"

        text = _plain(
            _invoke(monkeypatch, factory, "market", "show", "--axis", "coding").output
        )
        assert "vendor/weak" in text
        assert "coding= 90.0" in text
    finally:
        mesh.storage.close()


def test_market_show_rejects_an_unknown_axis(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    try:
        result = _invoke(
            monkeypatch,
            lambda: make_mesh(tmp_path),
            "market",
            "show",
            "--axis",
            "speed",
        )
        assert result.exit_code != 0
        assert "reasoning" in _plain(result.output)
    finally:
        mesh.storage.close()


def test_route_json_carries_the_attribution(tmp_path, monkeypatch):
    adapter = FakeAdapter("claude_code", lambda adapter, call: success("should not run"))
    mesh = make_mesh(tmp_path, adapters={"claude_code": adapter})
    _seed_market(mesh, [_model("anthropic/claude-opus-5-5", intelligence=88.0)])
    _seed_models(mesh, [_discovered("claude_code", "claude-opus-5-5")])
    try:
        payload = json.loads(
            _plain(
                _invoke(
                    monkeypatch,
                    lambda: make_mesh(tmp_path, adapters={"claude_code": adapter}),
                    "route",
                    "--objective",
                    "Design the release plan",
                    "--kind",
                    "plan",
                    "--json",
                ).output
            )
        )
        assert payload["routing"]["attribution"].startswith("Benchmark data: Epoch AI")
    finally:
        mesh.storage.close()
