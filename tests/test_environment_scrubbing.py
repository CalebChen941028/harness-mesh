"""Environment scrubbing covers credential-shaped variables."""

from __future__ import annotations

from harness_mesh.security import scrub_subprocess_env


def test_scrub_pattern_table_and_exact_names():
    cleaned = scrub_subprocess_env(
        {
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp/home",
            "LANG": "C.UTF-8",
            "HARNESS_MESH_TASK_ID": "task-1",
            "AWS_ACCESS_KEY_ID": "AKIA-should-hide",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/google.json",
            "DATABASE_URL": "postgres://user:pass@localhost/db",
            "MY_PRIVATE_KEY": "private",
            "GH_PAT": "pat-value",
            "NPM_CONFIG__AUTH": "auth-value",
            "SERVICE_CREDENTIAL": "cred",
            "SOME_PASSWD": "passwd",
            "API_KEY": "api-key",
            "NORMAL_VALUE": "kept",
        }
    )
    for hidden in (
        "AWS_ACCESS_KEY_ID",
        "SSH_AUTH_SOCK",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "DATABASE_URL",
        "MY_PRIVATE_KEY",
        "GH_PAT",
        "NPM_CONFIG__AUTH",
        "SERVICE_CREDENTIAL",
        "SOME_PASSWD",
        "API_KEY",
    ):
        assert hidden not in cleaned, hidden
    for kept in ("PATH", "HOME", "LANG", "HARNESS_MESH_TASK_ID", "NORMAL_VALUE"):
        assert cleaned[kept], kept


def test_url_embedded_password_is_removed_even_with_innocent_name():
    cleaned = scrub_subprocess_env({"CONNECTION": "mysql://admin:secret@db.local/app"})
    assert "CONNECTION" not in cleaned
