"""Audit WP4: inherited recursion state cannot be reset by tool arguments."""

from __future__ import annotations

import json

import pytest

from harness_mesh.models import TaskStatus
from tests.helpers import make_mesh


def _payload(result) -> dict:
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


@pytest.mark.asyncio
async def test_dispatch_cannot_lower_inherited_depth(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    try:
        mesh.config.max_call_depth = 3
        monkeypatch.setenv("HARNESS_MESH_CALL_DEPTH", "3")
        server = __import__("harness_mesh.mcp_server", fromlist=["create_server"]).create_server(mesh.router)
        result = await server.call_tool(
            "dispatch_task",
            {
                "target_harness": "claude_code",
                "objective": "attempt to reset depth",
                "call_depth": 0,
            },
        )
        payload = _payload(result)
        assert payload["status"] == TaskStatus.REJECTED.value
        assert "call_depth=3" in payload["error_detail"]
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_dispatch_merges_trace_chain_and_keeps_ancestors(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    try:
        monkeypatch.setenv("HARNESS_MESH_TRACE_CHAIN", json.dumps(["claude_code"]))
        server = __import__("harness_mesh.mcp_server", fromlist=["create_server"]).create_server(mesh.router)
        result = await server.call_tool(
            "dispatch_task",
            {
                "target_harness": "claude_code",
                "objective": "attempt to erase trace chain",
                "trace_chain": [],
            },
        )
        payload = _payload(result)
        assert payload["status"] == TaskStatus.REJECTED.value
        assert "recursive harness cycle" in payload["error_detail"]
        assert "claude_code" in payload["error_detail"]
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_parallel_dispatch_children_cannot_reset_depth(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    try:
        mesh.config.max_call_depth = 3
        monkeypatch.setenv("HARNESS_MESH_CALL_DEPTH", "3")
        server = __import__("harness_mesh.mcp_server", fromlist=["create_server"]).create_server(mesh.router)
        result = await server.call_tool(
            "parallel_dispatch",
            {
                "tasks": [
                    {
                        "target_harness": "claude_code",
                        "objective": "child resets depth",
                        "call_depth": 0,
                    }
                ]
            },
        )
        payload = _payload(result)
        assert payload["results"][0]["status"] == TaskStatus.REJECTED.value
        assert "call_depth=3" in payload["results"][0]["error_detail"]
    finally:
        mesh.storage.close()


def test_effective_depth_and_trace_helpers(monkeypatch):
    from harness_mesh.mcp_server import _effective_call_depth, _merge_trace_chain

    monkeypatch.setenv("HARNESS_MESH_CALL_DEPTH", "2")
    monkeypatch.setenv("HARNESS_MESH_TRACE_CHAIN", json.dumps(["a", "b"]))
    assert _effective_call_depth(None) == 2
    assert _effective_call_depth(0) == 2
    assert _effective_call_depth(5) == 5
    assert _merge_trace_chain(None) == ["a", "b"]
    assert _merge_trace_chain(["b", "c"]) == ["a", "b", "c"]
    monkeypatch.delenv("HARNESS_MESH_CALL_DEPTH")
    monkeypatch.delenv("HARNESS_MESH_TRACE_CHAIN")
    assert _effective_call_depth(None) == 0
    assert _merge_trace_chain(["a"]) == ["a"]
