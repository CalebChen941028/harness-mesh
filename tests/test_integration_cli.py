"""``hmesh integrations`` and the discovery hints in doctor/status."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import yaml
from typer.testing import CliRunner

from harness_mesh.cli import app as cli_app
from tests.helpers import init_git_repo

BINARIES = ("claude", "codex", "dsh", "agy", "zcode")


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _fake_binary(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_config(
    workspace: Path,
    *,
    detected: tuple[str, ...] = ("claude_code", "zcode"),
    extra: str = "",
) -> Path:
    binaries = {
        "claude_code": "claude",
        "codex": "codex",
        "deepseek": "dsh",
        "antigravity": "agy",
        "zcode": "zcode",
    }
    lines = ["state_dir: .harness-mesh", "harnesses:"]
    for harness, name in binaries.items():
        binary = (
            str(_fake_binary(workspace.parent / "bin", name))
            if harness in detected
            else str(workspace.parent / "missing" / name)
        )
        lines.extend(
            [
                f"  {harness}:",
                f"    binary: {binary}",
                "    binary_paths: []",
            ]
        )
        if harness == "zcode":
            lines.extend(
                [
                    "    extra:",
                    f"      macos_electron_bin: {workspace.parent / 'missing' / 'ZCode'}",
                    f"      macos_cjs_entry: {workspace.parent / 'missing' / 'zcode.cjs'}",
                ]
            )
    text = "\n".join(lines) + "\n" + extra
    path = workspace / ".harness-mesh" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _invoke(workspace: Path, *args: str):
    return CliRunner().invoke(
        cli_app,
        ["--workspace", str(workspace), *args],
        env={
            "HOME": str(workspace.parent / "home"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1",
        },
    )


def _workspace(tmp_path: Path, **kwargs) -> Path:
    workspace = tmp_path / "workspace"
    init_git_repo(workspace, {"README.md": "integration cli\n"})
    (tmp_path / "home").mkdir(exist_ok=True)
    _write_config(workspace, **kwargs)
    return workspace


def _statuses(payload: dict) -> dict[str, dict]:
    return {item["harness"]: item for item in payload["harnesses"]}


# ----------------------------------------------------------------------
# listing
# ----------------------------------------------------------------------
def test_integrations_json_lists_statuses_and_hints(tmp_path):
    workspace = _workspace(tmp_path)
    result = _invoke(workspace, "integrations", "--json")
    output = _plain(result.output)
    assert result.exit_code == 0, output
    payload = json.loads(output)
    assert set(payload) == {"harnesses"}
    statuses = _statuses(payload)
    assert statuses["claude_code"]["status"] == "detected_not_integrated"
    assert statuses["zcode"]["status"] == "detected_not_integrated"
    assert statuses["codex"]["status"] == "not_detected"
    assert set(statuses["claude_code"]) == {
        "harness",
        "display_name",
        "status",
        "detected",
        "registered",
        "ignored",
        "binary_path",
        "detail",
        "registration_files",
    }


def test_integrations_text_lists_state_and_next_command(tmp_path):
    workspace = _workspace(tmp_path)
    result = _invoke(workspace, "integrations")
    output = _plain(result.output)
    assert result.exit_code == 0, output
    assert "claude_code" in output
    assert "detected_not_integrated" in output
    assert "not_detected" in output
    assert "run `hmesh install --harness claude_code`" in output


# ----------------------------------------------------------------------
# ignore / unignore
# ----------------------------------------------------------------------
def test_ignore_and_unignore_round_trip(tmp_path):
    workspace = _workspace(tmp_path)
    config_path = workspace / ".harness-mesh" / "config.yaml"

    ignored = _invoke(workspace, "integrations", "ignore", "zcode")
    assert ignored.exit_code == 0, _plain(ignored.output)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["integration"]["ignore"] == ["zcode"]
    assert raw["harnesses"]["zcode"]["binary"]
    assert "zcode" in _plain(ignored.output)

    payload = json.loads(_plain(_invoke(workspace, "integrations", "--json").output))
    assert _statuses(payload)["zcode"]["status"] == "ignored"
    assert _statuses(payload)["zcode"]["ignored"] is True

    restored = _invoke(workspace, "integrations", "unignore", "zcode")
    assert restored.exit_code == 0, _plain(restored.output)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "integration" not in raw or not raw["integration"].get("ignore")
    payload = json.loads(_plain(_invoke(workspace, "integrations", "--json").output))
    assert _statuses(payload)["zcode"]["status"] == "detected_not_integrated"


def test_ignore_preserves_unrelated_configuration(tmp_path):
    workspace = _workspace(tmp_path, extra="summary_max_chars: 1234\n")
    config_path = workspace / ".harness-mesh" / "config.yaml"
    before = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    result = _invoke(workspace, "integrations", "ignore", "claude_code")
    assert result.exit_code == 0, _plain(result.output)
    after = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert after["summary_max_chars"] == 1234
    assert after["harnesses"] == before["harnesses"]
    assert after["integration"]["ignore"] == ["claude_code"]


def test_ignore_is_idempotent_and_json_has_a_stable_shape(tmp_path):
    workspace = _workspace(tmp_path)
    for _ in range(2):
        result = _invoke(workspace, "integrations", "ignore", "zcode", "--json")
        assert result.exit_code == 0, _plain(result.output)
    payload = json.loads(_plain(result.output))
    assert payload["harness"] == "zcode"
    assert payload["ignored"] is True
    assert payload["config_path"] == str(workspace / ".harness-mesh" / "config.yaml")
    raw = yaml.safe_load(
        (workspace / ".harness-mesh" / "config.yaml").read_text(encoding="utf-8")
    )
    assert raw["integration"]["ignore"] == ["zcode"]


def test_ignore_rejects_an_unknown_harness(tmp_path):
    workspace = _workspace(tmp_path)
    config_path = workspace / ".harness-mesh" / "config.yaml"
    before = config_path.read_bytes()
    result = _invoke(workspace, "integrations", "ignore", "unknown")
    output = _plain(result.output)
    assert result.exit_code == 2
    assert "unknown" in output.lower()
    assert "claude_code" in output
    assert config_path.read_bytes() == before


def test_integrations_rejects_a_missing_name_and_unknown_action(tmp_path):
    workspace = _workspace(tmp_path)
    missing = _invoke(workspace, "integrations", "ignore")
    assert missing.exit_code == 2
    assert "NAME" in _plain(missing.output) or "name" in _plain(missing.output)
    unknown = _invoke(workspace, "integrations", "forget", "zcode")
    assert unknown.exit_code == 2


# ----------------------------------------------------------------------
# doctor and status hints
# ----------------------------------------------------------------------
def test_doctor_prints_one_hint_with_a_command(tmp_path):
    workspace = _workspace(tmp_path)
    result = _invoke(workspace, "doctor")
    output = _plain(result.output)
    assert result.exit_code == 0, output
    assert output.rstrip().splitlines()[-1].startswith("integration:")
    assert "Claude Code" in output
    assert "ZCode" in output
    assert "hmesh install --harness claude_code --harness zcode" in output


def test_doctor_hint_disappears_when_the_harness_is_ignored(tmp_path):
    workspace = _workspace(tmp_path, detected=("claude_code",))
    ignored = _invoke(workspace, "integrations", "ignore", "claude_code")
    assert ignored.exit_code == 0, _plain(ignored.output)
    output = _plain(_invoke(workspace, "doctor").output)
    assert "integration:" not in output


def test_status_prints_the_same_hint_without_writing_config(tmp_path):
    workspace = _workspace(tmp_path)
    config_path = workspace / ".harness-mesh" / "config.yaml"
    before = config_path.read_bytes()
    result = _invoke(workspace, "status")
    output = _plain(result.output)
    assert result.exit_code == 0, output
    assert "integration:" in output
    assert "hmesh install --harness claude_code" in output
    assert config_path.read_bytes() == before


def test_status_hint_disappears_when_integrated(tmp_path):
    workspace = _workspace(tmp_path, detected=("claude_code",))
    (tmp_path / "home" / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "harness-mesh": {
                        "command": os.sys.executable,
                        "args": ["-m", "harness_mesh.mcp_server"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    output = _plain(_invoke(workspace, "status").output)
    assert "integration:" not in output
