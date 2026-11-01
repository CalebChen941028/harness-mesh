"""SQLite-backed durable state for harness-mesh.

The ledger keeps the five pieces of state that must survive a process restart:

* ``discovered_models`` — runtime model inventory and authorisation flags.
* ``quota_circuits`` — CLOSED / OPEN / HALF_OPEN circuit state.
* ``tasks`` — tickets, their request payload, status and checkpoint.
* ``assignments`` / ``failover_hops`` — the multi-hop execution history.
* ``effects`` — idempotent durable side-effect queue keyed by ``(kind, key)``.
* ``resources`` — full diff / transcript payloads read page-by-page by the
  orchestrating harness instead of being pushed into its context window.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .models import (
    AssignmentRecord,
    ContinuationCheckpoint,
    DiscoveredModel,
    FailoverHop,
    QuotaCircuitRecord,
    TaskRequest,
    TaskResourcePage,
    TaskResult,
    TaskStatus,
    utc_now,
)


class StateStoreError(RuntimeError):
    pass


class TaskNotFoundError(StateStoreError):
    pass


class ResourceNotFoundError(StateStoreError):
    pass


def _pid_alive(pid: int) -> bool:
    """True while the process exists (zombies still count as alive)."""

    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, ValueError):
        # A permission error means the pid exists but belongs to another user.
        return True
    return True


_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovered_models (
    harness TEXT NOT NULL,
    model_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'default',
    source TEXT NOT NULL DEFAULT 'local_config',
    is_active_default INTEGER NOT NULL DEFAULT 0,
    authorized INTEGER NOT NULL DEFAULT 1,
    capability_score REAL NOT NULL DEFAULT 20.0,
    assigned_tiers TEXT NOT NULL DEFAULT '[]',
    supports_reasoning INTEGER NOT NULL DEFAULT 0,
    context_window INTEGER,
    discovered_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (harness, model_id)
);

CREATE TABLE IF NOT EXISTS quota_circuits (
    scope_key TEXT PRIMARY KEY,
    harness TEXT NOT NULL,
    model TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'closed',
    failure_reason TEXT NOT NULL DEFAULT '',
    exhausted_at REAL,
    cooldown_until REAL,
    probe_count INTEGER NOT NULL DEFAULT 0,
    last_probed_at REAL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    parent_task_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    request_json TEXT NOT NULL,
    result_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    initial_harness TEXT,
    current_harness TEXT,
    current_model TEXT,
    resolved_difficulty TEXT,
    resolved_model TEXT,
    resolved_effort TEXT,
    checkpoint_json TEXT,
    blocked_harness TEXT,
    blocked_model TEXT,
    worktree_path TEXT,
    worktree_branch TEXT,
    call_depth INTEGER NOT NULL DEFAULT 0,
    trace_chain TEXT NOT NULL DEFAULT '[]',
    error_detail TEXT
);

CREATE TABLE IF NOT EXISTS assignments (
    assignment_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    harness TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    report_summary TEXT NOT NULL DEFAULT '',
    error_reason TEXT,
    duration_ms INTEGER,
    exit_code INTEGER,
    session_id TEXT,
    call_depth INTEGER NOT NULL DEFAULT 0,
    trace_chain TEXT NOT NULL DEFAULT '[]',
    pid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_assignments_task ON assignments(task_id, attempt);
CREATE INDEX IF NOT EXISTS idx_assignments_status ON assignments(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_assignments_task_attempt_unique ON assignments(task_id, attempt);

CREATE TABLE IF NOT EXISTS failover_hops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    from_harness TEXT NOT NULL,
    from_model TEXT NOT NULL,
    to_harness TEXT NOT NULL,
    to_model TEXT NOT NULL,
    reason TEXT NOT NULL,
    preserved_files TEXT NOT NULL DEFAULT '[]',
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hops_task ON failover_hops(task_id, id);

CREATE TABLE IF NOT EXISTS effects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_run_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(kind, key)
);

CREATE TABLE IF NOT EXISTS project_context_entries (
    key TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT 'note',
    content TEXT NOT NULL,
    author_harness TEXT NOT NULL DEFAULT 'controller',
    task_id TEXT,
    source TEXT NOT NULL DEFAULT 'controller',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    task_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    content TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (task_id, resource)
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    source TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    payload TEXT NOT NULL
);
"""


