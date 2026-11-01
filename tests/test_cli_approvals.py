"""``hmesh install --approve`` and the doctor approvals column."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from typer.testing import CliRunner

from harness_mesh.cli import app as cli_app
from tests.helpers import init_git_repo


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _fake_binaries(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("claude", "codex", "agy"):
        path = directory / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    return directory


def _invoke(tmp_path: Path, *args: str, home: Path | None = None):
    workspace = tmp_path / "workspace"
    if not workspace.exists():
        init_git_repo(workspace, {"README.md": "cli approvals\n"})
    bin_dir = _fake_binaries(tmp_path / "bin")
    config = Path(__file__).resolve().parents[1] / "config" / "harnesses.example.yaml"
    env = {
        "HOME": str(home or tmp_path / "home"),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1",
    }
    return CliRunner().invoke(
        cli_app,
        ["--workspace", str(workspace), "--config", str(config), *args],
        env=env,
    )


def _claude_allow(home: Path) -> list[str]:
    payload = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    return payload["permissions"]["allow"]


def test_install_approve_read_writes_seven_rules(tmp_path):
    result = _invoke(
        tmp_path,
        "install",
        "--harness",
        "claude_code",
        "--approve",
        "read",
        "--yes",
    )
    output = _plain(result.output)
    assert result.exit_code == 0, output
    assert "approvals: level=read tools=7 status=applied" in output
    allow = _claude_allow(tmp_path / "home")
    assert len([entry for entry in allow if entry.startswith("mcp__harness-mesh__")]) == 7


def test_install_approve_all_writes_thirteen_rules_and_json(tmp_path):
    result = _invoke(
        tmp_path,
        "install",
        "--harness",
        "claude_code",
        "--approve",
        "all",
        "--yes",
        "--json",
    )
    assert result.exit_code == 0, _plain(result.output)
    payload = json.loads(result.output)
    approval = payload[0]["approvals"]
    assert approval["level"] == "all"
    assert approval["status"] == "applied"
    assert len(approval["tools"]) == 13
    assert approval["files"] == [str(tmp_path / "home" / ".claude" / "settings.json")]
    assert len([e for e in _claude_allow(tmp_path / "home") if e.startswith("mcp__harness-mesh__")]) == 13


def test_install_approve_none_clears_previous_rules(tmp_path):
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls)", "mcp__harness-mesh__list_harnesses"]}}),
        encoding="utf-8",
    )
    result = _invoke(
        tmp_path,
        "install",
        "--harness",
        "claude_code",
        "--approve",
        "none",
        "--yes",
        home=home,
    )
    assert result.exit_code == 0, _plain(result.output)
    allow = _claude_allow(home)
    assert allow == ["Bash(ls)"]


def test_install_rejects_an_unknown_approval_level(tmp_path):
    result = _invoke(tmp_path, "install", "--approve", "everything", "--yes")
    output = _plain(result.output)
    assert result.exit_code == 2
    assert "--approve" in output
    assert "read" in output and "dispatch" in output and "all" in output and "none" in output


def test_install_dry_run_lists_approval_count_entries_and_file(tmp_path):
    result = _invoke(
        tmp_path,
        "install",
        "--harness",
        "claude_code",
        "--approve",
        "dispatch",
        "--dry-run",
    )
    output = _plain(result.output)
    assert result.exit_code == 0, output
    assert "approvals: level=dispatch tools=10 status=planned" in output
    assert "mcp__harness-mesh__dispatch_task" in output
    assert str(tmp_path / "home" / ".claude" / "settings.json") in output
    assert not (tmp_path / "home" / ".claude" / "settings.json").exists()


def test_doctor_reports_approved_and_missing_tools(tmp_path):
    installed = _invoke(
        tmp_path,
        "install",
        "--harness",
        "claude_code",
        "--approve",
        "read",
        "--yes",
    )
    assert installed.exit_code == 0, _plain(installed.output)
    result = _invoke(tmp_path, "doctor", "--harness", "claude_code")
    output = _plain(result.output)
    assert result.exit_code == 0, output
    assert "approvals: 7/10 approved" in output
    assert "missing: dispatch_task, parallel_dispatch, cancel_task" in output


def test_doctor_json_contains_the_approval_report(tmp_path):
    result = _invoke(tmp_path, "doctor", "--harness", "claude_code", "--json")
    assert result.exit_code == 0, _plain(result.output)
    payload = json.loads(result.output)
    approval = payload[0]["approvals"]
    assert approval["level"] == "dispatch"
    assert approval["required"] == 10
    assert approval["approved"] == 0
    assert len(approval["missing"]) == 10
    assert not (tmp_path / "home" / ".claude" / "settings.json").exists()


def test_doctor_counts_coverage_against_the_default_level(tmp_path):
    installed = _invoke(
        tmp_path,
        "install",
        "--harness",
        "claude_code",
        "--approve",
        "all",
        "--yes",
    )
    assert installed.exit_code == 0, _plain(installed.output)
    result = _invoke(tmp_path, "doctor", "--harness", "claude_code")
    output = _plain(result.output)
    assert result.exit_code == 0, output
    assert "approvals: 10/10 approved" in output
    assert "missing: none" in output
