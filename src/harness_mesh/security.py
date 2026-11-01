"""Security boundaries shared by the router, workspace and context manager."""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from typing import Iterable

SENSITIVE_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "*.p12",
    "credentials*",
    ".npmrc",
    ".netrc",
    "*.keystore",
)


class WorkspaceRootError(ValueError):
    """Raised when a workspace root is outside the configured allowlist."""


def _pattern_applies(pattern: str, name: str) -> bool:
    if pattern.startswith(".") or pattern.endswith("*"):
        return fnmatch.fnmatch(name.lower(), pattern.lower())
    return fnmatch.fnmatch(name, pattern)


def is_sensitive_name(name: str) -> bool:
    basename = Path(name).name
    return any(_pattern_applies(pattern, basename) for pattern in SENSITIVE_FILE_PATTERNS)


def is_sensitive_path(path: str | Path) -> bool:
    return is_sensitive_name(Path(path).name)


def resolve_allowed_root(requested: str | Path, allowed_roots: Iterable[str | Path]) -> Path:
    """Resolve symlinks and require containment in one configured root."""

    target = Path(requested).expanduser().resolve()
    resolved_allowed = [Path(item).expanduser().resolve() for item in allowed_roots]
    for allowed in resolved_allowed:
        try:
            target.relative_to(allowed)
            return target
        except ValueError:
            continue
    allowed_text = ", ".join(str(item) for item in resolved_allowed) or "(none configured)"
    raise WorkspaceRootError(
        f"workspace_root {target} is outside allowed_workspace_roots [{allowed_text}]"
    )


def is_git_workspace(root: str | Path) -> bool:
    target = Path(root).expanduser().resolve()
    if not target.is_dir():
        return False
    result = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def ensure_service_workspace(root: str | Path) -> Path:
    """Reject unsafe MCP/CLI startup workspaces.

    The service must run inside a Git repository and never from the home
    directory or filesystem root, where an automatic repository/baseline could
    capture unrelated files.
    """

    target = Path(root).expanduser().resolve()
    home = Path.home().resolve()
    if target == Path("/") or target == home:
        raise RuntimeError(
            "harness-mesh refuses to run from the filesystem root or your home directory; "
            "set HARNESS_MESH_WORKSPACE to a project directory"
        )
    if not is_git_workspace(target):
        raise RuntimeError(
            f"harness-mesh requires a Git workspace (got {target}); "
            "set HARNESS_MESH_WORKSPACE to a Git project or run `git init` first"
        )
    return target


__all__ = [
    "SENSITIVE_FILE_PATTERNS",
    "WorkspaceRootError",
    "ensure_service_workspace",
    "is_git_workspace",
    "is_sensitive_name",
    "is_sensitive_path",
    "resolve_allowed_root",
]