class StateStore:
    """Thread-safe SQLite ledger used by the router, quota guardian and MCP."""

    def __init__(self, db_path: str | Path, *, read_only: bool = False):
        self.path = Path(db_path).expanduser()
        self.read_only = read_only
        self._lock = threading.RLock()
        if read_only:
            # ``hmesh doctor`` inspects the ledger without creating, migrating
            # or touching WAL files.  A missing database is simply "empty".
            if not self.path.exists():
                raise FileNotFoundError(self.path)
            self._conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, check_same_thread=False, timeout=30
            )
            self._conn.row_factory = sqlite3.Row
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(project_context_entries)").fetchall()
        }
        if "source" not in columns:
            self._conn.execute(
                "ALTER TABLE project_context_entries ADD COLUMN source TEXT NOT NULL DEFAULT 'controller'"
            )
        # Assignments gained the exact lineage handed to the adapter plus the
        # spawned harness pid.  Old databases keep working because the new
        # columns carry defaults (a NULL pid means "unknown, assume alive").
        assignment_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(assignments)").fetchall()
        }
        for name, ddl in (
            ("call_depth", "INTEGER NOT NULL DEFAULT 0"),
            ("trace_chain", "TEXT NOT NULL DEFAULT '[]'"),
            ("pid", "INTEGER"),
        ):
            if name not in assignment_columns:
                self._conn.execute(f"ALTER TABLE assignments ADD COLUMN {name} {ddl}")

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                if not self.read_only:
                    self._conn.commit()
            finally:
                self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cursor = self._conn.execute(sql, tuple(params))
        self._conn.commit()
        return cursor

    # ------------------------------------------------------------------
    # discovered models
    # ------------------------------------------------------------------
    def save_discovered_models(
        self,
        models: Iterable[DiscoveredModel],
        *,
        preserve_unauthorized: bool = True,
    ) -> list[DiscoveredModel]:
        now = time.time()
        saved: list[DiscoveredModel] = []
        with self._lock:
            for model in models:
                existing = self._conn.execute(
                    "SELECT authorized FROM discovered_models WHERE harness=? AND model_id=?",
                    (model.harness, model.model_id),
                ).fetchone()
                authorized = model.authorized
                if preserve_unauthorized and existing is not None and not bool(existing["authorized"]):
                    authorized = False
                tiers = list(model.assigned_tiers) if authorized else []
                self._conn.execute(
                    """
                    INSERT INTO discovered_models
                        (harness, model_id, provider, source, is_active_default, authorized,
                         capability_score, assigned_tiers, supports_reasoning, context_window,
                         discovered_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(harness, model_id) DO UPDATE SET
                        provider=excluded.provider,
                        source=excluded.source,
                        is_active_default=excluded.is_active_default,
                        authorized=excluded.authorized,
                        capability_score=excluded.capability_score,
                        assigned_tiers=excluded.assigned_tiers,
                        supports_reasoning=excluded.supports_reasoning,
                        context_window=excluded.context_window,
                        discovered_at=excluded.discovered_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        model.harness,
                        model.model_id,
                        model.provider,
                        model.source,
                        int(model.is_active_default),
                        int(authorized),
                        float(model.capability_score),
                        json.dumps(tiers),
                        int(model.supports_reasoning),
                        model.context_window,
                        float(model.discovered_at or now),
                        now,
                    ),
                )
                saved.append(model.model_copy(update={"authorized": authorized, "assigned_tiers": tiers}))
            self._conn.commit()
        return saved

    def list_discovered_models(
        self, harness: str | None = None, *, authorized_only: bool = False
    ) -> list[DiscoveredModel]:
        query = "SELECT * FROM discovered_models"
        clauses: list[str] = []
        params: list[Any] = []
        if harness is not None:
            clauses.append("harness=?")
            params.append(harness)
        if authorized_only:
            clauses.append("authorized=1")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY harness, capability_score DESC, model_id"
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_model(row) for row in rows]

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> DiscoveredModel:
        try:
            tiers = json.loads(row["assigned_tiers"] or "[]")
        except json.JSONDecodeError:
            tiers = []
        return DiscoveredModel(
            harness=row["harness"],
            model_id=row["model_id"],
            provider=row["provider"],
            source=row["source"],
            is_active_default=bool(row["is_active_default"]),
            authorized=bool(row["authorized"]),
            capability_score=float(row["capability_score"]),
            assigned_tiers=list(tiers),
            supports_reasoning=bool(row["supports_reasoning"]),
            context_window=row["context_window"],
            discovered_at=float(row["discovered_at"]),
        )

    def mark_model_unauthorized(self, harness: str, model_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE discovered_models SET authorized=0, assigned_tiers='[]', updated_at=? "
                "WHERE harness=? AND model_id=?",
                (time.time(), harness, model_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def set_model_tiers(self, harness: str, tiers: dict[str, list[str]]) -> None:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT model_id FROM discovered_models WHERE harness=?", (harness,)
            ).fetchall()
            for row in rows:
                model_id = row["model_id"]
                self._conn.execute(
                    "UPDATE discovered_models SET assigned_tiers=?, updated_at=? WHERE harness=? AND model_id=?",
                    (json.dumps(list(tiers.get(model_id, []))), now, harness, model_id),
                )
            self._conn.commit()

    def latest_discovery_ts(self, harness: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(discovered_at) AS ts FROM discovered_models WHERE harness=?", (harness,)
            ).fetchone()
        return float(row["ts"] or 0.0) if row else 0.0

    def count_authorized_models(self, harness: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM discovered_models WHERE harness=? AND authorized=1", (harness,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def reset_unauthorized(self, harness: str | None = None) -> int:
        sql = "UPDATE discovered_models SET authorized=1, updated_at=?"
        params: list[Any] = [time.time()]
        if harness is not None:
            sql += " WHERE harness=?"
            params.append(harness)
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # quota circuits
    # ------------------------------------------------------------------
    def upsert_circuit(self, record: QuotaCircuitRecord) -> QuotaCircuitRecord:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO quota_circuits
                    (scope_key, harness, model, state, failure_reason, exhausted_at,
                     cooldown_until, probe_count, last_probed_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    state=excluded.state,
                    failure_reason=excluded.failure_reason,
                    exhausted_at=excluded.exhausted_at,
                    cooldown_until=excluded.cooldown_until,
                    probe_count=excluded.probe_count,
                    last_probed_at=excluded.last_probed_at,
                    updated_at=excluded.updated_at
                """,
                (
                    record.scope_key,
                    record.harness,
                    record.model,
                    record.state.value if hasattr(record.state, "value") else str(record.state),
                    record.failure_reason,
                    record.exhausted_at,
                    record.cooldown_until,
                    record.probe_count,
                    record.last_probed_at,
                    time.time(),
                ),
            )
            self._conn.commit()
        return record

    def get_circuit(self, harness: str, model: str) -> QuotaCircuitRecord | None:
        scope_key = f"{harness}::{model}"
        with self._lock:
            row = self._conn.execute("SELECT * FROM quota_circuits WHERE scope_key=?", (scope_key,)).fetchone()
        return self._row_to_circuit(row) if row else None

    def list_circuits(
        self, harness: str | None = None, states: Iterable[str] | None = None
    ) -> list[QuotaCircuitRecord]:
        query = "SELECT * FROM quota_circuits"
        clauses: list[str] = []
        params: list[Any] = []
        if harness is not None:
            clauses.append("harness=?")
            params.append(harness)
        state_list = list(states or [])
        if state_list:
            placeholders = ",".join("?" for _ in state_list)
            clauses.append(f"state IN ({placeholders})")
            params.extend(state_list)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY harness, model"
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_circuit(row) for row in rows]

    @staticmethod
    def _row_to_circuit(row: sqlite3.Row) -> QuotaCircuitRecord:
        return QuotaCircuitRecord(
            scope_key=row["scope_key"],
            harness=row["harness"],
            model=row["model"],
            state=row["state"],
            failure_reason=row["failure_reason"],
            exhausted_at=row["exhausted_at"],
            cooldown_until=row["cooldown_until"],
            probe_count=row["probe_count"],
            last_probed_at=row["last_probed_at"],
        )

    def delete_circuit(self, harness: str, model: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM quota_circuits WHERE scope_key=?", (f"{harness}::{model}",)
            )
            self._conn.commit()

    def clear_circuits(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM quota_circuits")
            self._conn.commit()

    # ------------------------------------------------------------------
    # shared project context blackboard
    # ------------------------------------------------------------------
    def upsert_project_context(
        self,
        key: str,
        content: str,
        *,
        category: str = "note",
        author_harness: str = "controller",
        task_id: str | None = None,
        source: str = "controller",
    ) -> dict[str, Any]:
        now = time.time()
        key = str(key).strip()[:128] or "untitled"
        content = str(content)[:4000]
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO project_context_entries
                    (key, category, content, author_harness, task_id, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    category=excluded.category,
                    content=excluded.content,
                    author_harness=excluded.author_harness,
                    task_id=excluded.task_id,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (key, category, content, author_harness, task_id, source, now),
            )
            self._conn.commit()
        return {
            "key": key,
            "category": category,
            "content": content,
            "author_harness": author_harness,
            "task_id": task_id,
            "source": source,
            "updated_at": now,
        }

    def list_project_context(self, category: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM project_context_entries"
        params: list[Any] = []
        if category:
            query += " WHERE category=?"
            params.append(category)
        query += " ORDER BY category, key"
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_project_context(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM project_context_entries WHERE key=?", (key,)
            ).fetchone()
        return dict(row) if row else None

    def delete_project_context(self, key: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM project_context_entries WHERE key=?", (key,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # tasks (tickets)
    # ------------------------------------------------------------------
    def create_task(
        self,
        request: TaskRequest,
        *,
        status: str | TaskStatus = TaskStatus.PENDING,
        initial_harness: str | None = None,
        worktree_path: str | None = None,
        worktree_branch: str | None = None,
        checkpoint: ContinuationCheckpoint | None = None,
    ) -> None:
        now = time.time()
        status_value = status.value if hasattr(status, "value") else str(status)
        checkpoint_json = checkpoint.model_dump_json() if checkpoint else None
        with self._lock:
            existing = self._conn.execute(
                "SELECT created_at FROM tasks WHERE task_id=?", (request.task_id,)
            ).fetchone()
            created_at = float(existing["created_at"]) if existing else now
            self._conn.execute(
                """
                INSERT INTO tasks
                    (task_id, parent_task_id, status, request_json, created_at, updated_at,
                     initial_harness, current_harness, current_model, checkpoint_json,
                     worktree_path, worktree_branch, call_depth, trace_chain)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status=excluded.status,
                    request_json=excluded.request_json,
                    updated_at=excluded.updated_at,
                    initial_harness=COALESCE(tasks.initial_harness, excluded.initial_harness),
                    current_harness=excluded.current_harness,
                    current_model=excluded.current_model,
                    checkpoint_json=COALESCE(excluded.checkpoint_json, tasks.checkpoint_json),
                    worktree_path=COALESCE(excluded.worktree_path, tasks.worktree_path),
                    worktree_branch=COALESCE(excluded.worktree_branch, tasks.worktree_branch),
                    call_depth=excluded.call_depth,
                    trace_chain=excluded.trace_chain
                """,
                (
                    request.task_id,
                    request.parent_task_id,
                    status_value,
                    request.model_dump_json(),
                    created_at,
                    now,
                    initial_harness or request.target_harness,
                    request.target_harness,
                    request.model,
                    checkpoint_json,
                    worktree_path,
                    worktree_branch,
                    request.call_depth,
                    json.dumps(request.trace_chain),
                ),
            )
            self._conn.commit()

    def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "result_json",
            "initial_harness",
            "current_harness",
            "current_model",
            "resolved_difficulty",
            "resolved_model",
            "resolved_effort",
            "checkpoint_json",
            "blocked_harness",
            "blocked_model",
            "worktree_path",
            "worktree_branch",
            "error_detail",
            "parent_task_id",
        }
        updates: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"cannot update task column: {key}")
            if key in {"checkpoint_json", "result_json"} and value is not None and not isinstance(value, str):
                value = value.model_dump_json() if hasattr(value, "model_dump_json") else json.dumps(value)
            updates.append(f"{key}=?")
            params.append(value)
        if not updates:
            return
        updates.append("updated_at=?")
        params.append(time.time())
        params.append(task_id)
        with self._lock:
            # Column names come from the allow-list above; values are bound parameters.
            statement = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id=?"  # nosec B608
            self._conn.execute(statement, tuple(params))
            self._conn.commit()

    def set_task_result(self, task_id: str, result: TaskResult) -> None:
        self.update_task(
            task_id,
            status=result.status.value if hasattr(result.status, "value") else str(result.status),
            result_json=result.model_dump_json(),
            current_harness=result.target_harness,
            current_model=result.resolved_model,
            error_detail=result.error_detail,
            worktree_path=result.worktree_path,
            worktree_branch=result.worktree_branch,
        )

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        data = dict(row)
        try:
            data["request"] = TaskRequest.model_validate_json(row["request_json"])
        except Exception:
            data["request"] = None
        if row["result_json"]:
            try:
                data["result"] = TaskResult.model_validate_json(row["result_json"])
            except Exception:
                data["result"] = None
        else:
            data["result"] = None
        if row["checkpoint_json"]:
            try:
                data["checkpoint"] = ContinuationCheckpoint.model_validate_json(row["checkpoint_json"])
            except Exception:
                data["checkpoint"] = None
        else:
            data["checkpoint"] = None
        try:
            data["trace_chain"] = json.loads(row["trace_chain"] or "[]")
        except json.JSONDecodeError:
            data["trace_chain"] = []
        return data

    def task_exists(self, task_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return row is not None

    def list_tasks(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data.pop("request_json", None)
            data.pop("result_json", None)
            data.pop("checkpoint_json", None)
            results.append(data)
        return results

    def get_request(self, task_id: str) -> TaskRequest | None:
        return self.get_task(task_id)["request"]

    def next_attempt(self, task_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS attempt FROM assignments WHERE task_id=?", (task_id,)
            ).fetchone()
        return int(row["attempt"]) + 1 if row else 1

    # ------------------------------------------------------------------
    # assignments and failover hops
    # ------------------------------------------------------------------
    def add_assignment(self, record: AssignmentRecord) -> AssignmentRecord:
        # Until the adapter reports the harness pid, the dispatching process
        # owns the assignment.  Recording it immediately keeps the global
        # active-assignment count honest even before the child exists.
        pid = record.pid if record.pid is not None else os.getpid()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO assignments
                    (assignment_id, task_id, attempt, harness, model, status, started_at,
                     ended_at, report_summary, error_reason, duration_ms, exit_code, session_id,
                     call_depth, trace_chain, pid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(assignment_id) DO UPDATE SET
                    status=excluded.status, ended_at=excluded.ended_at,
                    report_summary=excluded.report_summary, error_reason=excluded.error_reason,
                    duration_ms=excluded.duration_ms, exit_code=excluded.exit_code,
                    session_id=excluded.session_id, call_depth=excluded.call_depth,
                    trace_chain=excluded.trace_chain, pid=excluded.pid
                """,
                (
                    record.assignment_id,
                    record.task_id,
                    record.attempt,
                    record.harness,
                    record.model,
                    record.status,
                    record.started_at,
                    record.ended_at,
                    record.report_summary,
                    record.error_reason,
                    record.duration_ms,
                    record.exit_code,
                    record.session_id,
                    int(record.call_depth),
                    json.dumps(list(record.trace_chain)),
                    pid,
                ),
            )
            self._conn.commit()
        return record.model_copy(update={"pid": pid})

    def update_assignment(self, assignment_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "ended_at",
            "report_summary",
            "error_reason",
            "duration_ms",
            "exit_code",
            "session_id",
            "call_depth",
            "trace_chain",
            "pid",
        }
        updates: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"cannot update assignment column: {key}")
            if key == "trace_chain" and not isinstance(value, str):
                value = json.dumps([str(item) for item in value])
            updates.append(f"{key}=?")
            params.append(value)
        if not updates:
            return
        params.append(assignment_id)
        with self._lock:
            # Column names come from the allow-list above; values are bound parameters.
            statement = f"UPDATE assignments SET {', '.join(updates)} WHERE assignment_id=?"  # nosec B608
            self._conn.execute(statement, tuple(params))
            self._conn.commit()

    @staticmethod
    def _row_to_assignment(row: sqlite3.Row) -> AssignmentRecord:
        try:
            trace = json.loads(row["trace_chain"] or "[]")
        except json.JSONDecodeError:
            trace = []
        return AssignmentRecord(
            assignment_id=row["assignment_id"],
            task_id=row["task_id"],
            attempt=row["attempt"],
            harness=row["harness"],
            model=row["model"],
            status=row["status"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            report_summary=row["report_summary"],
            error_reason=row["error_reason"],
            duration_ms=row["duration_ms"],
            exit_code=row["exit_code"],
            session_id=row["session_id"],
            call_depth=int(row["call_depth"] or 0),
            trace_chain=[str(item) for item in trace],
            pid=int(row["pid"]) if row["pid"] is not None else None,
        )

    def list_assignments(self, task_id: str) -> list[AssignmentRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM assignments WHERE task_id=? ORDER BY attempt, started_at", (task_id,)
            ).fetchall()
        return [self._row_to_assignment(row) for row in rows]

    def list_running_assignments(self) -> list[AssignmentRecord]:
        """Every assignment still marked running, oldest first."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM assignments WHERE status='running' ORDER BY started_at, attempt"
            ).fetchall()
        return [self._row_to_assignment(row) for row in rows]

    def get_running_assignment(self, task_id: str) -> AssignmentRecord | None:
        """The newest running assignment for one ticket, if any."""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assignments WHERE task_id=? AND status='running' "
                "ORDER BY attempt DESC, started_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return self._row_to_assignment(row) if row is not None else None

    def count_live_assignments(self) -> int:
        """Count running assignments whose process is still alive.

        A row whose pid no longer exists is a leftover from a crashed host; it
        is closed as ``ended_error``/``stale`` instead of occupying a slot
        forever.  Rows without a pid predate the column and are treated as
        alive because their owner cannot be proven dead.
        """

        live = 0
        stale: list[str] = []
        for record in self.list_running_assignments():
            if record.pid is None or _pid_alive(record.pid):
                live += 1
            else:
                stale.append(record.assignment_id)
        for assignment_id in stale:
            self.update_assignment(
                assignment_id,
                status="ended_error",
                ended_at=utc_now(),
                error_reason="stale",
            )
        return live

    def add_failover_hop(self, task_id: str, hop: FailoverHop) -> FailoverHop:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO failover_hops
                    (task_id, attempt, from_harness, from_model, to_harness, to_model,
                     reason, preserved_files, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    hop.attempt,
                    hop.from_harness,
                    hop.from_model,
                    hop.to_harness,
                    hop.to_model,
                    hop.reason,
                    json.dumps(list(hop.preserved_files)),
                    hop.timestamp,
                ),
            )
            self._conn.commit()
        return hop

    def list_failover_hops(self, task_id: str) -> list[FailoverHop]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM failover_hops WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        hops: list[FailoverHop] = []
        for row in rows:
            try:
                files = json.loads(row["preserved_files"] or "[]")
            except json.JSONDecodeError:
                files = []
            hops.append(
                FailoverHop(
                    attempt=row["attempt"],
                    from_harness=row["from_harness"],
                    from_model=row["from_model"],
                    to_harness=row["to_harness"],
                    to_model=row["to_model"],
                    reason=row["reason"],
                    preserved_files=list(files),
                    timestamp=row["timestamp"],
                )
            )
        return hops

    # ------------------------------------------------------------------
    # durable effects queue
    # ------------------------------------------------------------------
    def enqueue_effect(
        self,
        kind: str,
        key: str,
        payload: dict[str, Any] | None = None,
        *,
        next_run_at: float | None = None,
    ) -> bool:
        """Idempotently enqueue a durable effect.

        A completed/failed effect with the same ``(kind, key)`` is re-armed
        rather than duplicated, so a ticket that suspends twice still gets a
        pending ``resume_waiting`` entry on the second suspension.
        """

        now = time.time()
        payload_text = json.dumps(payload or {})
        with self._lock:
            existing = self._conn.execute(
                "SELECT id, status FROM effects WHERE kind=? AND key=?", (kind, key)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO effects
                        (kind, key, payload, status, attempts, next_run_at, created_at, updated_at)
                    VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
                    """,
                    (kind, key, payload_text, next_run_at or now, now, now),
                )
                self._conn.commit()
                return True
            if existing["status"] in {"done", "failed"}:
                self._conn.execute(
                    """
                    UPDATE effects
                    SET payload=?, status='pending', attempts=0, next_run_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (payload_text, next_run_at or now, now, existing["id"]),
                )
                self._conn.commit()
                return True
            self._conn.execute(
                "UPDATE effects SET payload=?, updated_at=? WHERE id=?",
                (payload_text, now, existing["id"]),
            )
            self._conn.commit()
            return False

    def list_effects(self, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM effects"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        effects: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            effects.append(item)
        return effects

    def due_effects(self, now: float | None = None, limit: int = 50) -> list[dict[str, Any]]:
        timestamp = now if now is not None else time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM effects WHERE status='pending' AND next_run_at<=? ORDER BY next_run_at LIMIT ?",
                (timestamp, limit),
            ).fetchall()
        effects: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            effects.append(item)
        return effects

    def mark_effect_running(self, effect_id: int) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE effects SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                (now, effect_id),
            )
            self._conn.commit()

    def mark_effect_done(self, effect_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE effects SET status='done', updated_at=? WHERE id=?", (time.time(), effect_id)
            )
            self._conn.commit()

    def get_effect(self, kind: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM effects WHERE kind=? AND key=?", (kind, key)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["payload"] = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        return item

    def mark_effect_done_by_key(self, kind: str, key: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE effects SET status='done', updated_at=? WHERE kind=? AND key=?",
                (time.time(), kind, key),
            )
            self._conn.commit()

    def reset_stale_effects(self, older_than_seconds: float = 300.0) -> int:
        """Return interrupted ``running`` effects to the pending queue.

        The effects worker marks an item running before executing it.  If the
        process dies in between, that row would otherwise stay ``running``
        forever and the resume/probe work would be lost.
        """

        now = time.time()
        cutoff = now - max(0.0, float(older_than_seconds))
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE effects
                SET status='pending', next_run_at=?, updated_at=?
                WHERE status='running' AND updated_at < ?
                """,
                (now, now, cutoff),
            )
            self._conn.commit()
            return cursor.rowcount

    def mark_effect_failed(self, effect_id: int, retry_delay: float = 30.0) -> None:
        """Reschedule a failed effect with bounded exponential backoff."""

        now = time.time()
        with self._lock:
            row = self._conn.execute("SELECT attempts FROM effects WHERE id=?", (effect_id,)).fetchone()
            attempts = int(row["attempts"]) if row else 1
            delay = min(max(1.0, retry_delay) * (2 ** max(0, attempts - 1)), 3600.0)
            self._conn.execute(
                "UPDATE effects SET status='pending', next_run_at=?, updated_at=? WHERE id=?",
                (now + delay, now, effect_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # paged resources
    # ------------------------------------------------------------------
    def save_resource(self, task_id: str, resource: str, content: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO resources (task_id, resource, content, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id, resource) DO UPDATE SET
                    content=excluded.content, updated_at=excluded.updated_at
                """,
                (task_id, resource, content, time.time()),
            )
            self._conn.commit()

    def get_resource(self, task_id: str, resource: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT content FROM resources WHERE task_id=? AND resource=?", (task_id, resource)
            ).fetchone()
        return row["content"] if row else None

    def read_task_resource(
        self,
        task_id: str,
        resource: str = "diff",
        offset: int = 0,
        limit: int = 4000,
    ) -> TaskResourcePage:
        if not self.task_exists(task_id):
            raise TaskNotFoundError(f"task not found: {task_id}")
        resource_name = (resource or "diff").strip().lower()
        content = self.get_resource(task_id, resource_name)
        if content is None and resource_name == "assignments":
            assignments = self.list_assignments(task_id)
            hops = self.list_failover_hops(task_id)
            content = json.dumps(
                {
                    "task_id": task_id,
                    "assignments": [a.model_dump(mode="json") for a in assignments],
                    "failover_history": [h.model_dump(mode="json") for h in hops],
                },
                indent=2,
                ensure_ascii=False,
            )
        if content is None and resource_name == "transcript":
            content = ""
        if content is None:
            raise ResourceNotFoundError(
                f"resource '{resource_name}' is not available for task {task_id}; "
                "known resources: diff, transcript, assignments"
            )
        total = len(content)
        start = max(0, int(offset))
        cap = max(1, min(int(limit), 200_000))
        end = min(total, start + cap)
        chunk = content[start:end] if start < total else ""
        next_offset = end if end < total else total
        return TaskResourcePage(
            task_id=task_id,
            resource=resource_name,
            offset=start,
            limit=cap,
            total_length=total,
            content=chunk,
            next_offset=next_offset,
            has_more=end < total,
        )

    def resource_names(self, task_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT resource FROM resources WHERE task_id=? ORDER BY resource", (task_id,)
            ).fetchall()
        return [row["resource"] for row in rows]

    # ------------------------------------------------------------------
    # market snapshots
    # ------------------------------------------------------------------
    def save_market_snapshot(self, source: str, fetched_at: float, payload: Any) -> None:
        """Persist one provider snapshot; ``payload`` is stored as JSON text."""

        encoded = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self._execute(
            "INSERT INTO market_snapshots (source, fetched_at, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET fetched_at=excluded.fetched_at, payload=excluded.payload",
            (str(source), float(fetched_at), encoded),
        )

    def get_market_snapshot(self, source: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT source, fetched_at, payload FROM market_snapshots WHERE source=?",
                (str(source),),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        return {"source": row["source"], "fetched_at": float(row["fetched_at"]), "payload": payload}

    def list_market_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, fetched_at, payload FROM market_snapshots ORDER BY source"
            ).fetchall()
        snapshots: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = None
            snapshots.append(
                {"source": row["source"], "fetched_at": float(row["fetched_at"]), "payload": payload}
            )
        return snapshots

    def delete_market_snapshot(self, source: str) -> None:
        self._execute("DELETE FROM market_snapshots WHERE source=?", (str(source),))

    # ------------------------------------------------------------------
    # waiting queue helpers
    # ------------------------------------------------------------------
    def list_tasks_with_status(self, status: str) -> list[dict[str, Any]]:
        return self.list_tasks(status=status, limit=500)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            models = self._conn.execute(
                "SELECT harness, COUNT(*) AS n, SUM(authorized) AS authorized FROM discovered_models GROUP BY harness"
            ).fetchall()
            tasks = self._conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
            circuits = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM quota_circuits GROUP BY state"
            ).fetchall()
        return {
            "models": {row["harness"]: {"total": row["n"], "authorized": row["authorized"] or 0} for row in models},
            "tasks": {row["status"]: row["n"] for row in tasks},
            "circuits": {row["state"]: row["n"] for row in circuits},
        }


__all__ = [
    "ResourceNotFoundError",
    "StateStore",
    "StateStoreError",
    "TaskNotFoundError",
]
