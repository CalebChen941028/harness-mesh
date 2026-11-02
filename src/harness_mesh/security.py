"""Security boundaries shared by the router, workspace and context manager."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Iterable, Mapping

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


def filter_sensitive_paths(paths: Iterable[str | Path]) -> tuple[list[str], list[str]]:
    """Split changed paths into ``(visible, skipped)``.

    ``visible`` keeps the input order (deduplicated); ``skipped`` is sorted and
    deduplicated so callers can report exactly which files a diff excluded.
    """

    visible: list[str] = []
    skipped: set[str] = set()
    seen: set[str] = set()
    for item in paths:
        path = str(item)
        if is_sensitive_path(path):
            skipped.add(path)
            continue
        if path in seen:
            continue
        seen.add(path)
        visible.append(path)
    return visible, sorted(skipped)


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


def _is_task_worktree(target: Path) -> bool:
    """True when the path is a task worktree or a directory inside one."""

    parts = target.parts
    return any(
        part == ".harness-mesh" and index + 1 < len(parts) and parts[index + 1] == "worktrees"
        for index, part in enumerate(parts)
    )


def ensure_service_workspace(root: str | Path, *, auto_init_repo: bool = False) -> Path:
    """Reject unsafe MCP/CLI startup workspaces.

    The service must run inside a Git repository and never from the home
    directory or filesystem root, where an automatic repository/baseline could
    capture unrelated files.  A task worktree is also refused: opening a
    second state database there loses the shared ledger and its lineage.
    """

    target = Path(root).expanduser().resolve()
    home = Path.home().resolve()
    if target == Path("/") or target == home:
        raise RuntimeError(
            "harness-mesh refuses to run from the filesystem root or your home directory; "
            "set HARNESS_MESH_WORKSPACE to a project directory"
        )
    if _is_task_worktree(target):
        raise RuntimeError(
            f"harness-mesh refuses to use a task worktree as the workspace root ({target}); "
            "run from the project root, or set HARNESS_MESH_WORKSPACE to the project root"
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


def is_sensitive_env_name(name: str) -> bool:
    """True when an environment variable name marks a credential."""

    upper = str(name).upper()
    if upper in _SENSITIVE_ENV_EXACT:
        return True
    return any(marker in upper for marker in _SENSITIVE_ENV_MARKERS)


def scrub_subprocess_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop credential-shaped variables while keeping ordinary process state."""

    cleaned: dict[str, str] = {}
    for key, value in env.items():
        name = str(key)
        if name.startswith("HARNESS_MESH_"):
            cleaned[name] = str(value)
            continue
        if is_sensitive_env_name(name):
            continue
        if _URL_WITH_PASSWORD.search(str(value)):
            continue
        cleaned[name] = str(value)
    return cleaned


# ---------------------------------------------------------------------------
# credential redaction for text that may be persisted or shown to a caller
# ---------------------------------------------------------------------------
REDACTED = "***"

_OPENAI_STYLE = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}")
_GITHUB_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}")
_GITHUB_PAT = re.compile(r"github_pat_[A-Za-z0-9_]{22,}")
_SLACK_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])xox[baprs]-[A-Za-z0-9-]{10,}")
_AWS_AKID = re.compile(r"(?<![A-Za-z0-9_-])(?:AKIA|ASIA)[0-9A-Z]{16}")
_GOOGLE_API_KEY = re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}")
_HUGGINGFACE_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])hf_[A-Za-z0-9]{30,}")
_NPM_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])npm_[A-Za-z0-9]{30,}")
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_QUOTED_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret)"
    r"\b[\"']?\s*[:=]\s*[\"'])([^\"'\s]{8,})([\"'])"
)
_BARE_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|apikey)\b\s*[:=]\s*)"
    r"(?=[A-Za-z0-9._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{16,}(\b)"
)
_AUTHORIZATION = re.compile(
    r"(?i)(\bAuthorization\b[\"']?\s*[:=]\s*[\"']?)(?:Bearer\s+)?([A-Za-z0-9._~+/=-]{8,})"
)
_URL_PASSWORD = re.compile(r"(://[^/:\s@'\"]+:)([^/@\s]+)(@)")

_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    _OPENAI_STYLE,
    _GITHUB_TOKEN,
    _GITHUB_PAT,
    _SLACK_TOKEN,
    _AWS_AKID,
    _GOOGLE_API_KEY,
    _HUGGINGFACE_TOKEN,
    _NPM_TOKEN,
    _JWT,
)


def _redact_assignment(match: re.Match[str]) -> str:
    suffix = match.group(3) if bool(match.lastindex and match.lastindex >= 3) else ""
    return f"{match.group(1)}{REDACTED}{suffix}"


def redact_text(text: str, *, env: Mapping[str, str] | None = None) -> str:
    """Return ``text`` with credential-shaped substrings replaced by ``***``.

    The caller -- not this helper -- decides where the result is written.  It
    is used before persisting transcripts/diffs and before tool responses or
    plugin metadata leave the process.  Ordinary text is preserved byte for
    byte; only literal secrets and high-confidence credential shapes change.
    """

    redacted = str(text)
    if not redacted:
        return redacted
    if env:
        values = {
            str(value)
            for key, value in env.items()
            if is_sensitive_env_name(key) and len(str(value)) >= 8
        }
        for value in sorted(values, key=len, reverse=True):
            if value:
                redacted = redacted.replace(value, REDACTED)
    redacted = _PEM_PRIVATE_KEY.sub(REDACTED, redacted)
    for pattern in _CREDENTIAL_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    redacted = _QUOTED_ASSIGNMENT.sub(_redact_assignment, redacted)
    redacted = _BARE_ASSIGNMENT.sub(_redact_assignment, redacted)
    redacted = _AUTHORIZATION.sub(_redact_assignment, redacted)
    redacted = _URL_PASSWORD.sub(_redact_assignment, redacted)
    return redacted


def redact_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """Return a copy of ``env`` with credential-shaped values replaced."""

    cleaned: dict[str, str] = {}
    for key, value in (env or {}).items():
        name = str(key)
        text = str(value)
        if is_sensitive_env_name(name) or redact_text(text, env=None) != text:
            cleaned[name] = REDACTED
        else:
            cleaned[name] = text
    return cleaned


__all__ = [
    "REDACTED",
    "SENSITIVE_FILE_PATTERNS",
    "WorkspaceRootError",
    "ensure_service_workspace",
    "filter_sensitive_paths",
    "is_git_workspace",
    "is_sensitive_env_name",
    "is_sensitive_name",
    "is_sensitive_path",
    "redact_env",
    "redact_text",
    "resolve_allowed_root",
    "scrub_subprocess_env",
]
