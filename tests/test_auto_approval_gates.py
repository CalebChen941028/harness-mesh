"""Auto-approval gates and built-in runtime environment scrubbing."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_mesh.adapters.antigravity import AntigravityAdapter
from harness_mesh.adapters.base import AdapterCall
from harness_mesh.adapters.claude_code import ClaudeCodeAdapter
from harness_mesh.adapters.codex import CodexAdapter
from harness_mesh.adapters.deepseek import DeepSeekAdapter, _BuiltinDeepSeekRuntime
from harness_mesh.adapters.zcode import ZCodeAdapter
from harness_mesh.config import default_config


def _call(tmp_path: Path, **overrides) -> AdapterCall:
    values = dict(
        task_id="task-approval",
        assignment_id="asg-approval",
        prompt="do work",
        workdir=tmp_path,
        model="demo-model",
    )
    values.update(overrides)
    return AdapterCall(**values)


def _adapter(adapter_type, tmp_path: Path, config=None):
    config = config or default_config()
    harness = {
        ClaudeCodeAdapter: "claude_code",
        CodexAdapter: "codex",
        AntigravityAdapter: "antigravity",
        ZCodeAdapter: "zcode",
        DeepSeekAdapter: "deepseek",
    }[adapter_type]
    return adapter_type(config, config.harnesses[harness], tmp_path, env={}, which=lambda name: None)


def test_shared_write_suppresses_auto_approval_by_default(tmp_path):
    config = default_config()
    config.harnesses["claude_code"].auto_approve = True
    adapter = _adapter(ClaudeCodeAdapter, tmp_path, config)
    call = _call(tmp_path, shared_write=True)
    args = adapter._augment_command(["claude", "-p", "prompt"], call)
    assert "--dangerously-skip-permissions" not in args

    config.auto_approve_shared_write = True
    args = adapter._augment_command(["claude", "-p", "prompt"], call)
    assert "--dangerously-skip-permissions" in args


def test_shared_write_gate_covers_codex_antigravity_and_zcode(tmp_path):
    config = default_config()
    for harness in ("codex", "antigravity", "zcode"):
        config.harnesses[harness].auto_approve = True

    codex = _adapter(CodexAdapter, tmp_path, config)
    args = codex._augment_command(["codex", "exec", "--json", "prompt"], _call(tmp_path, shared_write=True))
    assert "--full-auto" not in args

    antigravity = _adapter(AntigravityAdapter, tmp_path, config)
    args = antigravity._augment_command(["agy", "-p", "prompt"], _call(tmp_path, shared_write=True))
    assert "--dangerously-skip-permissions" not in args

    zcode = _adapter(ZCodeAdapter, tmp_path, config)
    args = zcode._augment_command(["zcode", "-p", "prompt"], _call(tmp_path, shared_write=True))
    assert "--mode" not in args or "yolo" not in args

    config.auto_approve_shared_write = True
    args = codex._augment_command(["codex", "exec", "--json", "prompt"], _call(tmp_path, shared_write=True))
    assert "--full-auto" in args


def test_dsh_permission_mode_honours_auto_approve_and_read_only(tmp_path):
    config = default_config()
    adapter = _adapter(DeepSeekAdapter, tmp_path, config)
    config.harnesses["deepseek"].auto_approve = False
    env = adapter._build_env(_call(tmp_path))
    assert env["DSH_PERMISSION_MODE"] == "workspace-write"

    config.harnesses["deepseek"].auto_approve = True
    env = adapter._build_env(_call(tmp_path))
    assert env["DSH_PERMISSION_MODE"] == "danger-full-access"
    env = adapter._build_env(_call(tmp_path, read_only=True))
    assert env["DSH_PERMISSION_MODE"] == "read-only"
    env = adapter._build_env(_call(tmp_path, shared_write=True))
    assert env["DSH_PERMISSION_MODE"] == "workspace-write"
    config.auto_approve_shared_write = True
    env = adapter._build_env(_call(tmp_path, shared_write=True))
    assert env["DSH_PERMISSION_MODE"] == "danger-full-access"


@pytest.mark.asyncio
async def test_builtin_runtime_scrubs_secret_environment(tmp_path):
    config = default_config()
    adapter = DeepSeekAdapter(
        config,
        config.harnesses["deepseek"],
        tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "LANG": "C",
            "OPENAI_API_KEY": "sk-should-not-appear",
            "GITHUB_TOKEN": "ghp-should-not-appear",
            "SERVICE_SECRET": "secret-value",
            "DB_PASSWORD": "password-value",
        },
        which=lambda name: None,
    )
    runtime = _BuiltinDeepSeekRuntime(adapter, _call(tmp_path), api_key="test-key")
    output = await runtime._run_command({"command": "env"})
    assert "sk-should-not-appear" not in output
    assert "ghp-should-not-appear" not in output
    assert "secret-value" not in output
    assert "password-value" not in output
    assert "PATH=" in output
