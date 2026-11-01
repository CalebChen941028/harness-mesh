"""ZCode must not fall back to its yolo default when approval is withheld."""

from __future__ import annotations

from pathlib import Path

from harness_mesh.adapters.base import AdapterCall
from harness_mesh.adapters.zcode import ZCodeAdapter
from harness_mesh.config import default_config


def _args(tmp_path: Path, config, **call_overrides) -> list[str]:
    adapter = ZCodeAdapter(config, config.harnesses["zcode"], tmp_path, env={}, which=lambda name: None)
    call = AdapterCall(
        task_id="t", assignment_id="a", prompt="p", workdir=tmp_path, model="m", **call_overrides
    )
    return adapter._augment_command(["zcode", "-p", "prompt"], call)


def _mode(args: list[str]) -> str | None:
    return args[args.index("--mode") + 1] if "--mode" in args else None


def test_isolated_worktree_keeps_yolo(tmp_path):
    config = default_config()
    assert _mode(_args(tmp_path, config)) == "yolo"


def test_read_only_uses_plan(tmp_path):
    assert _mode(_args(tmp_path, default_config(), read_only=True)) == "plan"


def test_shared_write_without_opt_in_uses_build(tmp_path):
    config = default_config()
    assert _mode(_args(tmp_path, config, shared_write=True)) == "build"


def test_disabled_auto_approve_uses_build(tmp_path):
    config = default_config()
    config.harnesses["zcode"].auto_approve = False
    assert _mode(_args(tmp_path, config)) == "build"


def test_shared_write_with_opt_in_keeps_yolo(tmp_path):
    config = default_config()
    config.auto_approve_shared_write = True
    assert _mode(_args(tmp_path, config, shared_write=True)) == "yolo"


def test_restricted_mode_can_be_relaxed_to_edit_but_not_to_unknown_values(tmp_path):
    config = default_config()
    config.harnesses["zcode"].extra["restricted_mode"] = "edit"
    assert _mode(_args(tmp_path, config, shared_write=True)) == "edit"
    config.harnesses["zcode"].extra["restricted_mode"] = "yolo"
    assert _mode(_args(tmp_path, config, shared_write=True)) == "build"
    config.harnesses["zcode"].extra["restricted_mode"] = "guarded"
    assert _mode(_args(tmp_path, config, shared_write=True)) == "build"


def test_explicit_mode_in_the_template_is_respected(tmp_path):
    config = default_config()
    adapter = ZCodeAdapter(config, config.harnesses["zcode"], tmp_path, env={}, which=lambda name: None)
    call = AdapterCall(task_id="t", assignment_id="a", prompt="p", workdir=tmp_path, model="m", shared_write=True)
    args = adapter._augment_command(["zcode", "-p", "prompt", "--mode", "edit"], call)
    assert args.count("--mode") == 1
