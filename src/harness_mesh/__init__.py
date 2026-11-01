"""Harness-Mesh: federated cross-harness orchestration."""

from __future__ import annotations

__version__ = "0.3.1"

from .config import MeshConfig, build_handoff_prompt, evaluate_difficulty, load_config
from .context import ProjectContextManager
from .models import (
    AssignmentRecord,
    CircuitState,
    ContinuationCheckpoint,
    DiscoveredModel,
    FailoverHop,
    HarnessStatus,
    HarnessType,
    QuotaCircuitRecord,
    QuotaExhaustedPolicy,
    TaskDifficulty,
    TaskRequest,
    TaskResult,
    TaskStatus,
    WorkspaceMode,
)

__all__ = [
    "AssignmentRecord",
    "CircuitState",
    "ContinuationCheckpoint",
    "DiscoveredModel",
    "FailoverHop",
    "HarnessStatus",
    "HarnessType",
    "MeshConfig",
    "ProjectContextManager",
    "QuotaCircuitRecord",
    "QuotaExhaustedPolicy",
    "TaskDifficulty",
    "TaskRequest",
    "TaskResult",
    "TaskStatus",
    "WorkspaceMode",
    "__version__",
    "build_handoff_prompt",
    "evaluate_difficulty",
    "load_config",
]
