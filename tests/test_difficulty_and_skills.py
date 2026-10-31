"""Difficulty heuristics, SKILL.md matching and dynamic MCP compilation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_mesh.config import build_handoff_prompt, evaluate_difficulty
from harness_mesh.models import ContinuationCheckpoint, TaskDifficulty, TaskRequest, TaskStatus
from tests.helpers import FakeAdapter, make_mesh, make_skill, success, write_claude_settings, write_json


def test_difficulty_heuristics_use_scope_not_model_names():
    assert evaluate_difficulty("Fix a typo in the README docs") == TaskDifficulty.LOW
    assert evaluate_difficulty("Rename a variable in one file") == TaskDifficulty.LOW
    assert (
        evaluate_difficulty("Implement a small CLI command with tests")
        in {TaskDifficulty.MEDIUM, TaskDifficulty.HIGH}
    )
    assert (
        evaluate_difficulty(
            "Design a secure distributed architecture, migrate the database and refactor the concurrency layer"
        )
        == TaskDifficulty.CRITICAL
    )

    plain = evaluate_difficulty("Update module behaviour")
    many_files = evaluate_difficulty(
        "Update module behaviour",
        context_files=[f"src/module_{index}.py" for index in range(10)],
    )
    assert many_files.value in {"medium", "high", "critical"}
    assert many_files != TaskDifficulty.LOW
    assert plain.value in {"low", "medium"}

    assert evaluate_difficulty("typo ONLY", context_notes="docs") == TaskDifficulty.LOW


def test_handoff_prompt_contains_checkpoint_and_recovery_protocol():
    checkpoint = ContinuationCheckpoint(
        interrupted_harness="claude_code",
        interrupted_model="claude-opus-9",
        interruption_reason="quota_exhausted:budget_exceeded",
        partial_modified_files=["src/a.py", "src/b.py"],
        partial_git_diff="+def partial(): pass\n",
        partial_stdout_tail="previous assignment output tail",
        captured_at="2026-01-01T00:00:00Z",
        worktree_path="/tmp/worktree",
        last_assignment_id="asg_1",
    )
    task = TaskRequest(
        task_id="task_prompt",
        target_harness="codex",
        objective="Finish the interrupted work.",
        difficulty="high",
        workspace_root="/tmp",
        context_files=["src/a.py"],
    )

    prompt = build_handoff_prompt(
        task,
        checkpoint,
        next_harness="codex",
        next_model="gpt-5-codex",
        skills_prompt="<activated_skills>demo</activated_skills>",
        reasoning_effort="high",
    )

    assert "<continuation_checkpoint>" in prompt
    assert "</continuation_checkpoint>" in prompt
    assert "<recovery_note>" in prompt
    assert "git status --porcelain" in prompt
    assert "git diff HEAD" in prompt
    assert "src/a.py" in prompt and "src/b.py" in prompt
    assert "+def partial(): pass" in prompt
    assert "previous assignment output tail" in prompt
    assert "claude_code/claude-opus-9" in prompt
    assert "<activated_skills>demo</activated_skills>" in prompt
    assert "reasoning_effort: high" in prompt
    assert "do NOT revert" in prompt


def test_skill_scan_match_and_mcp_config_compilation(tmp_path):
    mesh = make_mesh(tmp_path)
    try:
        skill_root = mesh.workspace_root / ".harness-mesh" / "skills"
        make_skill(
            skill_root,
            "git-diff-review",
            "Review git diffs for correctness and regressions",
            ["git", "review"],
            body="Run git diff and inspect every hunk.",
        )
        make_skill(
            mesh.home_dir / ".claude" / "skills",
            "security-audit",
            "Audit code for security vulnerabilities",
            ["security"],
            body="Check authentication and injection boundaries.",
        )
        make_skill(
            mesh.home_dir / ".gemini" / "skills",
            "gemini-helper",
            "Gemini Antigravity helper workflow",
            ["gemini"],
        )
        write_json(
            mesh.workspace_root / ".mcp.json",
            {
                "mcpServers": {
                    "filesystem": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                        "env": {"NODE_OPTIONS": "--no-warnings"},
                    },
                    "github": {"command": "npx", "args": ["-y", "gh-mcp"]},
                }
            },
        )
        (mesh.home_dir / ".codex").mkdir(parents=True, exist_ok=True)
        (mesh.home_dir / ".codex" / "config.toml").write_text(
            "[mcp_servers.docs]\n"
            'command = "npx"\n'
            'args = ["-y", "docs-mcp"]\n',
            encoding="utf-8",
        )

        skills = mesh.skills.scan_skills(force=True)
        skill_names = {skill.name for skill in skills}
        assert {"git-diff-review", "security-audit", "gemini-helper"} <= skill_names

        matched = mesh.skills.match_skills("review a git diff for regressions")
        assert matched
        assert matched[0].name == "git-diff-review"

        plugins = mesh.skills.scan_plugins(force=True)
        plugin_names = {plugin.name for plugin in plugins}
        assert {"filesystem", "github", "docs"} <= plugin_names

        task = TaskRequest(
            task_id="task_skills",
            target_harness="claude_code",
            objective="Review a git diff for security regressions",
            auto_match_skills=True,
            skills=["security-audit"],
            plugins=["filesystem"],
            workspace_root=str(mesh.workspace_root),
        )
        selected_skills, selected_plugins = mesh.skills.resolve_for_task(task)
        selected_names = [skill.name for skill in selected_skills]
        assert "security-audit" in selected_names
        assert "git-diff-review" in selected_names
        assert [plugin.name for plugin in selected_plugins] == ["filesystem"]

        config_path = mesh.skills.compile_mcp_config(task.task_id, selected_plugins)
        assert config_path is not None
        assert config_path.name == ".mcp.task_skills.json"
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
        assert payload["mcpServers"]["filesystem"]["command"] == "npx"
        assert payload["mcpServers"]["filesystem"]["args"][1].endswith("server-filesystem")

        prompt = mesh.skills.build_skills_prompt(selected_skills)
        assert "<activated_skills>" in prompt
        assert "git-diff-review" in prompt
        assert "security-audit" in prompt
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_router_does_not_report_plugins_an_adapter_cannot_mount(tmp_path):
    captured = {}

    def handler(adapter, call):
        captured["mcp_path"] = call.mcp_config_path
        captured["plugins"] = call.plugins
        return success("nothing mounted")

    fake = FakeAdapter("claude_code", handler)
    fake.supports_mcp_config = False
    mesh = make_mesh(tmp_path, adapters={"claude_code": fake})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        write_json(
            mesh.workspace_root / ".mcp.json",
            {"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "server-filesystem"]}}},
        )
        result = await mesh.router.dispatch(
            TaskRequest(
                target_harness="claude_code",
                objective="Use a plugin that this channel cannot mount.",
                plugins=["filesystem"],
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED
        assert result.activated_plugins == []
        assert captured["mcp_path"] is None
        assert captured["plugins"] == []
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_router_activates_matched_skill_and_plugins(tmp_path):
    captured = {}

    def handler(adapter, call):
        captured["skills"] = call.skills
        captured["plugins"] = call.plugins
        captured["mcp_path"] = call.mcp_config_path
        captured["prompt"] = call.prompt
        return success("skills executed")

    fake = FakeAdapter("claude_code", handler)
    mesh = make_mesh(tmp_path, adapters={"claude_code": fake})
    try:
        write_claude_settings(mesh.home_dir, model="claude-opus-9")
        make_skill(
            mesh.workspace_root / ".harness-mesh" / "skills",
            "git-diff-review",
            "Review git diffs for correctness",
            ["git", "review"],
        )
        write_json(
            mesh.workspace_root / ".mcp.json",
            {"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "server-filesystem"]}}},
        )

        request = TaskRequest(
            target_harness="claude_code",
            objective="Review the git diff for correctness",
            auto_match_skills=True,
            plugins=["filesystem"],
            workspace_root=str(mesh.workspace_root),
            timeout_seconds=30,
        )
        result = await mesh.router.dispatch(request)

        assert result.status == TaskStatus.COMPLETED
        assert "git-diff-review" in result.activated_skills
        assert result.activated_plugins == ["filesystem"]
        assert captured["skills"] == result.activated_skills
        assert captured["plugins"] == ["filesystem"]
        assert captured["mcp_path"] is not None
        assert Path(captured["mcp_path"]).exists()
        assert "<activated_skills>" in captured["prompt"]
        assert "git-diff-review" in captured["prompt"]
    finally:
        mesh.storage.close()
