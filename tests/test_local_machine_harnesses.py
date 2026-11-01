"""Real-machine integration tests for the five locally installed harnesses.

These tests intentionally call the real CLIs and are skipped on machines where
the expected macOS application bundles are absent.
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import pytest

from harness_mesh.config import default_config
from harness_mesh.models import TaskRequest, TaskStatus
from harness_mesh.router import create_mesh_runtime
from tests.helpers import init_git_repo

REAL_PATHS = {
    "claude_code": "~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude",
    "codex": "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    "deepseek": "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh",
    "antigravity": "/Applications/Gemini.app/Contents/Helpers/agy",
    "zcode": "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
}


def _real_machine_available() -> bool:
    if sys.platform != "darwin":
        return False
    for _, pattern in REAL_PATHS.items():
        expanded = os.path.expanduser(pattern)
        if glob.glob(expanded) if "*" in expanded else Path(expanded).exists():
            continue
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _real_machine_available(),
    reason="real Claude/Codex/DSH/Antigravity/ZCode installations not found",
)


@pytest.mark.asyncio
async def test_real_status_and_full_model_inventory(tmp_path):
    workspace = tmp_path / "workspace"
    init_git_repo(workspace, {"README.md": "real-machine status fixture\n"})
    runtime = create_mesh_runtime(config=default_config(), workspace_root=workspace)
    try:
        statuses = await runtime.router.list_harnesses(refresh_models=True)
        by_name = {status.harness: status for status in statuses}
        for harness in ("claude_code", "codex", "deepseek", "antigravity", "zcode"):
            status = by_name[harness]
            assert status.available is True, (harness, status.version_or_detail)
            assert status.mode == "cli", (harness, status.mode)
            assert status.binary_path
            assert status.authorized_models, harness

        claude = {model.model_id: model for model in runtime.storage.list_discovered_models("claude_code")}
        assert claude
        active = [model.model_id for model in claude.values() if model.is_active_default]
        assert len(active) <= 1
        tiers = runtime.discovery.get_tier_models("claude_code")
        assert set(tiers) == {"low", "medium", "high", "critical"}
        assert all(model_id in claude for model_id in tiers.values())
        # Rich catalog metadata should surface when the local subscription
        # exposes context/reasoning fields; do not pin vendor model ids here.
        if any(model.context_window for model in claude.values()):
            assert any(model.context_window and model.context_window >= 100_000 for model in claude.values())

        for harness in ("codex", "deepseek", "antigravity", "zcode"):
            models = runtime.storage.list_discovered_models(harness)
            assert models, harness
            assert all(model.authorized for model in models if model.authorized is not None)

        full = await runtime.discovery.discover_all(force_refresh=True)
        assert set(full) == {"claude_code", "codex", "deepseek", "antigravity", "zcode"}
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_real_deepseek_dispatch_creates_file_in_worktree(tmp_path):
    repo = tmp_path / "repo"
    init_git_repo(repo, {"README.md": "real-machine dispatch fixture\n"})
    runtime = create_mesh_runtime(config=default_config(), workspace_root=repo)
    try:
        if not runtime.storage.list_discovered_models("deepseek"):
            await runtime.discovery.discover_for_harness("deepseek", force_refresh=True)
        if not runtime.storage.list_discovered_models("deepseek"):
            pytest.skip("DeepSeek Harness has no discoverable model on this machine")
        request = TaskRequest(
            task_id="real-dsh-task",
            target_harness="deepseek",
            objective=(
                "Use your shell/file tools to create a file named harness_mesh_real_ok.txt "
                "in the current working directory with exactly the text OK. "
                "Do not do anything else, then reply with DONE."
            ),
            difficulty="low",
            workspace_root=str(repo),
            timeout_seconds=300,
        )
        result = await runtime.router.dispatch(request)
        assert result.status == TaskStatus.COMPLETED, result.summary + "\n" + result.raw_stderr
        assert result.worktree_path is not None
        created = Path(result.worktree_path) / "harness_mesh_real_ok.txt"
        assert created.exists(), result.summary
        assert created.read_text(encoding="utf-8").strip() == "OK"
        assert not (repo / "harness_mesh_real_ok.txt").exists()
        applied = runtime.router.apply_task_patch("real-dsh-task")
        assert applied["applied_files"]
        assert (repo / "harness_mesh_real_ok.txt").read_text(encoding="utf-8").strip() == "OK"
    finally:
        await runtime.aclose()
