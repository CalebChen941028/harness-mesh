"""Machines that only have some of the five harnesses installed."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import harness_mesh.cli as cli_module
from harness_mesh.config import default_config
from harness_mesh.installer import HarnessInstaller
from harness_mesh.market import MarketModel
from harness_mesh.models import DiscoveredModel, TaskRequest, TaskStatus
from harness_mesh.quota import QuotaErrorDetector, QuotaGuardian
from harness_mesh.router import NO_HARNESS_MESSAGE
from tests.helpers import FakeAdapter, failure, make_mesh, success

ALL_HARNESSES = ("claude_code", "codex", "deepseek", "antigravity", "zcode")
INSTALLED_SETS = [
    frozenset(),
    frozenset({"codex"}),
    frozenset({"claude_code", "deepseek"}),
    frozenset(ALL_HARNESSES),
]
INSTALLED_IDS = ["none", "codex", "claude_and_deepseek", "all"]
HARNESS_MODELS = {
    "claude_code": "claude-opus-5-5",
    "codex": "gpt-6-astra",
    "deepseek": "deepseek-v4.1-flash",
    "antigravity": "gemini-4-argon",
    "zcode": "glm-5.3",
}


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _market_model(harness: str) -> MarketModel:
    model_id = HARNESS_MODELS[harness]
    return MarketModel(
        key=f"vendor/{model_id}",
        vendor="vendor",
        name=model_id,
        input_per_mtok=1.0,
        output_per_mtok=3.0,
        intelligence=70.0,
        intelligence_source="epoch_ai",
        intelligence_reasoning=70.0,
        intelligence_coding=70.0,
        benchmarks_used=["gpqa_diamond"],
        benchmark_data_date="2026-10-26",
        sources=["epoch"],
    )


def _seed(mesh) -> None:
    now = time.time()
    catalog = [_market_model(harness) for harness in ALL_HARNESSES]
    mesh.storage.save_market_snapshot(
        "epoch",
        now,
        {"models": [model.model_dump(mode="json") for model in catalog], "fetched_at": now},
    )
    mesh.storage.save_discovered_models(
        [
            DiscoveredModel(
                harness=harness,
                model_id=HARNESS_MODELS[harness],
                source="local_config",
                capability_score=50.0,
                discovered_at=now,
            )
            for harness in ALL_HARNESSES
        ]
    )


def _mesh(tmp_path, installed: frozenset[str]):
    adapters = {
        name: FakeAdapter(name, lambda adapter, call: success("done")) for name in installed
    }
    mesh = make_mesh(tmp_path, adapters=adapters)
    _seed(mesh)
    return mesh


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", INSTALLED_SETS, ids=INSTALLED_IDS)
async def test_status_hides_models_of_uninstalled_harnesses(tmp_path, installed):
    mesh = _mesh(tmp_path, installed)
    try:
        statuses = await mesh.router.list_harnesses()
        assert {status.harness for status in statuses} == set(ALL_HARNESSES)
        for status in statuses:
            if status.harness in installed:
                assert status.installed is True
                assert status.authorized_models
                assert status.model_details
            else:
                assert status.installed is False
                assert status.authorized_models == []
                assert status.tier_models == {}
                assert status.model_details == []
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", INSTALLED_SETS, ids=INSTALLED_IDS)
async def test_dispatch_auto_only_uses_installed_harnesses(tmp_path, installed):
    mesh = _mesh(tmp_path, installed)
    try:
        result = await mesh.router.dispatch(
            TaskRequest(
                objective="Implement the flag",
                task_kind="implement",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        if not installed:
            assert result.status == TaskStatus.FAILED, result.summary
            assert result.summary == NO_HARNESS_MESSAGE
            assert result.error_detail == NO_HARNESS_MESSAGE
        else:
            assert result.status == TaskStatus.COMPLETED, result.summary
            assert result.initial_harness in installed
            assert result.target_harness in installed
            assert {assignment.harness for assignment in result.assignments} <= installed
    finally:
        mesh.storage.close()


def test_status_cli_prints_not_installed_for_missing_harnesses(tmp_path, monkeypatch):
    mesh = _mesh(tmp_path, frozenset({"codex"}))
    monkeypatch.setattr(cli_module, "_runtime", lambda ctx: mesh)
    try:
        output = _plain(CliRunner().invoke(cli_module.app, ["status"]).output)
        assert "not installed" in output
        assert "codex" in output
        assert "gpt-6-astra" in output
        # A missing harness never lists its model or tier map.
        for harness in ("claude_code", "deepseek", "antigravity", "zcode"):
            assert HARNESS_MODELS[harness] not in output
    finally:
        pass


def test_models_cli_reports_missing_harnesses_without_models(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli_module, "_runtime", lambda ctx: _mesh(tmp_path, frozenset({"codex"}))
    )
    try:
        payload = json.loads(
            _plain(
                CliRunner().invoke(
                    cli_module.app, ["models", "--harness", "claude_code", "--json"]
                ).output
            )
        )
        assert payload["installed"] is False
        assert payload["models"] == []
        assert payload["tier_models"] == {}
        text = _plain(
            CliRunner().invoke(
                cli_module.app, ["models", "--harness", "claude_code"]
            ).output
        )
        assert "not installed" in text
    finally:
        pass


@pytest.mark.asyncio
async def test_a_single_installed_harness_wins_every_task_kind(tmp_path):
    mesh = _mesh(tmp_path, frozenset({"codex"}))
    try:
        for kind in ("plan", "implement", "docs"):
            result = await mesh.router.dispatch(
                TaskRequest(
                    objective=f"Do the {kind} work",
                    task_kind=kind,
                    workspace_root=str(mesh.workspace_root),
                    timeout_seconds=30,
                )
            )
            assert result.status == TaskStatus.COMPLETED, result.summary
            assert result.initial_harness == "codex"
            assert result.target_harness == "codex"
    finally:
        mesh.storage.close()


@pytest.mark.asyncio
async def test_all_models_of_the_only_harness_blocked_waits_for_quota(tmp_path):
    mesh = _mesh(tmp_path, frozenset({"codex"}))
    try:
        mesh.guardian.record_exhaustion("codex", "gpt-6-astra", "usage_limit", is_account_wide=True)
        result = await mesh.router.dispatch(
            TaskRequest(
                objective="Implement the flag",
                task_kind="implement",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.WAITING_FOR_QUOTA, result.summary
        record = mesh.storage.get_task(result.task_id)
        assert record["status"] == TaskStatus.WAITING_FOR_QUOTA.value
        assert record["blocked_harness"] == "codex"
    finally:
        mesh.storage.close()


# ---------------------------------------------------------------------------
# quota classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "You've hit your usage limit. Try again later.",
        "usage_limit_reached",
        "insufficient_quota",
    ],
)
def test_account_level_quota_errors_open_the_whole_harness(message):
    quota, account_wide, reason, _ = QuotaErrorDetector.detect_quota_exhaustion(
        stderr=message
    )
    assert quota is True
    assert account_wide is True
    assert reason in {"usage_limit", "insufficient_quota"}

    store = _guardian_store()
    try:
        guardian = QuotaGuardian(store)
        guardian.record_exhaustion("codex", "gpt-6-astra", reason, is_account_wide=True)
        assert guardian.is_available("codex", "gpt-6-astra") is False
        assert guardian.is_available("codex", "gpt-6-astra-mini") is False
        assert guardian.is_available("claude_code", "claude-opus-5-5") is True
    finally:
        store.close()


def test_model_level_quota_errors_only_open_one_model():
    quota, account_wide, reason, _ = QuotaErrorDetector.detect_quota_exhaustion(
        stderr="HTTP 429 too many requests: rate_limit_exceeded"
    )
    assert quota is True
    assert account_wide is False

    store = _guardian_store()
    try:
        guardian = QuotaGuardian(store)
        guardian.record_exhaustion("codex", "gpt-6-astra", reason, is_account_wide=False)
        assert guardian.is_available("codex", "gpt-6-astra") is False
        assert guardian.is_available("codex", "gpt-6-astra-mini") is True
    finally:
        store.close()


def _guardian_store():
    import tempfile

    from harness_mesh.storage import StateStore

    directory = tempfile.mkdtemp()
    return StateStore(Path(directory) / "state.db")


@pytest.mark.asyncio
async def test_usage_limit_on_one_model_fails_over_instead_of_burning_the_harness(tmp_path):
    codex = FakeAdapter(
        "codex",
        lambda adapter, call: failure(
            "You've hit your usage limit.", exit_code=1
        ),
    )
    claude = FakeAdapter("claude_code", lambda adapter, call: success("claude did it"))
    mesh = make_mesh(tmp_path, adapters={"codex": codex, "claude_code": claude})
    _seed(mesh)
    try:
        result = await mesh.router.dispatch(
            TaskRequest(
                objective="Implement the flag",
                target_harness="codex",
                task_kind="implement",
                workspace_root=str(mesh.workspace_root),
                timeout_seconds=30,
            )
        )
        assert result.status == TaskStatus.COMPLETED, result.summary
        assert len(codex.calls) == 1
        assert len(claude.calls) == 1
        assert all(
            assignment.harness != "codex" or assignment.status == "ended_quota"
            for assignment in result.assignments
        )
    finally:
        mesh.storage.close()


# ---------------------------------------------------------------------------
# installer
# ---------------------------------------------------------------------------


def _installer(tmp_path, *, which=None, dry_run=False, extra_paths=None):
    config = default_config()
    config.harnesses["zcode"].extra.update(
        extra_paths
        or {
            "macos_electron_bin": str(tmp_path / "missing" / "ZCode"),
            "macos_cjs_entry": str(tmp_path / "missing" / "zcode.cjs"),
        }
    )
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir(exist_ok=True)
    workspace.mkdir(exist_ok=True)
    return HarnessInstaller(
        config,
        workspace,
        home_dir=home,
        which=which or (lambda name: None),
        dry_run=dry_run,
        allow_system_paths=False,
    )


def test_install_dry_run_skips_the_missing_harnesses(tmp_path):
    installer = _installer(tmp_path, dry_run=True)
    results = installer.install(["claude_code", "codex", "antigravity"])
    assert [result.action for result in results] == ["skipped", "skipped", "skipped"]
    assert all("binary not found" in result.detail for result in results)


def test_zcode_without_binary_or_app_is_skipped_and_writes_nothing(tmp_path):
    installer = _installer(tmp_path, dry_run=False)
    result = installer.install(["zcode"])[0]
    assert result.action == "skipped"
    assert result.detail == "ZCode not found"
    assert not (installer.state_dir / "zcode-plugin").exists()


def test_zcode_with_an_app_but_no_cli_still_generates_the_plugin(tmp_path):
    electron = tmp_path / "ZCode"
    electron.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cjs = tmp_path / "zcode.cjs"
    cjs.write_text("// entry\n", encoding="utf-8")
    installer = _installer(
        tmp_path,
        dry_run=False,
        extra_paths={
            "macos_electron_bin": str(electron),
            "macos_cjs_entry": str(cjs),
        },
    )
    result = installer.install(["zcode"])[0]
    assert result.action in {"ready", "installed"}
    assert (installer.state_dir / "zcode-plugin").exists()


def test_doctor_reports_how_many_harnesses_are_installed(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "harnesses:\n"
        "  zcode:\n"
        "    extra:\n"
        "      macos_electron_bin: /nonexistent/ZCode\n"
        "      macos_cjs_entry: /nonexistent/zcode.cjs\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "DSH_HOME": str(home / ".dsh"),
        "PATH": str(tmp_path / "empty-bin"),
        "HARNESS_MESH_DISABLE_SYSTEM_BINARIES": "1",
    }
    result = CliRunner().invoke(
        cli_module.app,
        ["--workspace", str(workspace), "--config", str(config_path), "doctor"],
        env=env,
    )
    output = _plain(result.output)
    assert "0 of 5 harnesses installed" in output


# ---------------------------------------------------------------------------
# router message
# ---------------------------------------------------------------------------


def test_no_harness_message_is_the_documented_sentence():
    assert NO_HARNESS_MESSAGE == (
        "No supported harness found. Install one of: Claude Code, Codex, "
        "DeepSeek Harness, Antigravity, ZCode, then run hmesh install."
    )
