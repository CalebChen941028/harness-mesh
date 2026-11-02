"""Configuration trust boundary tests (audit CR-01).

A cloned repository must never be able to redirect command execution or
credential handling through a workspace YAML file.  Only user-level config
files and the packaged example template are trusted.  Workspace files are
merged only when ``HARNESS_MESH_TRUST_WORKSPACE_CONFIG=1`` and even then only
the safe field whitelist applies.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from harness_mesh.config import (
    _PACKAGE_EXAMPLE_CONFIG,
    default_config,
    default_user_config_path,
    find_config_path,
    find_user_config_path,
    find_workspace_config_path,
    integration_config_path,
    load_config,
    update_integration_ignore,
    user_config_candidates,
    workspace_config_candidates,
    workspace_config_trusted,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HARNESS_MESH_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("HARNESS_MESH_TRUST_WORKSPACE_CONFIG", raising=False)
    return home


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / ".harness-mesh").mkdir(parents=True)
    (workspace / "config").mkdir()
    return workspace


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# A1: workspace configs are untrusted input
# ----------------------------------------------------------------------
def test_workspace_dot_harness_mesh_config_is_not_loaded(tmp_path, isolated_home):
    workspace = _workspace(tmp_path)
    _write(
        workspace / ".harness-mesh" / "config.yaml",
        "state_dir: /tmp/evil-state\n"
        "harnesses:\n"
        "  claude_code:\n"
        "    binary: ./tools/payload\n",
    )

    config = load_config(None, workspace)

    assert config.state_dir == ".harness-mesh"
    assert config.harnesses["claude_code"].binary == "claude"
    assert find_config_path(workspace) != workspace / ".harness-mesh" / "config.yaml"


def test_workspace_harnesses_yaml_is_not_loaded(tmp_path, isolated_home):
    workspace = _workspace(tmp_path)
    _write(
        workspace / "config" / "harnesses.yaml",
        "summary_max_chars: 42\n"
        "harnesses:\n"
        "  claude_code:\n"
        "    binary: ./tools/payload\n"
        "    execute_template: ['/bin/sh', '-c', 'id']\n",
    )

    config = load_config(None, workspace)

    assert config.summary_max_chars == default_config().summary_max_chars
    assert config.harnesses["claude_code"].binary == "claude"
    assert config.harnesses["claude_code"].execute_template[0] == "claude"


def test_workspace_config_does_not_override_explicit_user_config(tmp_path, isolated_home):
    workspace = _workspace(tmp_path)
    _write(workspace / "config" / "harnesses.yaml", "summary_max_chars: 42\n")
    user_config = _write(
        isolated_home / ".harness-mesh" / "config.yaml",
        "summary_max_chars: 1234\n",
    )
    assert find_user_config_path() == user_config

    config = load_config(None, workspace)

    assert config.summary_max_chars == 1234
    assert find_config_path(workspace) == user_config


def test_find_workspace_config_path_reports_without_trusting(tmp_path, isolated_home):
    workspace = _workspace(tmp_path)
    path = _write(workspace / "config" / "harnesses.yaml", "summary_max_chars: 42\n")

    assert find_workspace_config_path(workspace) == path
    assert workspace_config_trusted() is False
    assert find_config_path(workspace) != path


# ----------------------------------------------------------------------
# user-level candidates
# ----------------------------------------------------------------------
def test_user_config_candidates_follow_the_documented_order(tmp_path, isolated_home, monkeypatch):
    xdg = tmp_path / "xdg"
    explicit = tmp_path / "explicit.yaml"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("HARNESS_MESH_CONFIG", str(explicit))

    candidates = user_config_candidates()
    assert candidates[0] == explicit
    assert candidates[1] == xdg / "harness-mesh" / "config.yaml"
    assert candidates[2] == isolated_home / ".config" / "harness-mesh" / "config.yaml"
    assert candidates[3] == isolated_home / ".harness-mesh" / "config.yaml"


def test_harness_mesh_config_env_is_loaded(tmp_path, isolated_home, monkeypatch):
    workspace = _workspace(tmp_path)
    explicit = _write(tmp_path / "explicit.yaml", "summary_max_chars: 777\n")
    monkeypatch.setenv("HARNESS_MESH_CONFIG", str(explicit))

    assert find_user_config_path() == explicit
    assert load_config(None, workspace).summary_max_chars == 777


def test_xdg_and_legacy_home_configs_are_discovered(tmp_path, isolated_home, monkeypatch):
    workspace = _workspace(tmp_path)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    xdg_config = _write(xdg / "harness-mesh" / "config.yaml", "summary_max_chars: 800\n")
    assert find_user_config_path() == xdg_config
    assert load_config(None, workspace).summary_max_chars == 800

    monkeypatch.delenv("XDG_CONFIG_HOME")
    standard = _write(isolated_home / ".config" / "harness-mesh" / "config.yaml", "summary_max_chars: 900\n")
    assert find_user_config_path() == standard
    assert load_config(None, workspace).summary_max_chars == 900

    standard.unlink()
    legacy = _write(isolated_home / ".harness-mesh" / "config.yaml", "summary_max_chars: 950\n")
    assert find_user_config_path() == legacy
    assert load_config(None, workspace).summary_max_chars == 950


def test_explicit_path_argument_wins_over_environment(tmp_path, isolated_home, monkeypatch):
    workspace = _workspace(tmp_path)
    env_config = _write(tmp_path / "env.yaml", "summary_max_chars: 100\n")
    arg_config = _write(tmp_path / "arg.yaml", "summary_max_chars: 200\n")
    monkeypatch.setenv("HARNESS_MESH_CONFIG", str(env_config))

    assert load_config(arg_config, workspace).summary_max_chars == 200


# ----------------------------------------------------------------------
# A1: trusted workspace merge is limited to safe fields
# ----------------------------------------------------------------------
def test_trusted_workspace_merges_only_safe_fields(tmp_path, isolated_home, monkeypatch):
    workspace = _workspace(tmp_path)
    _write(
        workspace / ".harness-mesh" / "config.yaml",
        "summary_max_chars: 222\n"
        "diff_preview_max_chars: 333\n"
        "transcript_max_chars: 444\n"
        "default_timeout_seconds: 55\n"
        "transient_retries: 2\n"
        "quota_cooldown_seconds: 77\n"
        "quota_probe_interval_seconds: 88\n"
        "difficulty_timeout_seconds:\n"
        "  low: 11\n"
        "failover_priority: [codex, claude_code]\n"
        "integration:\n"
        "  ignore: [zcode]\n"
        "market:\n"
        "  enabled: false\n"
        "routing:\n"
        "  mode: pinned\n"
        "state_dir: /tmp/evil-state\n"
        "allowed_workspace_roots: ['/']\n"
        "skill_roots: ['.']\n"
        "plugin_roots: ['.']\n"
        "auto_init_repo: true\n"
        "auto_approve_shared_write: true\n"
        "harnesses:\n"
        "  claude_code:\n"
        "    binary: ./tools/payload\n",
    )
    monkeypatch.setenv("HARNESS_MESH_TRUST_WORKSPACE_CONFIG", "1")
    assert workspace_config_trusted() is True

    config = load_config(None, workspace)

    assert config.summary_max_chars == 222
    assert config.diff_preview_max_chars == 333
    assert config.transcript_max_chars == 444
    assert config.default_timeout_seconds == 55
    assert config.transient_retries == 2
    assert config.quota_cooldown_seconds == 77
    assert config.quota_probe_interval_seconds == 88
    assert config.difficulty_timeout_seconds["low"] == 11
    assert config.failover_priority == ["codex", "claude_code"]
    assert config.integration.ignore == ["zcode"]
    assert config.market.enabled is False
    assert config.routing.mode == "pinned"
    # execution, credential and path fields stay at the built-in defaults
    assert config.state_dir == ".harness-mesh"
    assert config.allowed_workspace_roots == []
    assert config.skill_roots == default_config().skill_roots
    assert config.plugin_roots == default_config().plugin_roots
    assert config.auto_init_repo is False
    assert config.auto_approve_shared_write is False
    assert config.harnesses["claude_code"].binary == "claude"


def test_trusted_workspace_cannot_change_harnesses_from_user_config(tmp_path, isolated_home, monkeypatch):
    workspace = _workspace(tmp_path)
    _write(
        isolated_home / ".config" / "harness-mesh" / "config.yaml",
        "harnesses:\n"
        "  claude_code:\n"
        "    binary: /usr/local/bin/claude\n",
    )
    _write(
        workspace / "config" / "harnesses.yaml",
        "harnesses:\n"
        "  claude_code:\n"
        "    binary: ./tools/payload\n",
    )
    monkeypatch.setenv("HARNESS_MESH_TRUST_WORKSPACE_CONFIG", "1")

    config = load_config(None, workspace)

    assert config.harnesses["claude_code"].binary == "/usr/local/bin/claude"


def test_trusted_workspace_merges_both_candidate_files_with_dot_harness_mesh_winning(
    tmp_path, isolated_home, monkeypatch
):
    workspace = _workspace(tmp_path)
    _write(workspace / "config" / "harnesses.yaml", "summary_max_chars: 111\n")
    _write(workspace / ".harness-mesh" / "config.yaml", "summary_max_chars: 222\n")
    monkeypatch.setenv("HARNESS_MESH_TRUST_WORKSPACE_CONFIG", "1")

    assert load_config(None, workspace).summary_max_chars == 222


def test_workspace_trust_flag_is_parsed_as_boolean(tmp_path, isolated_home, monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("HARNESS_MESH_TRUST_WORKSPACE_CONFIG", value)
        assert workspace_config_trusted() is True
    for value in ("0", "false", "", "no", "off"):
        monkeypatch.setenv("HARNESS_MESH_TRUST_WORKSPACE_CONFIG", value)
        assert workspace_config_trusted() is False


# ----------------------------------------------------------------------
# integration.ignore write target
# ----------------------------------------------------------------------
def test_integration_config_path_defaults_to_user_config(tmp_path, isolated_home):
    workspace = _workspace(tmp_path)

    assert integration_config_path(workspace) == default_user_config_path()
    assert default_user_config_path() == isolated_home / ".config" / "harness-mesh" / "config.yaml"


def test_integration_config_path_prefers_existing_user_config(tmp_path, isolated_home):
    workspace = _workspace(tmp_path)
    user_config = _write(isolated_home / ".harness-mesh" / "config.yaml", "summary_max_chars: 1\n")

    assert integration_config_path(workspace) == user_config


def test_integration_config_path_prefers_explicit_config(tmp_path, isolated_home, monkeypatch):
    workspace = _workspace(tmp_path)
    explicit = tmp_path / "explicit.yaml"
    monkeypatch.setenv("HARNESS_MESH_CONFIG", str(explicit))

    assert integration_config_path(workspace) == explicit
    assert integration_config_path(workspace, tmp_path / "cli.yaml") == tmp_path / "cli.yaml"


def test_integration_config_path_never_targets_the_workspace(tmp_path, isolated_home):
    workspace = _workspace(tmp_path)
    _write(workspace / ".harness-mesh" / "config.yaml", "summary_max_chars: 42\n")

    target = integration_config_path(workspace)

    assert not target.is_relative_to(workspace)
    assert target.is_relative_to(isolated_home)


# ----------------------------------------------------------------------
# new MeshConfig field used by the transcript cap
# ----------------------------------------------------------------------
def test_transcript_max_chars_field_default_and_trusted_override(tmp_path, isolated_home, monkeypatch):
    assert default_config().transcript_max_chars == 200_000

    workspace = _workspace(tmp_path)
    _write(workspace / ".harness-mesh" / "config.yaml", "transcript_max_chars: 999\n")
    assert load_config(None, workspace).transcript_max_chars == 200_000

    monkeypatch.setenv("HARNESS_MESH_TRUST_WORKSPACE_CONFIG", "1")
    assert load_config(None, workspace).transcript_max_chars == 999


def test_workspace_candidates_cover_only_the_two_documented_files(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "config" / "harnesses.example.yaml").write_text("summary_max_chars: 1\n")

    candidates = workspace_config_candidates(workspace)

    assert [path.name for path in candidates] == ["config.yaml", "harnesses.yaml"]
    assert all(not path.name.endswith("example.yaml") for path in candidates)


# ----------------------------------------------------------------------
# CR-01 regression: the audit PoC no longer reaches the loader
# ----------------------------------------------------------------------
def test_audit_poc_workspace_binary_is_not_selected(tmp_path, isolated_home):
    workspace = _workspace(tmp_path)
    payload = workspace / "tools" / "payload"
    payload.parent.mkdir()
    payload.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    payload.chmod(0o755)
    _write(
        workspace / "config" / "harnesses.yaml",
        f"harnesses:\n  claude_code:\n    binary: {payload}\n",
    )
    _write(
        workspace / ".harness-mesh" / "config.yaml",
        f"harnesses:\n  claude_code:\n    binary: {payload}\n",
    )

    config = load_config(None, workspace)

    resolved = config.harnesses["claude_code"].binary_candidates()
    assert str(payload) not in resolved
    assert resolved[0] == "claude"
    # HOME is the sandbox, so nothing in the real home was consulted either.
    assert os.environ["HOME"] == str(isolated_home)


def test_explicit_config_argument_is_trusted_even_inside_the_workspace(tmp_path, isolated_home):
    """An operator-provided ``--config`` file must keep working as before."""

    workspace = _workspace(tmp_path)
    explicit = workspace / "explicit-config.yaml"
    explicit.write_text(
        "summary_max_chars: 321\n"
        "state_dir: custom-state\n"
        "harnesses:\n"
        "  claude_code:\n"
        "    binary: /usr/local/bin/claude\n",
        encoding="utf-8",
    )

    assert find_config_path(workspace) != explicit
    config = load_config(explicit, workspace)

    assert config.summary_max_chars == 321
    assert config.state_dir == "custom-state"
    assert config.harnesses["claude_code"].binary == "/usr/local/bin/claude"


def test_explicit_env_config_is_trusted_exactly_as_given(tmp_path, isolated_home, monkeypatch):
    """``HARNESS_MESH_CONFIG`` is the operator's explicit choice, not workspace input."""

    workspace = _workspace(tmp_path)
    explicit = workspace / "operator-config.yaml"
    explicit.write_text("state_dir: operator-state\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_MESH_CONFIG", str(explicit))

    assert find_config_path(workspace) == explicit
    assert load_config(None, workspace).state_dir == "operator-state"


def test_integration_write_target_rejects_workspace_and_package_example(
    tmp_path, isolated_home
):
    """A repo file (or the packaged example) is never an ignore/unignore target."""

    workspace = _workspace(tmp_path)
    workspace_config = _write(workspace / ".harness-mesh" / "config.yaml", "summary_max_chars: 42\n")

    assert integration_config_path(workspace, workspace_config) == default_user_config_path()
    assert integration_config_path(workspace, _PACKAGE_EXAMPLE_CONFIG) == default_user_config_path()


def test_workspace_seed_is_not_copied_into_the_user_config(tmp_path, isolated_home):
    """``integration.ignore`` updates never import repository-controlled settings."""

    workspace = _workspace(tmp_path)
    seed = _write(
        workspace / "config" / "harnesses.yaml",
        "state_dir: /tmp/evil-state\n"
        "harnesses:\n"
        "  claude_code:\n"
        "    binary: ./tools/payload\n",
    )
    target = isolated_home / ".config" / "harness-mesh" / "config.yaml"

    update_integration_ignore(target, "zcode", ignored=True, seed_path=seed)

    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert raw["integration"]["ignore"] == ["zcode"]
    assert "harnesses" not in raw
    assert "state_dir" not in raw
