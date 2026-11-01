"""Security boundaries shared by the router, workspace and context manager."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Iterable

SENSITIVE_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.p8",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "id_dsa*",
    "credentials",
    "credentials.json",
    "credentials.yml",
    "credentials.yaml",
    ".credentials*",
    ".npmrc",
    ".netrc",
    ".pypirc",
    ".git-credentials",
    ".dockercfg",
    ".htpasswd",
    "*.tfvars",
    "terraform.tfstate*",
    "kubeconfig",
    "secrets.json",
    "secrets.yml",
    "secrets.yaml",
    "secrets.toml",
)
SENSITIVE_DIR_SEGMENTS: tuple[str, ...] = (".ssh", ".aws", ".gnupg", ".git")
ALLOWED_ENV_EXAMPLES: frozenset[str] = frozenset(
    {".env.example", ".env.sample", ".env.template", ".env.dist"}
)
_PRIVATE_KEY_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")


def _pattern_applies(pattern: str, name: str) -> bool:
    return fnmatch.fnmatch(name.lower(), pattern.lower())


def is_sensitive_name(name: str) -> bool:
    basename = Path(name).name
    lowered = basename.lower()
    if lowered in ALLOWED_ENV_EXAMPLES:
        return False
    if lowered.endswith(".pub") and lowered.startswith(_PRIVATE_KEY_PREFIXES):
        return False
    return any(_pattern_applies(pattern, basename) for pattern in SENSITIVE_FILE_PATTERNS)


def is_sensitive_path(path: str | Path) -> bool:
    parts = Path(path).parts
    if any(part.lower() in SENSITIVE_DIR_SEGMENTS for part in parts):
        return True
    return is_sensitive_name(Path(path).name)


def sensitive_git_pathspecs() -> list[str]:
    """Git pathspec ``:(exclude,glob)`` arguments for sensitive material."""

    specs: list[str] = []
    for pattern in SENSITIVE_FILE_PATTERNS:
        specs.append(f":(exclude,glob){pattern}")
    for segment in SENSITIVE_DIR_SEGMENTS:
        specs.append(f":(exclude,glob)**/{segment}/**")
        specs.append(f":(exclude,glob){segment}/**")
    return specs


class WorkspaceRootError(ValueError):
    """Raised when a workspace root is outside the configured allowlist."""


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
    try:
        result = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git is not installed or not on PATH; install Git first") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot inspect Git workspace {target}: {exc}") from exc
    return result.returncode == 0 and bool(result.stdout.strip())


def ensure_service_workspace(root: str | Path, *, auto_init_repo: bool = False) -> Path:
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
    if auto_init_repo:
        return target
    if not is_git_workspace(target):
        raise RuntimeError(
            f"harness-mesh requires a Git workspace (got {target}); "
            "set HARNESS_MESH_WORKSPACE to a Git project, run `git init`, "
            "or enable auto_init_repo"
        )
    return target


_SENSITIVE_ENV_MARKERS = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "_AUTH",
    "_PAT",
)
_SENSITIVE_ENV_EXACT = {
    "AWS_ACCESS_KEY_ID",
    "SSH_AUTH_SOCK",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "DATABASE_URL",
}
_URL_WITH_PASSWORD = re.compile(r"://[^/@\s:]+:[^/@\s]+@")


def scrub_subprocess_env(env: dict[str, str]) -> dict[str, str]:
    """Drop credential-shaped variables while keeping ordinary process state."""

    cleaned: dict[str, str] = {}
    for key, value in env.items():
        name = str(key)
        if name.startswith("HARNESS_MESH_"):
            cleaned[name] = str(value)
            continue
        upper = name.upper()
        if upper in _SENSITIVE_ENV_EXACT:
            continue
        if any(marker in upper for marker in _SENSITIVE_ENV_MARKERS):
            continue
        if _URL_WITH_PASSWORD.search(str(value)):
            continue
        cleaned[name] = str(value)
    return cleaned


__all__ = [
    "SENSITIVE_FILE_PATTERNS",
    "WorkspaceRootError",
    "ensure_service_workspace",
    "is_git_workspace",
    "is_sensitive_name",
    "is_sensitive_path",
    "resolve_allowed_root",
    "scrub_subprocess_env",
]
