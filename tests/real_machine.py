"""Opt-in gate for tests that launch real, quota-consuming harness CLIs.

``pytest -q`` must behave identically on a developer laptop and in CI, so the
real-machine suites only run when ``HARNESS_MESH_REAL_MACHINE=1`` is set.
Suites that mutate a harness' global configuration additionally require
``HARNESS_MESH_REAL_MACHINE_GLOBAL=1``.
"""

from __future__ import annotations

import glob
import os
import shutil
import sys
from pathlib import Path

REAL_MACHINE_ENV = "HARNESS_MESH_REAL_MACHINE"
REAL_MACHINE_GLOBAL_ENV = "HARNESS_MESH_REAL_MACHINE_GLOBAL"

REAL_PATHS = {
    "claude_code": "~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude",
    "codex": "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    "deepseek": "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh",
    "antigravity": "/Applications/Gemini.app/Contents/Helpers/agy",
    "zcode": "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
}


def real_machine_enabled() -> bool:
    return os.environ.get(REAL_MACHINE_ENV) == "1"


def global_changes_enabled() -> bool:
    return os.environ.get(REAL_MACHINE_GLOBAL_ENV) == "1"


def real_machine_available() -> bool:
    if sys.platform != "darwin":
        return False
    for _, pattern in REAL_PATHS.items():
        expanded = os.path.expanduser(pattern)
        if glob.glob(expanded) if "*" in expanded else Path(expanded).exists():
            continue
        return False
    return True


def skip_reason() -> str:
    if not real_machine_enabled():
        return f"real-machine tests are opt-in; set {REAL_MACHINE_ENV}=1"
    return "real Claude/Codex/DSH/Antigravity/ZCode installations not found"


def binary_for(harness: str) -> Path:
    """Resolve a harness binary without touching any global configuration."""

    names = {
        "claude_code": "claude",
        "codex": "codex",
        "deepseek": "dsh",
        "antigravity": "agy",
        "zcode": "zcode",
    }
    found = shutil.which(names[harness])
    if found:
        return Path(found)
    matches = sorted(glob.glob(os.path.expanduser(REAL_PATHS[harness])))
    if not matches:
        raise FileNotFoundError(f"{harness} binary not found")
    return Path(matches[-1])
