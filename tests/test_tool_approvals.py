"""Approval classes for every MCP tool, and the four install levels."""

from __future__ import annotations

import pytest

from harness_mesh.approvals import TOOL_APPROVAL_CLASSES, tools_for_level
from harness_mesh.mcp_server import TOOL_NAMES


def test_every_tool_belongs_to_exactly_one_class():
    assert set(TOOL_APPROVAL_CLASSES) == set(TOOL_NAMES)
    assert len(TOOL_APPROVAL_CLASSES) == len(TOOL_NAMES)
    assert set(TOOL_APPROVAL_CLASSES.values()) == {"read", "dispatch", "write"}


def test_read_level_lists_only_read_tools():
    assert tools_for_level("read") == [
        "list_harnesses",
        "discover_harness_models",
        "get_model_market",
        "preview_routing",
        "list_skills_and_plugins",
        "get_task_status",
        "read_task_resource",
        "get_quota_status",
        "get_project_context",
        "get_integration_status",
        "plan_integration",
    ]


def test_dispatch_level_adds_dispatch_tools_to_read_tools():
    level = tools_for_level("dispatch")
    assert set(level) == set(tools_for_level("read")) | {
        "dispatch_task",
        "parallel_dispatch",
        "cancel_task",
    }
    assert level == [name for name in TOOL_NAMES if name in set(level)]
    assert "apply_task_patch" not in level
    assert "report_quota_status" not in level
    assert "update_project_context" not in level


def test_all_level_equals_the_mcp_tool_list():
    assert tools_for_level("all") == TOOL_NAMES


def test_none_level_approves_nothing():
    assert tools_for_level("none") == []


def test_unknown_level_is_rejected():
    with pytest.raises(ValueError, match="read.*dispatch.*all.*none"):
        tools_for_level("write")
