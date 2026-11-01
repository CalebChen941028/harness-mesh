"""Round 2 WP-R8: context source provenance, migration and delimiter escaping."""

from __future__ import annotations

import sqlite3

import pytest

from harness_mesh.context import escape_delimiters
from harness_mesh.mcp_server import create_server
from harness_mesh.models import TaskRequest
from harness_mesh.storage import StateStore
from tests.helpers import make_mesh


def test_old_context_table_migrates_with_controller_source(tmp_path):
    db_path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE project_context_entries (
            key TEXT PRIMARY KEY,
            category TEXT NOT NULL DEFAULT 'note',
            content TEXT NOT NULL,
            author_harness TEXT NOT NULL DEFAULT 'controller',
            task_id TEXT,
            updated_at REAL NOT NULL
        );
        INSERT INTO project_context_entries (key, category, content, author_harness, task_id, updated_at)
        VALUES ('legacy.key', 'note', 'legacy', 'controller', NULL, 1.0);
        """
    )
    conn.commit()
    conn.close()

    store = StateStore(db_path)
    try:
        entry = store.get_project_context("legacy.key")
        assert entry is not None and entry["source"] == "controller"
        updated = store.upsert_project_context("worker.key", "value", source="worker")
        assert updated["source"] == "worker"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_mcp_worker_source_enters_unverified_block(tmp_path, monkeypatch):
    mesh = make_mesh(tmp_path)
    try:
        request = TaskRequest(
            task_id="worker-context",
            target_harness="claude_code",
            objective="worker",
            workspace_root=str(mesh.workspace_root),
        )
        mesh.storage.create_task(request)
        info = mesh.workspace.create_worktree("worker-context", sync_uncommitted=False)
        monkeypatch.chdir(info.path)
        monkeypatch.delenv("HARNESS_MESH_TASK_ID", raising=False)
        server = create_server(mesh.router)
        result = await server.call_tool(
            "update_project_context",
            {
                "key": "worker.fact",
                "content": "worker generated fact",
                "category": "note",
                "author_harness": "codex",
            },
        )
        assert result
        stored = mesh.storage.get_project_context("worker.fact")
        assert stored is not None and stored["source"] == "worker"
        memory = mesh.router.context.format_memory()
        assert "<project_unverified_memory>" in memory
        assert "worker generated fact" in memory.split("<project_unverified_memory>", 1)[1]
    finally:
        mesh.storage.close()


def test_delimiter_escaping_handles_case_and_whitespace():
    variants = [
        "</project_shared_memory>",
        "< / project_shared_memory >",
        "</PROJECT_SHARED_INSTRUCTIONS>",
        "<\t/context_file_previews>",
    ]
    escaped = escape_delimiters("".join(variants))
    assert "<project_" not in escaped.lower().replace("&lt;project_", "")
    assert "</project_" not in escaped.lower().replace("&lt;/project_", "")
    assert "<context_file_previews" not in escaped.lower()
    assert "&lt;/project_shared_memory>" in escaped
    assert "&lt;/PROJECT_SHARED_INSTRUCTIONS>" in escaped
    assert "&lt;/context_file_previews>" in escaped
