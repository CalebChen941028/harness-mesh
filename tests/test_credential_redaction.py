"""Credential redaction, ledger permissions and plugin env sanitisation."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from harness_mesh.config import default_config
from harness_mesh.models import AssignmentRecord, DiscoveredModel, PluginSpec
from harness_mesh.security import (
    REDACTED,
    filter_sensitive_paths,
    redact_env,
    redact_text,
)
from harness_mesh.skills import SkillPluginManager
from harness_mesh.storage import StateStore


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------------------
# security.redact_text / redact_env / filter_sensitive_paths
# ---------------------------------------------------------------------------
def test_redact_text_replaces_only_sensitive_env_literals():
    env = {
        "OPENAI_API_KEY": "sk-env-literal-1234567890",
        "DB_PASSWORD": "password-value",
        "PATH": "/usr/bin:/bin",
    }
    text = "key=sk-env-literal-1234567890 password=password-value path=/usr/bin:/bin"
    redacted = redact_text(text, env=env)
    assert "sk-env-literal-1234567890" not in redacted
    assert "password-value" not in redacted
    assert "/usr/bin:/bin" in redacted


def test_redact_text_short_sensitive_values_are_kept():
    redacted = redact_text("value=short", env={"API_KEY": "short"})
    assert redacted == "value=short"


def test_redact_text_covers_high_confidence_credential_shapes():
    secrets = {
        "openai": "sk-" + "a1B2c3D4" * 3,
        "anthropic": "sk-ant-api03-" + "a1B2c3D4" * 3,
        "github": "ghp_" + "A1b2C3d4" * 5,
        "github_pat": "github_pat_" + "A1b2C3d4" * 4,
        "slack": "xoxb-" + "1234567890-abcdefghij",
        "aws": "AKIA" + "A1B2C3D4E5F6G7H8",
        "google": "AIza" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7r",
        "jwt": (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJ1c2VyX2lkIjoiMTIzNDU2Nzg5MCJ9."
            "abc123ABC456def789ghi012"
        ),
    }
    for name, secret in secrets.items():
        redacted = redact_text(f"before {secret} after")
        assert secret not in redacted, name
        assert REDACTED in redacted, name
        assert "before" in redacted and "after" in redacted, name


def test_redact_text_covers_assignments_pem_and_url_passwords():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    text = (
        '{"apiKey": "abcdefgh12345678"} '
        "Authorization: Bearer abcdefgh1234567890 "
        "postgres://user:pass@localhost/db "
        + pem
        + "\ntail"
    )
    redacted = redact_text(text)
    assert "abcdefgh12345678" not in redacted
    assert "Bearer abcdefgh1234567890" not in redacted
    assert "postgres://user:***@localhost/db" in redacted
    assert "BEGIN RSA PRIVATE KEY" not in redacted
    assert "tail" in redacted


def test_redact_text_preserves_ordinary_text_and_is_idempotent():
    text = (
        "task-sensitive-source inputTokens=12345 totalTokens=99 "
        "普通中文保持原样 sk-xxx\n"
        "def load():\n    return api_key_value\n"
    )
    once = redact_text(text)
    assert once == text
    assert redact_text(once) == once


def test_redact_text_handles_empty_and_env_none():
    assert redact_text("") == ""
    assert redact_text("plain text") == "plain text"
    assert redact_text("plain text", env=None) == "plain text"


def test_redact_env_hides_sensitive_names_and_credential_values():
    cleaned = redact_env(
        {
            "GITHUB_TOKEN": "ghp_" + "A1b2C3d4" * 5,
            "NODE_OPTIONS": "--no-warnings",
            "PLAIN": "hello",
            "SERVICE_SECRET": "secret-value",
        }
    )
    assert cleaned["GITHUB_TOKEN"] == REDACTED
    assert cleaned["SERVICE_SECRET"] == REDACTED
    assert cleaned["NODE_OPTIONS"] == "--no-warnings"
    assert cleaned["PLAIN"] == "hello"


def test_filter_sensitive_paths_keeps_order_and_sorts_skipped():
    visible, skipped = filter_sensitive_paths(
        [
            "src/main.py",
            ".env",
            Path("id_rsa"),
            ".ssh/config",
            "README.md",
            "src/main.py",
            ".env",
        ]
    )
    assert visible == ["src/main.py", "README.md"]
    assert skipped == [".env", ".ssh/config", "id_rsa"]


# ---------------------------------------------------------------------------
# StateStore permissions and legacy secret cleanup
# ---------------------------------------------------------------------------
def test_state_store_hardens_directory_and_ledger_permissions(tmp_path):
    state_dir = tmp_path / "workspace" / ".harness-mesh"
    db_path = state_dir / "state.db"
    store = StateStore(db_path)
    try:
        assert _mode(state_dir) == 0o700
        assert _mode(db_path) == 0o600
        for suffix in ("-wal", "-shm"):
            side = Path(f"{db_path}{suffix}")
            if side.exists():
                assert _mode(side) == 0o600, suffix
    finally:
        store.close()


def test_state_store_tightens_pre_existing_loose_files(tmp_path):
    state_dir = tmp_path / ".harness-mesh"
    db_path = state_dir / "state.db"
    StateStore(db_path).close()
    os.chmod(state_dir, 0o755)
    os.chmod(db_path, 0o644)
    store = StateStore(db_path)
    try:
        assert _mode(state_dir) == 0o700
        assert _mode(db_path) == 0o600
    finally:
        store.close()


def test_sanitize_legacy_secrets_rewrites_provider_blobs(tmp_path):
    store = StateStore(tmp_path / "state.db")
    try:
        store.save_discovered_models(
            [
                DiscoveredModel(
                    harness="zcode",
                    model_id="m1",
                    provider="{'apiKey': 'LEGACY-MARKER-7c1d'}",
                    source="local_config",
                ),
                DiscoveredModel(
                    harness="codex",
                    model_id="m2",
                    provider="{x}",
                    source="local_config",
                ),
                DiscoveredModel(
                    harness="deepseek",
                    model_id="m3",
                    provider="deepseek",
                    source="local_config",
                ),
            ]
        )
        assert store.sanitize_legacy_secrets() == 2
        by_id = {model.model_id: model for model in store.list_discovered_models()}
        assert by_id["m1"].provider == "zcode"
        assert by_id["m2"].provider == "codex"
        assert by_id["m3"].provider == "deepseek"
        # Re-running on an already repaired ledger is a no-op.
        assert store.sanitize_legacy_secrets() == 0
    finally:
        store.close()


def test_reopening_a_legacy_ledger_purges_secret_bytes(tmp_path):
    state_dir = tmp_path / ".harness-mesh"
    db_path = state_dir / "state.db"
    marker = "LEGACY-MARKER-7c1d"
    store = StateStore(db_path)
    try:
        store.save_discovered_models(
            [
                DiscoveredModel(
                    harness="zcode",
                    model_id="m1",
                    provider="{'apiKey': '" + marker + "'}",
                    source="local_config",
                )
            ]
        )
    finally:
        store.close()
    # The repair runs from __init__ on the next open, then checkpoint+vacuum.
    StateStore(db_path).close()
    raw = b"".join(
        path.read_bytes()
        for path in (db_path, Path(f"{db_path}-wal"))
        if path.exists()
    )
    assert marker.encode() not in raw


def test_count_running_assignments_for_task(tmp_path):
    store = StateStore(tmp_path / "state.db")
    try:
        store.add_assignment(
            AssignmentRecord(task_id="task-a", attempt=1, harness="codex", model="m", status="running")
        )
        store.add_assignment(
            AssignmentRecord(task_id="task-a", attempt=2, harness="codex", model="m", status="completed")
        )
        store.add_assignment(
            AssignmentRecord(task_id="task-b", attempt=1, harness="codex", model="m", status="running")
        )
        assert store.count_running_assignments_for_task("task-a") == 1
        assert store.count_running_assignments_for_task("task-b") == 1
        assert store.count_running_assignments_for_task("task-missing") == 0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# SkillPluginManager: plugin env redaction and private MCP configs
# ---------------------------------------------------------------------------
def _manager(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return SkillPluginManager(
        default_config(), root, home_dir=home, env={"HOME": str(home)}
    )


def test_compile_mcp_config_keeps_env_and_is_owner_only(tmp_path):
    manager = _manager(tmp_path)
    token = "ghp_" + "A1b2C3d4" * 5
    plugin = PluginSpec(
        name="demo",
        command="npx",
        args=["-y", "server"],
        env={
            "GITHUB_TOKEN": token,
            "NODE_OPTIONS": "--no-warnings",
        },
    )
    path = manager.compile_mcp_config("task_demo", [plugin])
    assert path is not None
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    payload = json.loads(path.read_text(encoding="utf-8"))
    env = payload["mcpServers"]["demo"]["env"]
    # The generated config is how the worker starts the plugin: env must stay
    # verbatim or the plugin loses its credentials.
    assert env["GITHUB_TOKEN"] == token
    assert env["NODE_OPTIONS"] == "--no-warnings"


def test_compile_mcp_config_keeps_extra_server_env(tmp_path):
    manager = _manager(tmp_path)
    path = manager.compile_mcp_config(
        "task_extra",
        [],
        extra_servers={
            "extra": {
                "command": "npx",
                "env": {"SERVICE_SECRET": "secret-value", "PLAIN": "hello"},
            }
        },
    )
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["mcpServers"]["extra"]["env"]["SERVICE_SECRET"] == "secret-value"
    assert payload["mcpServers"]["extra"]["env"]["PLAIN"] == "hello"


def test_list_available_redacts_plugin_env(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    plugin = PluginSpec(
        name="demo",
        command="npx",
        env={"API_KEY": "sk-" + "a1B2c3D4" * 3, "PLAIN": "hello"},
        server_config={
            "command": "npx",
            "env": {"API_KEY": "sk-" + "a1B2c3D4" * 3, "PLAIN": "hello"},
        },
    )
    monkeypatch.setattr(manager, "scan_skills", lambda force=False: [])
    monkeypatch.setattr(manager, "scan_plugins", lambda force=False: [plugin])
    payload = manager.list_available()
    assert payload["plugins"][0]["env"]["API_KEY"] == REDACTED
    assert payload["plugins"][0]["env"]["PLAIN"] == "hello"
    # The raw .mcp.json entry returned to MCP/CLI must not leak the same env.
    assert payload["plugins"][0]["server_config"]["env"]["API_KEY"] == REDACTED
    assert payload["plugins"][0]["server_config"]["env"]["PLAIN"] == "hello"
