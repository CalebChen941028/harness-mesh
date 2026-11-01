"""DSH patch validation happens before writes and rolls back."""

from __future__ import annotations

from pathlib import Path

import harness_mesh.installer as installer_module
from harness_mesh.config import default_config
from harness_mesh.installer import HarnessInstaller


def _fake_binary(directory: Path, name: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _installer(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    patch = home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
    patch.parent.mkdir(parents=True)
    patch.write_text("# user comment\n[]\n", encoding="utf-8")
    config = default_config()
    config.harnesses["deepseek"].binary = _fake_binary(tmp_path / "bin", "dsh")
    config.harnesses["deepseek"].binary_paths = []
    installer = HarnessInstaller(
        config,
        workspace,
        home_dir=home,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        which=lambda name: None,
        runner=lambda argv, cwd, env: (0, "", ""),
        config_path=tmp_path / "missing.yaml",
    )
    return installer, patch


def test_validation_failure_does_not_touch_original_file(tmp_path, monkeypatch):
    installer, patch = _installer(tmp_path)
    original = patch.read_text(encoding="utf-8")
    committed: list[Path] = []

    def fail_validation(content):
        raise ValueError("bad patch")

    monkeypatch.setattr(installer_module, "_validate_dsh_patch", fail_validation)
    monkeypatch.setattr(installer_module, "_atomic_write_text", lambda p, c: committed.append(p))
    result = installer.install(["deepseek"])[0]
    assert result.action == "failed"
    assert patch.read_text(encoding="utf-8") == original
    assert committed == []


def test_write_failure_restores_pre_write_content(tmp_path, monkeypatch):
    installer, patch = _installer(tmp_path)
    original = patch.read_text(encoding="utf-8")
    calls = 0

    def broken_write(path: Path, content: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            path.write_text("BROKEN CONTENT", encoding="utf-8")
            raise OSError("disk full")
        path.write_text(content, encoding="utf-8")

    monkeypatch.setattr(installer_module, "_atomic_write_text", broken_write)
    result = installer.install(["deepseek"])[0]
    assert result.action == "failed"
    assert patch.read_text(encoding="utf-8") == original
