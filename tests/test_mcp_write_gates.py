"""Server-side write gates for the MCP tool surface (audit H-08, CR-02).

These tests exercise the MCP server process itself, not the harness-side
pre-approval rules: ``HARNESS_MESH_READ_ONLY=1`` must refuse every write tool,
a caller resolved to a worker assignment must be refused the controller-only
subset, and model discovery payloads must be projected through
``DiscoveredModel.public_view``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from harness_mesh.mcp_server import create_server
from tests.helpers import make_mesh


async def _call(server, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await server.call_tool(name, arguments or {})
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


WRITE_TOOL_CALLS: dict[str, dict[str, Any]] = {
    "apply_task_patch": {"task_id": "task_missing"},
    "update_project_context": {"key": "wsd.gate", "content": "should not persist"},
    "report_quota_status": {"harness": "claude_code", "model": "*", "action": "open"},
    "cancel_task": {"task_id": "task_missing"},
    "install_integration": {
        "harness": "claude_code",
        "approve_level": "read",
        "plan_id": "0" * 16,
    },
}

CONTROLLER_ONLY_WRITES = (
    "apply_task_patch",
    "install_integration",
    "cancel_task",
    "report_quota_status",
)


@pytest.mark.parametrize("tool_name", sorted(WRITE_TOOL_CALLS))
async def test_read_only_mode_refuses_every_write_tool(tmp_path, monkeypatch, tool_name):
    mesh = make_mesh(tmp_path)
    monkeypatch.setenv("HARNESS_MESH_READ_ONLY", "1")
    try:
        server = create_server(mesh.router)
        result = await _call(server, tool_name, WRITE_TOOL_CALLS[tool_name])
        assert result.get("refused") is True, result
        assert "read-only" in result["error"]
    finally:
        mesh.storage.close()


async def test_read_only_mode_does_not_persist_project_context(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    monkeypatch.setenv("HARNESS_MESH_READ_ONLY", "1")
    try:
        server = create_server(mesh.router)
        result = await _call(
            server,
            "update_project_context",
            {"key": "wsd.readonly", "content": "nope"},
        )
        assert result["refused"] is True
        assert mesh.storage.list_project_context() == []
    finally:
        mesh.storage.close()


@pytest.mark.parametrize("tool_name", CONTROLLER_ONLY_WRITES)
async def test_worker_is_refused_controller_only_writes(tmp_path, monkeypatch, tool_name):
    mesh = make_mesh(tmp_path)
    monkeypatch.setenv("HARNESS_MESH_TASK_ID", "task_worker01")
    try:
        server = create_server(mesh.router)
        result = await _call(server, tool_name, WRITE_TOOL_CALLS[tool_name])
        assert result.get("refused") is True, result
        assert "worker" in result["error"].lower()
        assert "task_worker01" in result["error"]
    finally:
        mesh.storage.close()


async def test_worker_may_write_context_but_only_as_unverified(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    monkeypatch.setenv("HARNESS_MESH_TASK_ID", "task_worker01")
    try:
        server = create_server(mesh.router)
        result = await _call(
            server,
            "update_project_context",
            {"key": "wsd.worker", "content": "worker observation"},
        )
        assert result.get("source") == "worker", result
        entries = mesh.storage.list_project_context()
        assert [entry["source"] for entry in entries] == ["worker"]
        memory = mesh.router.context.format_memory()
        assert "<project_unverified_memory>" in memory
        assert "<project_shared_memory>" not in memory
    finally:
        mesh.storage.close()


async def test_controller_is_not_refused_by_the_write_gate(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        server = create_server(mesh.router)
        context = await _call(
            server,
            "update_project_context",
            {"key": "wsd.controller", "content": "controller fact"},
        )
        assert context.get("refused") is None
        assert context["source"] == "controller"

        quota = await _call(
            server,
            "report_quota_status",
            {"harness": "claude_code", "model": "*", "action": "open"},
        )
        assert quota.get("refused") is None
        assert quota["action"] == "open"

        # A write tool that fails for a non-authorisation reason must not be
        # reported as refused: the gate is about caller identity, not validity.
        missing = await _call(server, "cancel_task", {"task_id": "task_missing"})
        assert "refused" not in missing
        assert "error" in missing
    finally:
        mesh.storage.close()


async def test_discover_harness_models_projects_provider_through_public_view(tmp_path):
    mesh = make_mesh(tmp_path)
    leaked = "{'zcode': {'options': {'apiKey': 'sk-zcode-secret'}}}"

    async def fake_discover(harness=None, force_refresh=True):
        return {
            "harness": "zcode",
            "installed": True,
            "models": [
                {
                    "model_id": "glm-5",
                    "provider": leaked,
                    "authorized": True,
                    "capability_score": 80.0,
                    "source": "local_config",
                }
            ],
            "tier_models": {"high": "glm-5"},
        }

    mesh.router.discover_harness_models = fake_discover  # type: ignore[method-assign]
    try:
        server = create_server(mesh.router)
        payload = await _call(server, "discover_harness_models", {"harness": "zcode"})
        text = json.dumps(payload)
        assert "apiKey" not in text
        assert "sk-zcode-secret" not in text
        model = payload["models"][0]
        # public_view falls back to the harness name for enum-shaped providers.
        assert model["provider"] == "zcode"
        assert set(model) <= {
            "model_id",
            "provider",
            "authorized",
            "capability_score",
            "intelligence",
            "intelligence_source",
            "blended_price",
            "assigned_tiers",
            "source",
            "context_window",
            "supports_reasoning",
            "is_active_default",
        }
    finally:
        mesh.storage.close()


async def test_discover_all_projects_each_harness_payload(tmp_path):
    mesh = make_mesh(tmp_path)
    leaked = "{'options': {'jwt': 'eyJhbGciOiJIUzI1NiJ9.secret'}}"

    async def fake_discover(harness=None, force_refresh=True):
        return {
            "zcode": {
                "harness": "zcode",
                "installed": True,
                "models": [
                    {
                        "model_id": "glm-5",
                        "provider": leaked,
                        "authorized": True,
                        "source": "local_config",
                    }
                ],
                "tier_models": {},
            }
        }

    mesh.router.discover_harness_models = fake_discover  # type: ignore[method-assign]
    try:
        server = create_server(mesh.router)
        payload = await _call(server, "discover_harness_models", {})
        text = json.dumps(payload)
        assert leaked not in text
        model = payload["harnesses"]["zcode"]["models"][0]
        assert model["provider"] == "zcode"
    finally:
        mesh.storage.close()
