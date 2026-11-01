"""Real-machine MCP environment probe.

Measured on 2026-11-01 with Claude Code 2.1.309, codex-cli
0.166.0-alpha.5.1, DeepSeek Harness 0.2.1-rc.5, Antigravity 1.4.12 and
ZCode 0.17.0: Codex starts MCP servers with only the environment declared in
its MCP configuration, so ``HARNESS_MESH_*`` never reaches the child.  The
other four forward the host environment.  Every harness starts the MCP child
with the worktree as its working directory.

The tests only assert those two facts; the one-shot prompt is deliberately
trivial so a harness that cannot authenticate still exercises MCP startup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import yaml

from harness_mesh.adapters.base import AdapterCall
from harness_mesh.adapters.codex import _to_toml_inline
from harness_mesh.adapters.deepseek import DeepSeekAdapter
from harness_mesh.config import default_config, resolve_harness_binary
from tests.helpers import init_git_repo
from tests.real_machine import (
    REAL_MACHINE_GLOBAL_ENV,
    binary_for,
    global_changes_enabled,
    real_machine_available,
    real_machine_enabled,
    skip_reason,
)

pytestmark = pytest.mark.skipif(
    not real_machine_enabled() or not real_machine_available(),
    reason=skip_reason(),
)

PROBE = Path(__file__).with_name("fixtures") / "mcp_probe_server.py"
TIMEOUT_SECONDS = 240
HOST_LINEAGE = {
    "HARNESS_MESH_CALL_DEPTH": "2",
    "HARNESS_MESH_TRACE_CHAIN": json.dumps(["a", "b"]),
    "HARNESS_MESH_TASK_ID": "probe-task",
    "HARNESS_MESH_ASSIGNMENT_ID": "probe-assignment",
    "HARNESS_MESH_HARNESS": "probe-harness",
}


def _host_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(HOST_LINEAGE)
    return env


def _worktree(tmp_path: Path, label: str) -> Path:
    workspace = tmp_path / label / "workspace"
    init_git_repo(workspace, {"README.md": f"{label} probe workspace\n"})
    worktree = workspace / ".harness-mesh" / "worktrees" / "probe-task"
    init_git_repo(worktree, {"README.md": f"{label} probe worktree\n"})
    return worktree


def _run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int = TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _read_probe(out: Path, name: str, completed: subprocess.CompletedProcess, cmd: list[str]) -> dict:
    path = out / f"{name}.json"
    deadline = time.time() + 30
    while not path.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert path.exists(), (
        f"{name} never started the MCP probe\n"
        f"command: {cmd}\n"
        f"stdout: {completed.stdout[-2000:]}\n"
        f"stderr: {completed.stderr[-2000:]}"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_probe(payload: dict, worktree: Path, *, env_reaches_mcp: bool) -> None:
    assert Path(payload["cwd"]).resolve() == worktree.resolve(), payload["cwd"]
    values = payload["values"]
    if env_reaches_mcp:
        assert values.get("HARNESS_MESH_CALL_DEPTH") == "2", values
        assert json.loads(values.get("HARNESS_MESH_TRACE_CHAIN", "null")) == ["a", "b"], values
        assert values.get("HARNESS_MESH_TASK_ID") == "probe-task", values
    else:
        assert values == {}, values


def test_claude_code_forwards_environment_and_worktree(tmp_path):
    worktree = _worktree(tmp_path, "claude")
    out = tmp_path / "claude" / "out"
    out.mkdir(parents=True)
    config = {
        "mcpServers": {
            "hm_probe": {
                "command": sys.executable,
                "args": [str(PROBE), "claude_code", str(out)],
            }
        }
    }
    config_path = tmp_path / "claude" / "mcp.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    cmd = [
        str(binary_for("claude_code")),
        "-p",
        "Reply with exactly OK.",
        "--output-format",
        "json",
        "--mcp-config",
        str(config_path),
        "--strict-mcp-config",
        "--permission-mode",
        "plan",
    ]
    completed = _run(cmd, worktree, _host_env())
    payload = _read_probe(out, "claude_code", completed, cmd)
    _assert_probe(payload, worktree, env_reaches_mcp=True)


def test_codex_does_not_forward_environment_but_keeps_worktree(tmp_path):
    """Measured fact: codex-cli 0.166.0-alpha.5.1 does not pass HARNESS_MESH_*.

    If a future Codex release starts forwarding the host environment, this
    expectation must be flipped to True; the assertion below is intentionally
    strict so that change is noticed.
    """

    worktree = _worktree(tmp_path, "codex")
    out = tmp_path / "codex" / "out"
    out.mkdir(parents=True)
    server = {"command": sys.executable, "args": [str(PROBE), "codex", str(out)]}
    cmd = [
        str(binary_for("codex")),
        "-c",
        f"mcp_servers.hm_probe={_to_toml_inline(server)}",
        "exec",
        "--skip-git-repo-check",
        "--cd",
        str(worktree),
        "--json",
        "--sandbox",
        "read-only",
        "Reply with exactly OK.",
    ]
    completed = _run(cmd, worktree, _host_env())
    payload = _read_probe(out, "codex", completed, cmd)
    _assert_probe(payload, worktree, env_reaches_mcp=False)


def test_deepseek_forwards_environment_and_worktree(tmp_path):
    worktree = _worktree(tmp_path, "deepseek")
    out = tmp_path / "deepseek" / "out"
    out.mkdir(parents=True)
    config = default_config()
    adapter = DeepSeekAdapter(config, config.harnesses["deepseek"], worktree, env=dict(os.environ))
    call = AdapterCall(
        task_id="probe-task",
        assignment_id=f"probe-{uuid.uuid4().hex[:8]}",
        prompt="probe",
        workdir=worktree,
        model="<cli_active_default>",
        timeout_seconds=TIMEOUT_SECONDS,
    )
    overlay = adapter._prepare_dsh_overlay(call)
    assert overlay is not None
    try:
        entries = yaml.safe_load(overlay.read_text(encoding="utf-8")) or []
        entries.append(
            {
                "insert": [
                    {
                        "id": "hm-probe-mcp",
                        "name": "@deepseek-ai/dsh-mcp-client",
                        "config": {
                            "serverName": "hm_probe",
                            "transport": "stdio",
                            "command": sys.executable,
                            "args": [str(PROBE), "deepseek", str(out)],
                            "env": {},
                        },
                    }
                ]
            }
        )
        overlay.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
        cmd = [
            str(binary_for("deepseek")),
            "--profile",
            "headless",
            "--patch",
            str(overlay),
            "--json",
            "Reply with exactly OK.",
        ]
        completed = _run(cmd, worktree, _host_env())
    finally:
        adapter._cleanup_overlay(call.assignment_id)
    payload = _read_probe(out, "deepseek", completed, cmd)
    _assert_probe(payload, worktree, env_reaches_mcp=True)


def _zcode_binary(tmp_path: Path) -> str:
    config = default_config()
    resolved = resolve_harness_binary(
        config.harnesses["zcode"],
        shutil.which,
        tmp_path / "zcode-state",
        Path.home(),
        allow_system_paths=True,
    )
    assert resolved, "ZCode binary could not be resolved"
    return resolved


@pytest.mark.skipif(
    not global_changes_enabled(),
    reason=f"ZCode writes ~/.zcode/cli/plugins; set {REAL_MACHINE_GLOBAL_ENV}=1 to allow and restore it",
)
def test_zcode_forwards_environment_and_worktree(tmp_path):
    worktree = _worktree(tmp_path, "zcode")
    out = tmp_path / "zcode" / "out"
    out.mkdir(parents=True)
    suffix = uuid.uuid4().hex[:8]
    plugin_name = f"hm-probe-{suffix}"
    market_name = f"hm-probe-market-{suffix}"
    market = tmp_path / "zcode" / "marketplace"
    plugin_dir = market / "plugins" / plugin_name
    (plugin_dir / ".zcode-plugin").mkdir(parents=True)
    (market / "marketplace.json").write_text(
        json.dumps(
            {
                "name": market_name,
                "plugins": [
                    {
                        "name": plugin_name,
                        "source": f"./plugins/{plugin_name}",
                        "version": "0.0.0",
                        "description": "Harness-Mesh MCP environment probe",
                        "category": "developer-tools",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / ".zcode-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": plugin_name,
                "version": "0.0.0",
                "description": "Harness-Mesh MCP environment probe",
                "mcpServers": {
                    "hm_probe": {
                        "command": sys.executable,
                        "args": [str(PROBE), "zcode", str(out)],
                        "env": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    zcode = _zcode_binary(tmp_path)
    global_dir = Path.home() / ".zcode" / "cli" / "plugins"
    snapshots = {
        name: (global_dir / name).read_bytes()
        for name in ("known_marketplaces.json", "installed_plugins.json")
        if (global_dir / name).exists()
    }

    def run_plugin_command(*args: str) -> subprocess.CompletedProcess:
        return _run([zcode, *args], worktree, dict(os.environ))

    added_ok = False
    installed_ok = False
    try:
        added = run_plugin_command("plugins", "marketplace", "add", str(market), "--scope", "project")
        assert added.returncode == 0, added.stderr or added.stdout
        added_ok = True
        installed = run_plugin_command("plugins", "install", f"{plugin_name}@{market_name}", "-s", "project")
        assert installed.returncode == 0, installed.stderr or installed.stdout
        installed_ok = True
        cmd = [
            zcode,
            "-p",
            "Reply with exactly OK.",
            "--cwd",
            str(worktree),
            "--json",
        ]
        completed = _run(cmd, worktree, _host_env())
        payload = _read_probe(out, "zcode", completed, cmd)
        _assert_probe(payload, worktree, env_reaches_mcp=True)
    finally:
        if installed_ok:
            uninstalled = run_plugin_command("plugins", "uninstall", plugin_name, "-s", "project", "--force")
            assert uninstalled.returncode == 0, uninstalled.stderr or uninstalled.stdout
        if added_ok:
            removed = run_plugin_command("plugins", "marketplace", "remove", market_name)
            assert removed.returncode == 0, removed.stderr or removed.stdout
        for leftover in (
            global_dir / "cache" / market_name,
            global_dir / "marketplaces" / market_name,
        ):
            shutil.rmtree(leftover, ignore_errors=True)
        for name, original in snapshots.items():
            current = (global_dir / name).read_bytes()
            assert current == original, f"{name} was not restored to its pre-test content"


@pytest.mark.skipif(
    not global_changes_enabled(),
    reason=f"Antigravity writes a global MCP config; set {REAL_MACHINE_GLOBAL_ENV}=1 to allow and restore it",
)
def test_antigravity_forwards_environment_and_worktree(tmp_path):
    worktree = _worktree(tmp_path, "antigravity")
    out = tmp_path / "antigravity" / "out"
    out.mkdir(parents=True)
    agy = str(binary_for("antigravity"))
    config_path = Path.home() / ".gemini" / "config" / "mcp_config.json"
    original = config_path.read_bytes() if config_path.exists() else None
    server_name = f"hm-probe-{uuid.uuid4().hex[:8]}"
    added_ok = False
    try:
        added = _run(
            [agy, "mcp", "add", server_name, sys.executable, str(PROBE), "antigravity", str(out)],
            worktree,
            dict(os.environ),
        )
        assert added.returncode == 0, added.stderr or added.stdout
        added_ok = True
        cmd = [
            agy,
            "-p",
            "Reply with exactly OK.",
            "--output-format",
            "json",
            "--mode",
            "plan",
        ]
        completed = _run(cmd, worktree, _host_env())
        payload = _read_probe(out, "antigravity", completed, cmd)
        _assert_probe(payload, worktree, env_reaches_mcp=True)
    finally:
        if added_ok:
            removed = _run([agy, "mcp", "remove", server_name], worktree, dict(os.environ))
            assert removed.returncode == 0, removed.stderr or removed.stdout
        if original is not None:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_bytes(original)
        elif config_path.exists():
            config_path.unlink()
        if config_path.exists():
            assert server_name not in config_path.read_text(encoding="utf-8")
