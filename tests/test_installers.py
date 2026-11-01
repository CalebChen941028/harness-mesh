"""One-command installer coverage for all supported desktop harnesses."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from harness_mesh.config import default_config
from harness_mesh.installer import DSH_ENTRY_ID, HarnessInstaller


def _fake_binary(directory: Path, name: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _installer(tmp_path: Path, *, runner=None, dry_run: bool = False):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    workspace.mkdir()
    desktop_patch = home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
    desktop_patch.parent.mkdir(parents=True)
    desktop_patch.write_text(
        "# user overrides\n"
        "- id: keep-me\n"
        "  name: '@example/keep-me'\n",
        encoding="utf-8",
    )
    headless_patch = home / ".dsh" / "profiles" / "headless" / "cordis.patch.yml"
    headless_patch.parent.mkdir(parents=True)
    headless_patch.write_text(
        "# Your patch layer for this dsh profile:\n# a top-level YAML array of loader patch entries.\n[]\n",
        encoding="utf-8",
    )

    config = default_config()
    binaries = {
        "claude_code": _fake_binary(bin_dir, "claude"),
        "codex": _fake_binary(bin_dir, "codex"),
        "deepseek": _fake_binary(bin_dir, "dsh"),
        "antigravity": _fake_binary(bin_dir, "agy"),
        "zcode": _fake_binary(bin_dir, "zcode"),
    }
    for harness, binary in binaries.items():
        config.harnesses[harness].binary = binary
        config.harnesses[harness].binary_paths = []

    calls: list[list[str]] = []

    def fake_runner(argv, cwd, env):
        calls.append(list(argv))
        return 0, "", ""

    installer = HarnessInstaller(
        config,
        workspace,
        home_dir=home,
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        which=lambda name: None,
        runner=runner or fake_runner,
        config_path=tmp_path / "missing-config.yaml",
        dry_run=dry_run,
    )
    return installer, home, workspace, calls


def test_install_all_harnesses_is_idempotent(tmp_path):
    installer, home, workspace, calls = _installer(tmp_path)
    first = installer.install()
    assert {result.harness: result.action for result in first} == {
        "claude_code": "installed",
        "codex": "installed",
        "deepseek": "installed",
        "antigravity": "installed",
        "zcode": "installed",
    }

    claude_calls = [call for call in calls if call[0].endswith("/claude") and "mcp" in call]
    codex_calls = [call for call in calls if call[0].endswith("/codex") and "mcp" in call]
    agy_calls = [call for call in calls if call[0].endswith("/agy") and "mcp" in call]
    zcode_calls = [call for call in calls if call[0].endswith("/zcode") and "plugins" in call]

    assert any("add-json" in call for call in claude_calls)
    claude_add = next(call for call in claude_calls if "add-json" in call)
    payload = json.loads(claude_add[claude_add.index("harness-mesh") + 1])
    assert payload["type"] == "stdio"
    assert payload["args"] == ["-m", "harness_mesh.mcp_server"]
    assert payload["env"]["HARNESS_MESH_WORKSPACE"] == str(workspace.resolve())
    assert any("--env" in call for call in codex_calls)
    assert any("--env" in call for call in agy_calls)
    assert any("marketplace" in call and "add" in call for call in zcode_calls)
    assert any("install" in call and "harness-mesh@harness-mesh-local" in call for call in zcode_calls)

    market = workspace / ".harness-mesh" / "zcode-plugin" / "marketplace.json"
    manifest = (
        workspace
        / ".harness-mesh"
        / "zcode-plugin"
        / "plugins"
        / "harness-mesh"
        / ".zcode-plugin"
        / "plugin.json"
    )
    assert market.exists() and manifest.exists()
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    server = manifest_data["mcpServers"]["harness-mesh"]
    assert server["args"] == ["-m", "harness_mesh.mcp_server"]
    assert server["env"]["HARNESS_MESH_WORKSPACE"] == str(workspace.resolve())

    desktop_patch = home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
    desktop_text = desktop_patch.read_text(encoding="utf-8")
    assert desktop_text.count("id: mcp-harness-mesh") == 1
    assert "@deepseek-ai/dsh-mcp-client" in desktop_text
    assert "keep-me" in desktop_text
    parsed = yaml.safe_load(desktop_text)
    assert isinstance(parsed, list)
    inserted_rows = [
        row
        for item in parsed
        if isinstance(item, dict)
        for row in item.get("insert", [])
        if isinstance(row, dict)
    ]
    assert any(row.get("id") == DSH_ENTRY_ID for row in inserted_rows)
    entry = next(row for row in inserted_rows if row.get("id") == DSH_ENTRY_ID)
    assert entry["config"]["serverName"] == "harness_mesh"
    assert "python" in entry["config"]["command"]

    second = installer.install()
    assert all(result.action == "installed" for result in second)
    assert desktop_patch.read_text(encoding="utf-8").count("id: mcp-harness-mesh") == 1


def test_install_dry_run_does_not_touch_files_or_run_commands(tmp_path):
    installer, home, workspace, calls = _installer(tmp_path, dry_run=True)
    before = (home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml").read_text(encoding="utf-8")
    results = installer.install()
    assert all(result.action == "would_install" for result in results)
    assert calls == []
    assert not (workspace / ".harness-mesh").exists()
    assert (home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml").read_text(encoding="utf-8") == before


def test_uninstall_removes_entries_and_commands(tmp_path):
    installer, home, workspace, calls = _installer(tmp_path)
    installer.install()
    calls.clear()
    results = installer.install(uninstall=True)
    assert {result.harness: result.action for result in results} == {
        "claude_code": "uninstalled",
        "codex": "uninstalled",
        "deepseek": "uninstalled",
        "antigravity": "uninstalled",
        "zcode": "uninstalled",
    }
    desktop_text = (home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml").read_text(encoding="utf-8")
    assert "id: mcp-harness-mesh" not in desktop_text
    assert "keep-me" in desktop_text
    headless_text = (home / ".dsh" / "profiles" / "headless" / "cordis.patch.yml").read_text(encoding="utf-8")
    assert "mcp-harness-mesh" not in headless_text
    assert any("remove" in call for call in calls)
    assert any("plugins" in call and "uninstall" in call for call in calls)
    assert not (workspace / ".harness-mesh" / "zcode-plugin").exists()


def test_zcode_without_cli_leaves_marketplace_ready_for_ui(tmp_path):
    installer, home, workspace, calls = _installer(tmp_path)
    zcode = installer.config.harnesses["zcode"]
    zcode.binary = "/does/not/exist/zcode"
    # The app is detected through these paths; make them exist so the test does
    # not depend on whether ZCode is installed on the machine running it.
    fake_app = tmp_path / "ZCode.app"
    fake_app.mkdir()
    zcode.extra["macos_electron_bin"] = str(fake_app)
    zcode.extra["macos_cjs_entry"] = str(fake_app / "zcode.cjs")
    results = installer.install(["zcode"])
    assert results[0].action == "ready"
    assert calls == []
    assert (workspace / ".harness-mesh" / "zcode-plugin" / "marketplace.json").exists()


def test_zcode_is_skipped_when_neither_cli_nor_app_exists(tmp_path):
    installer, home, workspace, calls = _installer(tmp_path)
    zcode = installer.config.harnesses["zcode"]
    zcode.binary = "/does/not/exist/zcode"
    zcode.extra["macos_electron_bin"] = str(tmp_path / "missing" / "ZCode")
    zcode.extra["macos_cjs_entry"] = str(tmp_path / "missing" / "zcode.cjs")
    results = installer.install(["zcode"])
    assert results[0].action == "skipped"
    assert calls == []
    assert not (workspace / ".harness-mesh" / "zcode-plugin").exists()

