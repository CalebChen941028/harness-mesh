"""Risk classes for MCP tools and the approval levels built from them.

``hmesh install --approve`` writes pre-approval rules into harness configuration
files.  Read tools are safe to approve by default; dispatch tools create tickets
inside an isolated worktree; write tools change the primary workspace or shared
state and stay manual unless the user explicitly asks for ``all``.
"""

from __future__ import annotations

APPROVAL_LEVELS: tuple[str, ...] = ("read", "dispatch", "all", "none")
DEFAULT_APPROVAL_LEVEL = "dispatch"

TOOL_APPROVAL_CLASSES: dict[str, str] = {
    "list_harnesses": "read",
    "discover_harness_models": "read",
    "get_model_market": "read",
    "preview_routing": "read",
    "list_skills_and_plugins": "read",
    "get_task_status": "read",
    "read_task_resource": "read",
    "get_quota_status": "read",
    "get_project_context": "read",
    "dispatch_task": "dispatch",
    "parallel_dispatch": "dispatch",
    "cancel_task": "dispatch",
    "apply_task_patch": "write",
    "report_quota_status": "write",
    "update_project_context": "write",
    "get_integration_status": "read",
    "plan_integration": "read",
    "install_integration": "write",
}

_LEVEL_CLASSES: dict[str, frozenset[str]] = {
    "read": frozenset({"read"}),
    "dispatch": frozenset({"read", "dispatch"}),
    "all": frozenset({"read", "dispatch", "write"}),
    "none": frozenset(),
}


def tools_for_level(level: str) -> list[str]:
    """Return the MCP tools approved at ``level``, in ``TOOL_NAMES`` order."""

    # Imported lazily: ``mcp_server`` imports the installer, which imports
    # this module, so a module-level import would close the cycle.
    from .mcp_server import TOOL_NAMES

    classes = _LEVEL_CLASSES.get(str(level).lower())
    if classes is None:
        raise ValueError(
            f"unknown approval level {level!r}; expected one of: "
            + ", ".join(APPROVAL_LEVELS)
        )
    if classes == _LEVEL_CLASSES["all"]:
        return list(TOOL_NAMES)
    return [name for name in TOOL_NAMES if TOOL_APPROVAL_CLASSES[name] in classes]


__all__ = [
    "APPROVAL_LEVELS",
    "DEFAULT_APPROVAL_LEVEL",
    "TOOL_APPROVAL_CLASSES",
    "tools_for_level",
]
