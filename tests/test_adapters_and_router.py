"""Adapter parsing, timeout kill, paged resources, parallel dispatch and entry points."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
import types
from pathlib import Path
from shlex import quote

import httpx
import pytest
from typer.testing import CliRunner

from harness_mesh.adapters import (
    AdapterCall,
    AntigravityAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    DeepSeekAdapter,
    ZCodeAdapter,
)
from harness_mesh.adapters.base import render_command
from harness_mesh.cli import app as cli_app
from harness_mesh.config import default_config, resolve_harness_binary
from harness_mesh.mcp_server import TOOL_NAMES, create_server
from harness_mesh.models import TaskRequest, TaskStatus, WorkspaceMode
from harness_mesh.storage import ResourceNotFoundError
from harness_mesh.workspace import WorkspaceError
from tests.helpers import FakeAdapter, make_mesh, success, write_claude_settings, write_codex_config


def test_command_renderer_drops_empty_flag_value_pairs():
    rendered = render_command(
        ["claude", "--model", "{model}", "-p", "{prompt}", "--mcp-config", "{mcp_config}"],
        {"model": "", "prompt": "do work", "mcp_config": ""},
    )
    assert rendered == ["claude", "-p", "do work"]

    rendered = render_command(
        ["codex", "--model", "{model}", "{prompt}"],
        {"model": "gpt-5-codex", "prompt": "do work"},
    )
    assert rendered == ["codex", "--model", "gpt-5-codex", "do work"]


@pytest.mark.asyncio
async def test_claude_adapter_renders_flags_and_parses_json(tmp_path):
    config = default_config()
    captured = {}

    async def runner(args, cwd, timeout, env):
        captured["args"] = args
        return (
            0,
            json.dumps({"result": "claude finished", "session_id": "sess-claude", "is_error": False}),
            "",
            False,
        )

    adapter = ClaudeCodeAdapter(
        config,
        config.harnesses["claude_code"],
        tmp_path,
        env={},
        which=lambda name: "/fake/claude",
        runner=runner,
    )
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text("{}", encoding="utf-8")
    outcome = await adapter.execute(
        AdapterCall(
            task_id="task-adapter",
            assignment_id="asg-1",
            prompt="write the code",
            workdir=tmp_path,
            model="claude-opus-9",
            system_prompt="follow the skill",
            mcp_config_path=mcp_path,
        )
    )

    assert outcome.exit_code == 0
    assert outcome.parsed_output == "claude finished"
    assert outcome.session_id == "sess-claude"
    args = captured["args"]
    assert args[0] == "/fake/claude"
    assert "--model" in args and args[args.index("--model") + 1] == "claude-opus-9"
    assert "--append-system-prompt" in args
    assert args[args.index("--append-system-prompt") + 1] == "follow the skill"
    assert "--mcp-config" in args
    assert args[args.index("--mcp-config") + 1] == str(mcp_path)

    await adapter.execute(
        AdapterCall(
            task_id="task-adapter",
            assignment_id="asg-2",
            prompt="use the active default",
            workdir=tmp_path,
            model="<cli_active_default>",
        )
    )
    assert "--model" not in captured["args"]
    assert "--append-system-prompt" not in captured["args"]


@pytest.mark.asyncio
async def test_codex_and_zcode_adapters_parse_event_streams_and_plugins(tmp_path):
    config = default_config()
    codex_output = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "codex assistant text"},
                }
            ),
        ]
    )

    codex_args = {}

    async def codex_runner(args, cwd, timeout, env):
        codex_args["args"] = args
        return 0, codex_output, "", False

    codex = CodexAdapter(
        config,
        config.harnesses["codex"],
        tmp_path,
        env={},
        which=lambda name: "/fake/codex",
        runner=codex_runner,
    )
    codex_mcp = tmp_path / ".mcp.codex.json"
    codex_mcp.write_text(
        json.dumps({"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "server"]}}}),
        encoding="utf-8",
    )
    outcome = await codex.execute(
        AdapterCall(
            task_id="task-codex",
            assignment_id="asg-codex",
            prompt="do work",
            workdir=tmp_path,
            model="gpt-5-codex",
            mcp_config_path=codex_mcp,
        )
    )
    assert outcome.parsed_output == "codex assistant text"
    assert outcome.session_id == "thread-1"
    args = codex_args["args"]
    assert "-c" in args
    assert any("mcp_servers.filesystem" in item for item in args)
    assert args[-1] == "do work"

    zcode_args = {}

    async def zcode_runner(args, cwd, timeout, env):
        zcode_args["args"] = args
        return (
            0,
            "ZCode Built-in updated revision=42\n"
            + json.dumps({"response": "zcode done", "sessionId": "sess-z-1"}),
            "",
            False,
        )

    mcp_path = tmp_path / ".mcp.z.json"
    mcp_path.write_text("{}", encoding="utf-8")
    zcode = ZCodeAdapter(
        config,
        config.harnesses["zcode"],
        tmp_path,
        env={},
        which=lambda name: "/fake/zcode",
        runner=zcode_runner,
    )
    zcode_outcome = await zcode.execute(
        AdapterCall(
            task_id="task-z",
            assignment_id="asg-z",
            prompt="do work",
            workdir=tmp_path,
            model="zcode-pro",
            plugins=["alpha"],
            mcp_config_path=mcp_path,
        )
    )
    assert zcode_outcome.parsed_output == "zcode done"
    assert zcode_outcome.session_id == "sess-z-1"
    assert "--mode" in zcode_args["args"]
    assert zcode_args["args"][zcode_args["args"].index("--mode") + 1] == "yolo"
    # zcode 0.17 does not accept --mcp-config/--plugin flags.
    assert "--mcp-config" not in zcode_args["args"]
    assert "--plugin" not in zcode_args["args"]
    assert zcode.supports_mcp_config is False


def test_binary_paths_glob_and_zcode_wrapper(tmp_path):
    bin_root = tmp_path / "versions"
    for version in ("2.1.0", "2.1.3"):
        executable = bin_root / version / "claude.app" / "Contents" / "MacOS" / "claude"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)

    config = default_config()
    claude_cfg = config.harnesses["claude_code"].model_copy(
        update={"binary_paths": [str(bin_root / "*" / "claude.app" / "Contents" / "MacOS" / "claude")]}
    )
    resolved = resolve_harness_binary(
        claude_cfg,
        which_fn=lambda name: None,
        state_dir=tmp_path / "state",
        home_dir=tmp_path,
        allow_system_paths=True,
    )
    assert resolved is not None and "2.1.3" in resolved

    # Injected which functions never scan host/system paths.
    assert (
        resolve_harness_binary(
            claude_cfg,
            which_fn=lambda name: None,
            state_dir=tmp_path / "state",
            home_dir=tmp_path,
        )
        is None
    )

    electron = tmp_path / "ZCode.app" / "Contents" / "MacOS" / "ZCode"
    electron.parent.mkdir(parents=True)
    electron.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    electron.chmod(0o755)
    cjs_entry = tmp_path / "ZCode.app" / "Contents" / "Resources" / "glm" / "zcode.cjs"
    cjs_entry.parent.mkdir(parents=True)
    cjs_entry.write_text("// fake entry\n", encoding="utf-8")
    zcode_cfg = config.harnesses["zcode"].model_copy(
        update={
            "binary_paths": [],
            "extra": {
                **config.harnesses["zcode"].extra,
                "macos_electron_bin": str(electron),
                "macos_cjs_entry": str(cjs_entry),
                "macos_builtin_provider_config": str(tmp_path / "builtin.json"),
            },
        }
    )
    shim = resolve_harness_binary(
        zcode_cfg,
        which_fn=lambda name: None,
        state_dir=tmp_path / "state",
        home_dir=tmp_path,
        allow_system_paths=True,
    )
    assert shim is not None
    shim_path = Path(shim)
    assert shim_path.exists()
    assert os.access(shim_path, os.X_OK)
    script_text = shim_path.read_text(encoding="utf-8")
    assert "ELECTRON_RUN_AS_NODE=1" in script_text
    assert "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE=" in script_text
    assert str(electron) in script_text


@pytest.mark.asyncio
async def test_claude_effort_auto_approve_and_resume_flags(tmp_path):
    captured = {}

    async def runner(args, cwd, timeout, env):
        captured["args"] = args
        return 0, json.dumps({"result": "ok", "session_id": "sess-new"}), "", False

    config = default_config()
    adapter = ClaudeCodeAdapter(
        config,
        config.harnesses["claude_code"],
        tmp_path,
        env={},
        which=lambda name: "/fake/claude",
        runner=runner,
    )
    outcome = await adapter.execute(
        AdapterCall(
            task_id="task-claude-flags",
            assignment_id="asg-claude-flags",
            prompt="do work",
            workdir=tmp_path,
            model="demo-opus-5-5",
            reasoning_effort="max",
            session_id="sess-resume",
        )
    )
    args = captured["args"]
    assert outcome.parsed_output == "ok"
    assert args[args.index("--effort") + 1] == "max"
    assert "--dangerously-skip-permissions" in args
    assert args[args.index("--resume") + 1] == "sess-resume"
    assert "--permission-mode" not in args


@pytest.mark.asyncio
async def test_codex_full_auto_reasoning_and_resume_subcommand(tmp_path):
    captured = {}

    async def runner(args, cwd, timeout, env):
        captured["args"] = args
        return 0, json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}), "", False

    config = default_config()
    adapter = CodexAdapter(
        config,
        config.harnesses["codex"],
        tmp_path,
        env={},
        which=lambda name: "/fake/codex",
        runner=runner,
    )
    await adapter.execute(
        AdapterCall(
            task_id="task-codex-flags",
            assignment_id="asg-codex-flags",
            prompt="do work",
            workdir=tmp_path,
            model="demo-gpt-6.1-sol",
            reasoning_effort="high",
            session_id="sess-codex",
        )
    )
    args = captured["args"]
    assert "--full-auto" in args
    assert 'model_reasoning_effort="high"' in args
    assert "resume" in args
    assert args[args.index("resume") + 1] == "sess-codex"
    assert args[-1] == "do work"


@pytest.mark.asyncio
async def test_antigravity_native_async_sdk(tmp_path, monkeypatch):
    class _FakeConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _FakeAgent:
        def __init__(self, config):
            self.config = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def chat(self, prompt):
            async def stream():
                for token in ("O", "K"):
                    yield token

            return stream()

    module = types.ModuleType("fake.antigravity")
    module.Agent = _FakeAgent
    module.LocalAgentConfig = _FakeConfig
    monkeypatch.setitem(sys.modules, "fake.antigravity", module)

    config = default_config()
    adapter = AntigravityAdapter(
        config,
        config.harnesses["antigravity"],
        tmp_path,
        env={},
        which=lambda name: None,
    )
    adapter._sdk_module_name = "fake.antigravity"
    adapter._sdk_enabled = True
    monkeypatch.setattr(adapter, "_sdk_available", lambda: True)

    outcome = await adapter.execute(
        AdapterCall(
            task_id="task-sdk",
            assignment_id="asg-sdk",
            prompt="say ok",
            workdir=tmp_path,
            model="demo-gemini-4-argon-high",
        )
    )
    assert outcome.exit_code == 0
    assert outcome.parsed_output == "OK"
    assert outcome.metadata.get("native_async") is True


def test_deepseek_api_endpoint_v1_normalization():
    config = default_config()
    adapter = DeepSeekAdapter(
        config,
        config.harnesses["deepseek"],
        Path("/tmp"),
        env={"DEEPSEEK_BASE_URL": "https://api.deepseek.com"},
        which=lambda name: None,
    )
    assert adapter._api_endpoint("/models") == "https://api.deepseek.com/v1/models"
    assert adapter._api_endpoint("/chat/completions") == "https://api.deepseek.com/v1/chat/completions"

    adapter.env["DEEPSEEK_BASE_URL"] = "https://gateway.example/v1"
    assert adapter._api_endpoint("/models") == "https://gateway.example/v1/models"


@pytest.mark.asyncio
async def test_deepseek_adapter_builtin_runtime_executes_tool_calls(tmp_path):
    config = default_config()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "chat-1",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "write_file",
                                            "arguments": json.dumps(
                                                {"path": "deep.txt", "content": "builtin runtime file"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={"id": "chat-2", "choices": [{"message": {"role": "assistant", "content": "builtin done"}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekAdapter(
        config,
        config.harnesses["deepseek"],
        tmp_path,
        env={"DEEPSEEK_API_KEY": "test-deepseek-key"},
        which=lambda name: None,
        http_client=client,
    )
    try:
        outcome = await adapter.execute(
            AdapterCall(
                task_id="task-deep",
                assignment_id="asg-deep",
                prompt="create deep.txt",
                workdir=tmp_path,
                model="deepseek-chat",
                reasoning_effort="high",
            )
        )
        assert outcome.exit_code == 0
        assert outcome.parsed_output == "builtin done"
        assert outcome.session_id == "chat-2"
        assert (tmp_path / "deep.txt").read_text(encoding="utf-8") == "builtin runtime file"
        assert len(calls) == 2
        assert calls[0]["reasoning_effort"] == "high"
        assert adapter.supports_mcp_config is False
        assert calls[1]["messages"][-1]["role"] == "tool"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_duplicate_live_task_id_is_rejected_without_corrupting_the_ticket(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"count": 0}

    async def handler(adapter, call):
        calls["count"] += 1
        started.set()
        await release.wait()
        return success("first dispatch completed")

    mesh = make_mesh(
        tmp_path,
        adapters={"claude_code": FakeAdapter("claude_code", handler)},
    )
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        request_kwargs = dict(
            task_id="duplicate-task",
            target_harness="claude_code",
            objective="Only one dispatch may execute this ticket.",
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        first = asyncio.create_task(mesh.router.dispatch(TaskRequest(**request_kwargs)))
        await asyncio.wait_for(started.wait(), timeout=2)

        duplicate = await mesh.router.dispatch(TaskRequest(**request_kwargs))
        assert duplicate.status == TaskStatus.REJECTED
        assert "duplicate_active_task" in (duplicate.error_detail or "")

        release.set()
        first_result = await asyncio.wait_for(first, timeout=5)
        assert first_result.status == TaskStatus.COMPLETED
        assert calls["count"] == 1
        assert len(mesh.storage.list_assignments("duplicate-task")) == 1
        assert mesh.storage.get_task("duplicate-task")["status"] == "completed"
    finally:
        release.set()
        mesh.storage.close()


@pytest.mark.asyncio
async def test_read_only_mode_uses_disposable_worktree_and_cannot_be_applied(tmp_path):
    captured = {}

    def handler(adapter, call):
        captured["read_only"] = call.read_only
        captured["prompt"] = call.prompt
        (call.workdir / "agent-write.txt").write_text("isolated write\n", encoding="utf-8")
        return success("read-only inspection finished")

    mesh = make_mesh(
        tmp_path,
        adapters={"claude_code": FakeAdapter("claude_code", handler)},
    )
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        request = TaskRequest(
            target_harness="claude_code",
            objective="Inspect the project without changing it.",
            workspace_mode=WorkspaceMode.READ_ONLY,
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        result = await mesh.router.dispatch(request)

        assert result.status == TaskStatus.COMPLETED
        assert captured["read_only"] is True
        assert "READ-ONLY" in captured["prompt"]
        assert not (mesh.workspace_root / "agent-write.txt").exists()
        assert result.worktree_path is not None
        assert (Path(result.worktree_path) / "agent-write.txt").exists()
        assert result.modified_files == []
        assert result.git_diff == ""
        with pytest.raises(WorkspaceError):
            mesh.router.apply_task_patch(request.task_id)
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_reasoning_effort_reaches_adapter_and_result(tmp_path):
    captured = {}

    def handler(adapter, call):
        captured["effort"] = call.reasoning_effort
        captured["prompt"] = call.prompt
        return success("effort applied")

    mesh = make_mesh(
        tmp_path,
        adapters={"claude_code": FakeAdapter("claude_code", handler)},
    )
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="Implement a deterministic helper.",
                reasoning_effort="low",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED
        assert result.resolved_reasoning_effort == "low"
        assert captured["effort"] == "low"
        assert "reasoning_effort: low" in captured["prompt"]
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_adapter_env_and_readonly_flags_are_injected(tmp_path):
    captured = {}

    async def claude_runner(args, cwd, timeout, env):
        captured["args"] = args
        captured["env"] = env
        return 0, json.dumps({"result": "ok"}), "", False

    config = default_config()
    claude = ClaudeCodeAdapter(
        config,
        config.harnesses["claude_code"],
        tmp_path,
        env={},
        which=lambda name: "/fake/claude",
        runner=claude_runner,
    )
    await claude.execute(
        AdapterCall(
            task_id="task-env",
            assignment_id="asg-env",
            prompt="inspect",
            workdir=tmp_path,
            model="claude-opus-9",
            reasoning_effort="high",
            read_only=True,
        )
    )
    assert captured["env"]["HARNESS_MESH_REASONING_EFFORT"] == "high"
    assert captured["env"]["HARNESS_MESH_READ_ONLY"] == "1"
    assert "--permission-mode" in captured["args"]
    assert captured["args"][captured["args"].index("--permission-mode") + 1] == "plan"

    codex_captured = {}

    async def codex_runner(args, cwd, timeout, env):
        codex_captured["args"] = args
        return 0, json.dumps({"result": "ok"}), "", False

    codex = CodexAdapter(
        config,
        config.harnesses["codex"],
        tmp_path,
        env={},
        which=lambda name: "/fake/codex",
        runner=codex_runner,
    )
    await codex.execute(
        AdapterCall(
            task_id="task-codex-env",
            assignment_id="asg-codex-env",
            prompt="inspect",
            workdir=tmp_path,
            model="gpt-5-codex",
            reasoning_effort="high",
            read_only=True,
        )
    )
    codex_args = codex_captured["args"]
    assert "--sandbox" in codex_args
    assert codex_args[codex_args.index("--sandbox") + 1] == "read-only"
    assert any(item == 'model_reasoning_effort="high"' for item in codex_args)


@pytest.mark.asyncio
async def test_deepseek_builtin_kills_shell_child_on_cancellation(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chat-cancel",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-cancel",
                                    "type": "function",
                                    "function": {
                                        "name": "run_command",
                                        "arguments": json.dumps(
                                            {
                                                "command": (
                                                    f"{quote(sys.executable)} -c \"import os, pathlib, time; "
                                                    "pathlib.Path('builtin.pid').write_text(str(os.getpid())); "
                                                    'time.sleep(60)\"'
                                                )
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = default_config()
    adapter = DeepSeekAdapter(
        config,
        config.harnesses["deepseek"],
        tmp_path,
        env={"DEEPSEEK_API_KEY": "test-key"},
        which=lambda name: None,
        http_client=client,
    )
    execution = asyncio.create_task(
        adapter.execute(
            AdapterCall(
                task_id="task-builtin-cancel",
                assignment_id="asg-builtin-cancel",
                prompt="run the command",
                workdir=tmp_path,
                model="deepseek-chat",
            )
        )
    )
    try:
        pid: int | None = None
        for _ in range(100):
            pid_file = tmp_path / "builtin.pid"
            if pid_file.exists():
                pid = int(pid_file.read_text().strip())
                break
            await asyncio.sleep(0.05)
        assert pid is not None, "builtin runtime command did not start"
        execution.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(execution, timeout=10)

        alive = True
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            await asyncio.sleep(0.02)
        assert alive is False
    finally:
        if not execution.done():
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
        await client.aclose()


@pytest.mark.asyncio
async def test_deepseek_cli_generates_overlay_and_parses_ndjson(tmp_path):
    config = default_config()
    home = tmp_path / "home"
    dsh_home = home / ".dsh"
    desktop = dsh_home / "profiles" / "desktop" / "cordis.patch.yml"
    desktop.parent.mkdir(parents=True)
    desktop.write_text(
        "\n".join(
            [
                "- id: agent-default-model",
                '  name: "@deepseek-ai/dsh-agent-default-model"',
                "  config:",
                "    provider: opencode-go",
                "    model: deepseek-desktop",
                "- id: llm-pi-ai",
                '  name: "@deepseek-ai/dsh-llm-pi-ai"',
                "  config:",
                "    providers:",
                "      opencode-go:",
                "        apiKeyEnv: OPENCODE_GO_API_KEY",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    captured = {}

    async def dsh_runner(args, cwd, timeout, env):
        captured["args"] = args
        captured["env"] = env
        overlay_index = args.index("--patch") + 1
        overlay_path = Path(args[overlay_index])
        captured["overlay_path"] = overlay_path
        captured["overlay_text"] = overlay_path.read_text(encoding="utf-8")
        return (
            0,
            '{"type": "session", "sessionId": "sess-dsh-1"}\n'
            '{"type": "text", "text": "partial "}\n'
            '{"type": "final", "text": "dsh finished"}\n',
            "",
            False,
        )

    deepseek = DeepSeekAdapter(
        config,
        config.harnesses["deepseek"],
        tmp_path,
        env={"HOME": str(home), "DSH_HOME": str(dsh_home)},
        which=lambda name: "/fake/dsh" if name in {"dsh", "deepseek"} else None,
        runner=dsh_runner,
    )
    assert deepseek.supports_mcp_config is False
    outcome = await deepseek.execute(
        AdapterCall(
            task_id="task-dsh",
            assignment_id="asg-dsh",
            prompt="do work",
            workdir=tmp_path,
            model="demo-deepseek-4.1-flash",
            reasoning_effort="max",
            session_id="sess-resume",
            read_only=True,
        )
    )
    args = captured["args"]
    assert outcome.exit_code == 0
    assert outcome.parsed_output == "dsh finished"
    assert outcome.session_id == "sess-dsh-1"
    assert "--patch" in args
    assert "--session-id" in args
    assert captured["env"]["DSH_PERMISSION_MODE"] == "read-only"
    assert "credentials" in captured["overlay_text"]
    assert "demo-deepseek-4.1-flash" in captured["overlay_text"]
    assert "reasoningEffort: max" in captured["overlay_text"]
    assert not captured["overlay_path"].exists()  # cleaned in finally


@pytest.mark.asyncio
async def test_antigravity_cli_permissions_session_and_json_parser(tmp_path):
    config = default_config()
    captured = {}

    async def agy_runner(args, cwd, timeout, env):
        captured["args"] = args
        return (
            0,
            json.dumps(
                {
                    "conversation_id": "conv-9",
                    "status": "SUCCESS",
                    "response": "OK\n",
                    "usage": {},
                }
            ),
            "",
            False,
        )

    antigravity = AntigravityAdapter(
        config,
        config.harnesses["antigravity"],
        tmp_path,
        env={},
        which=lambda name: "/fake/agy",
        runner=agy_runner,
    )
    assert antigravity.supports_mcp_config is False
    outcome = await antigravity.execute(
        AdapterCall(
            task_id="task-agy",
            assignment_id="asg-agy",
            prompt="do work",
            workdir=tmp_path,
            model="demo-gemini-4-argon-high",
            reasoning_effort="high",
            session_id="conv-resume",
        )
    )
    args = captured["args"]
    assert outcome.exit_code == 0
    assert outcome.parsed_output == "OK"
    assert outcome.session_id == "conv-9"
    assert "--dangerously-skip-permissions" in args
    assert "--conversation" in args
    assert args[args.index("--conversation") + 1] == "conv-resume"


@pytest.mark.asyncio
async def test_claude_error_subtype_is_treated_as_failure(tmp_path):
    config = default_config()

    async def runner(args, cwd, timeout, env):
        return 0, json.dumps({"subtype": "error", "result": "Credit balance is too low"}), "", False

    adapter = ClaudeCodeAdapter(
        config,
        config.harnesses["claude_code"],
        tmp_path,
        env={},
        which=lambda name: "/fake/claude",
        runner=runner,
    )
    outcome = await adapter.execute(
        AdapterCall(
            task_id="task-claude-error",
            assignment_id="asg-claude-error",
            prompt="do work",
            workdir=tmp_path,
            model="claude-opus-9",
        )
    )
    assert outcome.exit_code != 0
    assert outcome.error_detail
    assert "Credit balance" in (outcome.stderr + outcome.error_detail)


@pytest.mark.asyncio
async def test_base_adapter_probe_quota_requires_a_real_completion(tmp_path):
    config = default_config()

    async def ok_runner(args, cwd, timeout, env):
        return 0, json.dumps({"result": "OK", "session_id": "probe-session"}), "", False

    ok_adapter = ClaudeCodeAdapter(
        config,
        config.harnesses["claude_code"],
        tmp_path,
        env={},
        which=lambda name: "/fake/claude",
        runner=ok_runner,
    )
    probe = await ok_adapter.probe_quota("claude-opus-9")
    assert probe is not None
    assert probe[0] is True

    async def exhausted_runner(args, cwd, timeout, env):
        return 1, "", "HTTP 429 too many requests: rate_limit_exceeded", False

    exhausted_adapter = ClaudeCodeAdapter(
        config,
        config.harnesses["claude_code"],
        tmp_path,
        env={},
        which=lambda name: "/fake/claude",
        runner=exhausted_runner,
    )
    probe = await exhausted_adapter.probe_quota("claude-opus-9")
    assert probe is not None
    assert probe[0] is False
    assert "quota" in probe[1]


@pytest.mark.asyncio
async def test_router_timeout_marks_ticket_timed_out(tmp_path):
    async def slow_handler(adapter, call):
        await asyncio.sleep(10)
        return success("too late")

    fake = FakeAdapter("claude_code", slow_handler)
    mesh = make_mesh(tmp_path, adapters={"claude_code": fake})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        request = TaskRequest(
            target_harness="claude_code",
            objective="Sleep for too long.",
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=1,
        )
        started = time.monotonic()
        result = await mesh.router.dispatch(request)
        elapsed = time.monotonic() - started

        assert result.status == TaskStatus.TIMEOUT
        assert result.error_detail == "timeout"
        assert elapsed < 6
        assert [assignment.status for assignment in result.assignments] == ["ended_error"]
        assert result.assignments[0].error_reason == "timeout"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_summary_and_diff_are_capped_while_full_resources_are_paged(tmp_path):
    def handler(adapter, call):
        (call.workdir / "big.txt").write_text("x" * 2000 + "\n", encoding="utf-8")
        return success("A" * 800)

    fake = FakeAdapter("claude_code", handler)
    mesh = make_mesh(
        tmp_path,
        adapters={"claude_code": fake},
        config_updates={"summary_max_chars": 60, "diff_preview_max_chars": 80},
    )
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        request = TaskRequest(
            target_harness="claude_code",
            objective="Produce a large file and a large report.",
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        result = await mesh.router.dispatch(request)

        assert result.status == TaskStatus.COMPLETED
        assert len(result.summary) <= 60
        assert "truncated" in result.summary
        assert result.git_diff_truncated is True
        assert len(result.git_diff) <= 80
        assert len(result.raw_stdout) <= 80  # raw logs are capped as well
        assert set(result.read_pointers) == {"diff", "transcript", "assignments"}

        diff_page = mesh.router.read_task_resource(request.task_id, "diff", offset=0, limit=100)
        full_diff = mesh.storage.get_resource(request.task_id, "diff")
        assert diff_page["total_length"] == len(full_diff)
        assert diff_page["has_more"] is True
        assert diff_page["next_offset"] == 100

        chunks: list[str] = []
        offset = 0
        while True:
            page = mesh.router.read_task_resource(request.task_id, "transcript", offset=offset, limit=128)
            chunks.append(page["content"])
            if not page["has_more"]:
                break
            offset = page["next_offset"]
        assert "".join(chunks) == mesh.storage.get_resource(request.task_id, "transcript")
        assert "Assignment #1" in "".join(chunks)

        assignments_page = mesh.router.read_task_resource(request.task_id, "assignments", limit=50)
        assignments_payload = json.loads(assignments_page["content"]) if not assignments_page["has_more"] else None
        if assignments_payload is None:
            accumulated = []
            offset = 0
            while True:
                page = mesh.router.read_task_resource(request.task_id, "assignments", offset=offset, limit=128)
                accumulated.append(page["content"])
                if not page["has_more"]:
                    break
                offset = page["next_offset"]
            assignments_payload = json.loads("".join(accumulated))
        assert len(assignments_payload["assignments"]) == 1

        with pytest.raises(ResourceNotFoundError):
            mesh.storage.read_task_resource(request.task_id, "does-not-exist")
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_parallel_dispatch_runs_independent_harnesses(tmp_path):
    async def claude_handler(adapter, call):
        await asyncio.sleep(0.05)
        (call.workdir / "claude.txt").write_text("claude\n", encoding="utf-8")
        return success("claude parallel done")

    async def codex_handler(adapter, call):
        await asyncio.sleep(0.05)
        (call.workdir / "codex.txt").write_text("codex\n", encoding="utf-8")
        return success("codex parallel done")

    mesh = make_mesh(
        tmp_path,
        adapters={
            "claude_code": FakeAdapter("claude_code", claude_handler),
            "codex": FakeAdapter("codex", codex_handler),
        },
    )
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        write_codex_config(mesh.home_dir, model="gpt-5-codex")

        requests = [
            TaskRequest(
                target_harness="claude_code",
                objective="Write claude.txt",
                workspace_root=str(tmp_path / "ws_claude"),
                timeout_seconds=30,
            ),
            TaskRequest(
                target_harness="codex",
                objective="Write codex.txt",
                workspace_root=str(tmp_path / "ws_codex"),
                timeout_seconds=30,
            ),
        ]
        results = await mesh.router.parallel_dispatch(requests, max_concurrency=2)

        assert len(results) == 2
        assert {result.status for result in results} == {TaskStatus.COMPLETED}
        assert {result.target_harness for result in results} == {"claude_code", "codex"}
        assert len({result.task_id for result in results}) == 2
        claude_result = next(result for result in results if result.target_harness == "claude_code")
        assert claude_result.status == TaskStatus.COMPLETED
        assert not (Path(requests[0].workspace_root) / "claude.txt").exists()  # merged only on demand
        assert claude_result.worktree_path is not None
        assert (Path(claude_result.worktree_path) / "claude.txt").exists()
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_cancel_task_kills_the_child_harness_process(tmp_path):
    import sys

    config = default_config()
    script = (
        "import os, pathlib, time; "
        "pathlib.Path('child.pid').write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    harness_config = config.harnesses["claude_code"].model_copy(
        update={"execute_template": [sys.executable, "-c", script]}
    )
    real_adapter = ClaudeCodeAdapter(
        config,
        harness_config,
        tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        which=lambda name: "/fake/claude",
    )
    mesh = make_mesh(tmp_path, adapters={"claude_code": real_adapter})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        request = TaskRequest(
            task_id="task-cancel-process",
            target_harness="claude_code",
            objective="Run a long child process that must be killed on cancel.",
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=120,
        )
        dispatch_task = asyncio.create_task(mesh.router.dispatch(request))
        worktree = mesh.workspace.worktree_path(request.task_id)
        pid: int | None = None
        for _ in range(100):
            pid_file = worktree / "child.pid"
            if pid_file.exists():
                pid = int(pid_file.read_text().strip())
                break
            await asyncio.sleep(0.05)
        assert pid is not None, "child harness process did not start"

        cancelled = await mesh.router.cancel_task(request.task_id)
        assert cancelled["status"] == "cancelled"
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(dispatch_task, timeout=10)

        alive = True
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            await asyncio.sleep(0.02)
        assert alive is False, "cancelled assignment left its harness process group running"

        record = mesh.storage.get_task(request.task_id)
        assert record["status"] == "cancelled"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_mcp_server_registers_thirteen_tools(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        server = create_server(mesh.router)
        tools = await server.list_tools()
        names = {tool.name for tool in tools}
        assert names == set(TOOL_NAMES)
        assert len(names) == 13

        by_name = {tool.name: tool for tool in tools}
        dispatch_schema = by_name["dispatch_task"].inputSchema
        assert {"objective", "target_harness"} <= set(dispatch_schema["required"])
        assert {
            "difficulty",
            "on_quota_exhausted",
            "fallback_harnesses",
            "max_failover_attempts",
            "call_depth",
            "trace_chain",
            "checkpoint",
            "depends_on",
            "sync_uncommitted",
            "include_project_context",
            "auto_record_context",
            "session_id",
        } <= set(dispatch_schema["properties"])
        read_schema = by_name["read_task_resource"].inputSchema
        assert {"task_id", "resource", "offset", "limit"} <= set(read_schema["properties"])
        report_schema = by_name["report_quota_status"].inputSchema
        assert "harness" in report_schema["required"]
        assert {"model", "action", "cooldown_seconds"} <= set(report_schema["properties"])
        context_schema = by_name["update_project_context"].inputSchema
        assert {"key", "content"} <= set(context_schema["required"])
        assert {"category", "author_harness", "task_id"} <= set(context_schema["properties"])
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_auto_target_resolves_to_a_concrete_harness(tmp_path):
    fake = FakeAdapter("claude_code", lambda adapter, call: success("auto target done"))
    mesh = make_mesh(tmp_path, adapters={"claude_code": fake})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        request = TaskRequest(
            target_harness="auto",
            objective="Run wherever an authorised model exists.",
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        result = await mesh.router.dispatch(request)

        assert result.status == TaskStatus.COMPLETED
        assert result.initial_harness == "claude_code"
        assert result.target_harness == "claude_code"
        record = mesh.storage.get_task(request.task_id)
        assert record["initial_harness"] == "claude_code"
        assert record["current_harness"] == "claude_code"
        assert record["resolved_difficulty"] is not None
        assert record["resolved_model"] == "claude-opus-9"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_base_adapter_kills_a_timed_out_subprocess(tmp_path):
    import sys

    config = default_config()
    harness_config = config.harnesses["claude_code"].model_copy(
        update={
            "execute_template": [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ]
        }
    )
    adapter = ClaudeCodeAdapter(
        config,
        harness_config,
        tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        which=lambda name: "/fake/claude",
    )
    started = time.monotonic()
    outcome = await adapter.execute(
        AdapterCall(
            task_id="task-timeout",
            assignment_id="asg-timeout",
            prompt="unused",
            workdir=tmp_path,
            model="claude-opus-9",
            timeout_seconds=1,
        )
    )
    elapsed = time.monotonic() - started

    assert outcome.timed_out is True
    assert outcome.exit_code == 124
    assert elapsed < 8
    assert "timed out" in outcome.stderr


def test_cli_init_mcp_and_help():
    runner = CliRunner()
    result = runner.invoke(cli_app, ["init-mcp", "--print"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mcpServers"]["harness-mesh"]["command"] == "hmesh-mcp"

    help_result = runner.invoke(cli_app, ["--help"])
    assert help_result.exit_code == 0
    for command in ("status", "models", "quota", "skills", "dispatch", "tasks", "read", "apply", "init-mcp", "context"):
        assert command in help_result.output


def test_cli_status_models_quota_and_skills_commands(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    write_claude_settings(home, model="claude-opus-9")
    config_path = Path(__file__).resolve().parents[1] / "config" / "harnesses.example.yaml"
    env = {
        "HOME": str(home),
        "DSH_HOME": str(home / ".dsh"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1",
    }
    runner = CliRunner()
    base = ["--workspace", str(workspace), "--config", str(config_path)]

    status_result = runner.invoke(cli_app, [*base, "status"], env=env)
    assert status_result.exit_code == 0, status_result.output
    assert "claude_code" in status_result.output
    assert "claude-opus-9" in status_result.output

    models_result = runner.invoke(cli_app, [*base, "models", "--refresh", "--harness", "claude_code"], env=env)
    assert models_result.exit_code == 0, models_result.output
    assert "claude-opus-9" in models_result.output
    assert "critical=" in models_result.output

    quota_result = runner.invoke(cli_app, [*base, "quota", "--probe"], env=env)
    assert quota_result.exit_code == 0, quota_result.output
    assert "waiting tickets: 0" in quota_result.output

    skills_result = runner.invoke(cli_app, [*base, "skills"], env=env)
    assert skills_result.exit_code == 0, skills_result.output
    assert "skills (" in skills_result.output
