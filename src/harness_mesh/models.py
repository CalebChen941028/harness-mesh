"""Pydantic data contracts shared by every harness-mesh subsystem.

The models in this module deliberately separate a *ticket* (``TaskRequest`` /
task record) from the individual *assignments* (``AssignmentRecord``) that try
to execute it.  A ticket owns one Git worktree; assignments may hop across
harnesses and models while the worktree and the ticket identity stay stable.
"""

from __future__ import annotations

import os
import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _StrEnum(str, Enum):
    """String enum that serialises as its plain value on every Python version."""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return str(self.value)


class HarnessType(_StrEnum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    ZCODE = "zcode"
    DEEPSEEK = "deepseek"
    ANTIGRAVITY = "antigravity"


class WorkspaceMode(_StrEnum):
    READ_ONLY = "read_only"
    SHARED_WRITE = "shared_write"
    ISOLATED_WORKTREE = "isolated_worktree"


class TaskDifficulty(_StrEnum):
    AUTO = "auto"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class QuotaExhaustedPolicy(_StrEnum):
    FAILOVER = "failover"
    WAIT_AND_RESUME = "wait_and_resume"
    FAIL = "fail"


class CircuitState(_StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class TaskStatus(_StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_QUOTA = "waiting_for_quota"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", use_enum_values=False, populate_by_name=True)


class DiscoveredModel(_Model):
    harness: str
    model_id: str
    provider: str = "default"
    source: str
    is_active_default: bool = False
    authorized: bool = True
    capability_score: float = 20.0
    assigned_tiers: list[str] = Field(default_factory=list)
    supports_reasoning: bool = False
    context_window: int | None = None
    discovered_at: float = Field(default_factory=time.time)

    @property
    def is_placeholder(self) -> bool:
        return self.model_id == "<cli_active_default>"


class SkillSpec(_Model):
    name: str
    path: str
    description: str = ""
    source_root: str = ""
    tags: list[str] = Field(default_factory=list)
    body_preview: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class PluginSpec(_Model):
    name: str
    path: str = ""
    kind: str = "mcp"  # mcp | plugin | cli
    description: str = ""
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    server_config: dict[str, Any] = Field(default_factory=dict)
    source_root: str = ""


class ContinuationCheckpoint(_Model):
    interrupted_harness: str
    interrupted_model: str
    interruption_reason: str
    partial_modified_files: list[str] = Field(default_factory=list)
    partial_git_diff: str = ""
    partial_stdout_tail: str = ""
    captured_at: str = Field(default_factory=lambda: _utc_now())
    worktree_path: str | None = None
    last_assignment_id: str | None = None


class AssignmentRecord(_Model):
    assignment_id: str = Field(default_factory=lambda: f"asg_{uuid.uuid4().hex[:12]}")
    task_id: str
    attempt: int
    harness: str
    model: str
    status: str = "pending"  # pending | running | ended_quota | ended_error | completed
    started_at: str = Field(default_factory=lambda: _utc_now())
    ended_at: str | None = None
    report_summary: str = ""
    error_reason: str | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    session_id: str | None = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        allowed = {"pending", "running", "ended_quota", "ended_error", "completed"}
        if value not in allowed:
            raise ValueError(f"invalid assignment status: {value}")
        return value


class FailoverHop(_Model):
    attempt: int
    from_harness: str
    from_model: str
    to_harness: str
    to_model: str
    reason: str
    preserved_files: list[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: _utc_now())


class QuotaCircuitRecord(_Model):
    scope_key: str
    harness: str
    model: str
    state: CircuitState = CircuitState.CLOSED
    failure_reason: str = ""
    exhausted_at: float | None = None
    cooldown_until: float | None = None
    probe_count: int = 0
    last_probed_at: float | None = None

    @property
    def is_account_wide(self) -> bool:
        return self.model == "*"


class TaskRequest(_Model):
    task_id: str = Field(default_factory=lambda: f"task_{uuid.uuid4().hex[:12]}")
    target_harness: str
    objective: str
    difficulty: TaskDifficulty = TaskDifficulty.AUTO
    model: str | None = None
    reasoning_effort: str | None = None
    skills: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    auto_match_skills: bool = False
    on_quota_exhausted: QuotaExhaustedPolicy = QuotaExhaustedPolicy.FAILOVER
    fallback_harnesses: list[str] = Field(default_factory=list)
    max_failover_attempts: int = 4
    context_files: list[str] = Field(default_factory=list)
    context_notes: str = ""
    depends_on: list[str] = Field(default_factory=list)
    sync_uncommitted: bool = True
    include_project_context: bool = True
    auto_record_context: bool = False
    workspace_mode: WorkspaceMode = WorkspaceMode.ISOLATED_WORKTREE
    workspace_root: str = Field(default_factory=os.getcwd)
    session_id: str | None = None
    timeout_seconds: int | None = None
    call_depth: int = 0
    trace_chain: list[str] = Field(default_factory=list)
    checkpoint: ContinuationCheckpoint | None = None
    parent_task_id: str | None = None

    @field_validator("target_harness")
    @classmethod
    def _target_non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("target_harness must not be empty")
        return value.strip()

    @field_validator("max_failover_attempts")
    @classmethod
    def _attempts_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("max_failover_attempts must be >= 0")
        return value


class TaskResourcePage(_Model):
    task_id: str
    resource: str
    offset: int
    limit: int
    total_length: int
    content: str
    next_offset: int
    has_more: bool


class TaskResult(_Model):
    task_id: str
    initial_harness: str
    target_harness: str
    status: TaskStatus
    resolved_difficulty: TaskDifficulty | None = None
    resolved_model: str | None = None
    resolved_reasoning_effort: str | None = None
    activated_skills: list[str] = Field(default_factory=list)
    activated_plugins: list[str] = Field(default_factory=list)
    failover_count: int = 0
    failover_history: list[FailoverHop] = Field(default_factory=list)
    assignments: list[AssignmentRecord] = Field(default_factory=list)
    summary: str = ""
    raw_stdout: str = ""
    raw_stderr: str = ""
    modified_files: list[str] = Field(default_factory=list)
    git_diff: str = ""
    git_diff_truncated: bool = False
    warnings: list[str] = Field(default_factory=list)
    read_pointers: dict[str, str] = Field(default_factory=dict)
    worktree_path: str | None = None
    worktree_branch: str | None = None
    native_session_id: str | None = None
    duration_ms: int = 0
    error_detail: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == TaskStatus.COMPLETED


class HarnessStatus(_Model):
    harness: str = ""
    available: bool = False
    mode: str = "cli"
    binary_path: str | None = None
    version_or_detail: str = ""
    active_default_model: str | None = None
    authorized_models: list[str] = Field(default_factory=list)
    tier_models: dict[str, str] = Field(default_factory=dict)
    quota_state: str = "closed"
    circuits: list[QuotaCircuitRecord] = Field(default_factory=list)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def utc_now() -> str:
    """Public helper used by workspace/quota modules."""

    return _utc_now()


__all__ = [
    "AssignmentRecord",
    "CircuitState",
    "ContinuationCheckpoint",
    "DiscoveredModel",
    "FailoverHop",
    "HarnessStatus",
    "HarnessType",
    "PluginSpec",
    "QuotaCircuitRecord",
    "QuotaExhaustedPolicy",
    "SkillSpec",
    "TaskDifficulty",
    "TaskRequest",
    "TaskResourcePage",
    "TaskResult",
    "TaskStatus",
    "WorkspaceMode",
    "utc_now",
]
