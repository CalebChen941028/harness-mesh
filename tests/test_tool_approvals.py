"""Approval classes for every MCP tool, and the four install levels."""

from __future__ import annotations

import pytest

from harness_mesh.approvals import (
    ALWAYS_MANUAL,
    TOOL_APPROVAL_CLASSES,
    tools_for_level,
)
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


def test_always_manual_tools_are_write_tools():
    assert ALWAYS_MANUAL == {"apply_task_patch", "install_integration"}
    assert set(ALWAYS_MANUAL) <= set(TOOL_APPROVAL_CLASSES)
    assert all(TOOL_APPROVAL_CLASSES[name] == "write" for name in ALWAYS_MANUAL)


def test_all_level_never_pre_approves_always_manual_tools():
    level = tools_for_level("all")
    assert level == [name for name in TOOL_NAMES if name not in ALWAYS_MANUAL]
    assert set(ALWAYS_MANUAL).isdisjoint(level)
    # The remaining write tools are still covered by the explicit opt-in.
    assert "report_quota_status" in level
    assert "update_project_context" in level
    # ``all`` still starts from the read + dispatch coverage.
    assert set(tools_for_level("dispatch")) <= set(level)


def test_none_level_approves_nothing():
    assert tools_for_level("none") == []


def test_unknown_level_is_rejected():
    with pytest.raises(ValueError, match="read.*dispatch.*all.*none"):
        tools_for_level("write")
