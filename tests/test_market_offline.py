"""The default suite must never touch the network, including child processes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.helpers import init_git_repo

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_MESH_REAL_MACHINE") == "1",
    reason="the real-machine suite deliberately leaves the offline switch unset",
)


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_default_suite_sets_the_offline_switch():
    assert os.environ.get("HARNESS_MESH_OFFLINE") == "1"


def test_market_refresh_subprocess_stays_offline(tmp_path):
    workspace = tmp_path / "workspace"
    init_git_repo(workspace, {"README.md": "offline market refresh\n"})
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["HOME"] = str(tmp_path / "home")
    assert env.get("HARNESS_MESH_OFFLINE") == "1"

    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness_mesh.cli",
            "--workspace",
            str(workspace),
            "market",
            "refresh",
            "--json",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(_plain(completed.stdout))
    statuses = {status["source"]: status["status"] for status in payload["statuses"]}
    assert statuses
    assert set(statuses.values()) <= {"skipped", "offline"}, statuses
    assert elapsed < 3.0, f"offline market refresh took {elapsed:.2f}s"
