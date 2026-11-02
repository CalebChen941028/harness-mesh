"""Git worktree isolation, checkpoint capture and patch application."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import cap_text
from .models import ContinuationCheckpoint, TaskRequest, utc_now
from .security import (
    filter_sensitive_paths,
    is_sensitive_path,
    redact_text,
    sensitive_git_pathspecs,
)

logger = logging.getLogger(__name__)


class WorkspaceError(RuntimeError):
    pass


@dataclass
class WorktreeInfo:
    task_id: str
    path: Path
    branch: str
    reused: bool = False
    baseline_commit: str | None = None
    escaping_symlinks: list[str] = field(default_factory=list)


@dataclass
class Changes:
    worktree_path: str
    modified_files: list[str] = field(default_factory=list)
    diff: str = ""
    base_ref: str = "HEAD"
    skipped_sensitive: list[str] = field(default_factory=list)


@dataclass
class ApplyResult:
    task_id: str
    strategy: str
    applied_files: list[str] = field(default_factory=list)
    used_fallback: bool = False
    detail: str = ""
    branch: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "strategy": self.strategy,
            "applied_files": self.applied_files,
            "used_fallback": self.used_fallback,
            "detail": self.detail,
            "branch": self.branch,
            "warnings": self.warnings,
        }


_SAFE_TASK_ID = re.compile(r"[^A-Za-z0-9._-]+")

# Ticket ids become directory names under ``worktrees/`` and branch components,
# so one shared pattern validates them.  The leading alphanumeric rules out
# ".", ".." and any dot-prefixed state directory; ".." and trailing dots are
# rejected explicitly because git accepts both inside a name.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RESERVED_TASK_IDS = frozenset({".meta", "worktrees", "tmp", "mcp", "git-guards"})
_WORKTREE_MARKER_NAME = ".harness-mesh-worktree.json"
_META_FILE_NAMES = frozenset({".harness-mesh-lineage.json", _WORKTREE_MARKER_NAME})
# Maximum argv bytes handed to one ``git diff`` when the installed Git build
# lacks ``--pathspec-from-file`` for ``git diff``; bigger changes are split
# into independently generated (and concatenable) patch chunks.
_DIFF_ARGV_CHUNK_CHARS = 60_000
_GIT_QUOTED_ESCAPES = {
    "a": 7,
    "b": 8,
    "f": 12,
    "n": 10,
    "r": 13,
    "t": 9,
    "v": 11,
    "\\": 92,
    '"': 34,
}


def _unquote_git_path(token: str) -> str:
    """Decode a C-style quoted path as emitted by ``git diff``/``git status``.

    Git quotes paths that contain non-ASCII bytes, control characters, quotes
    or backslashes (``core.quotePath`` defaults to true).  Parsers that only
    look for a literal ``+++ b/`` prefix are trivially bypassed by a filename
    such as ``docs/链接``; this decoder normalises both forms first.
    """

    text = str(token).strip()
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text
    body = text[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.extend(char.encode("utf-8", "surrogateescape"))
            index += 1
            continue
        index += 1
        if index >= len(body):
            out.extend(b"\\")
            break
        escape = body[index]
        if escape in _GIT_QUOTED_ESCAPES:
            out.append(_GIT_QUOTED_ESCAPES[escape])
            index += 1
            continue
        if escape in "01234567":
            digits = ""
            while index < len(body) and len(digits) < 3 and body[index] in "01234567":
                digits += body[index]
                index += 1
            out.append(int(digits, 8) & 0xFF)
            continue
        out.extend(escape.encode("utf-8", "surrogateescape"))
        index += 1
    return out.decode("utf-8", "surrogateescape")


def _is_meta_file(relative: str) -> bool:
    return Path(relative).name in _META_FILE_NAMES


def _meta_git_excludes() -> list[str]:
    specs: list[str] = []
    for name in sorted(_META_FILE_NAMES):
        specs.append(f":(exclude){name}")
        specs.append(f":(exclude)**/{name}")
    return specs


def _parse_name_status_z(payload: str) -> list[str]:
    """Path list from ``git diff --name-status -z`` (status NUL path NUL)."""

    entries = str(payload).split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        status = entries[index]
        index += 1
        if not status:
            continue
        if index >= len(entries):
            break
        first = entries[index]
        index += 1
        paths.append(first)
        if status[0] in ("R", "C") and index < len(entries):
            second = entries[index]
            index += 1
            paths.append(second)
    return [path for path in paths if path]


def _parse_raw_z(payload: str) -> list[dict[str, str]]:
    """Entries of ``git diff --raw -z`` (``:modes shas status NUL path NUL``)."""

    entries = str(payload).split("\0")
    records: list[dict[str, str]] = []
    index = 0
    while index + 1 < len(entries):
        header = entries[index]
        path = entries[index + 1]
        index += 2
        if not header.startswith(":"):
            continue
        fields = header[1:].split()
        if len(fields) < 5:
            continue
        records.append(
            {
                "old_mode": fields[0],
                "new_mode": fields[1],
                "status": fields[4],
                "path": path,
            }
        )
    return records


def _diff_header_path(raw: str) -> str | None:
    """Return the repository-relative path from a ``+++``/``---`` header."""

    value = str(raw).strip()
    if not value or value == "/dev/null":
        return None
    value = value.split("\t", 1)[0]
    if value.startswith('"'):
        value = _unquote_git_path(value)
    if value.startswith("b/") or value.startswith("a/"):
        value = value[2:]
    return value


def _parse_diff_blocks(diff: str) -> list[dict[str, Any]]:
    """Split a unified diff into blocks with decoded paths and modes."""

    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in str(diff).splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                blocks.append(current)
            current = {
                "path": None,
                "old_path": None,
                "new_mode": None,
                "plus_lines": [],
                "in_hunk": False,
            }
            continue
        if current is None:
            continue
        if line.startswith("new file mode "):
            current["new_mode"] = line[len("new file mode ") :].strip().split()[0]
        elif line.startswith("new mode "):
            current["new_mode"] = line[len("new mode ") :].strip().split()[0]
        elif line.startswith("index "):
            fields = line.split()
            if len(fields) >= 3 and current["new_mode"] is None:
                mode = fields[-1]
                if len(mode) == 6 and mode.isdigit():
                    current["new_mode"] = mode
        elif line.startswith("--- "):
            current["old_path"] = _diff_header_path(line[4:])
        elif line.startswith("+++ "):
            current["path"] = _diff_header_path(line[4:])
        elif line.startswith("@@"):
            current["in_hunk"] = True
        elif current["in_hunk"] and line.startswith("+"):
            current["plus_lines"].append(line[1:])
    if current is not None:
        blocks.append(current)
    return blocks


def _filter_diff_text(diff: str) -> tuple[str, list[str]]:
    """Drop diff blocks whose target path is sensitive or mesh-owned."""

    text = str(diff)
    if not text.strip():
        return "", []
    if not _parse_diff_blocks(text):
        return text, []
    kept: list[str] = []
    skipped: set[str] = set()
    current: list[str] = []
    current_path: str | None = None
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                if current_path is not None and (
                    is_sensitive_path(current_path) or _is_meta_file(current_path)
                ):
                    skipped.add(current_path)
                else:
                    kept.extend(current)
            current = [line]
            current_path = None
            continue
        current.append(line)
        if line.startswith("+++ "):
            current_path = _diff_header_path(line[4:])
        elif line.startswith("--- ") and current_path is None:
            current_path = _diff_header_path(line[4:])
    if current:
        if current_path is not None and (
            is_sensitive_path(current_path) or _is_meta_file(current_path)
        ):
            skipped.add(current_path)
        else:
            kept.extend(current)
    return "".join(kept), sorted(skipped)


class WorkspaceManager:
    """Owns ``.harness-mesh/worktrees/<task_id>`` for every ticket.

    A ticket keeps the same worktree for its whole lifetime, so Assignment #2
    on a different harness literally continues in the same directory where
    Assignment #1 was interrupted.  Worktrees are registered with Git so the
    main workspace is never mutated until ``apply_worktree_changes`` is called.
    """

    def __init__(
        self,
        workspace_root: str | Path,
        state_dir_name: str | Path = ".harness-mesh",
        *,
        git_binary: str = "git",
        env: Mapping[str, str] | None = None,
        baseline_ref: str = "HEAD",
        auto_init_repo: bool = False,
    ):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        state_candidate = Path(os.path.expanduser(str(state_dir_name)))
        self.state_dir = state_candidate if state_candidate.is_absolute() else self.workspace_root / state_candidate
        self.worktrees_root = self.state_dir / "worktrees"
        self.mcp_dir = self.state_dir / "mcp"
        self.tmp_dir = self.state_dir / "tmp"
        self.git_guard_dir = self.state_dir / "git-guards"
        self.git_binary = git_binary
        self.env = dict(os.environ)
        if env:
            self.env.update(env)
        self.env.setdefault("GIT_TERMINAL_PROMPT", "0")
        self.baseline_ref = baseline_ref
        self.auto_init_repo = bool(auto_init_repo)
        # ``--pathspec-from-file`` is not available in every git build (the
        # Apple Git shipped on macOS 15 rejects it for ``git diff``), so the
        # capability is probed once and argv pathspecs are the fallback.
        self._supports_pathspec_file: bool | None = None

    # ------------------------------------------------------------------
    # low-level git helpers
    # ------------------------------------------------------------------
    def _git(
        self,
        args: Iterable[str],
        *,
        cwd: Path | None = None,
        check: bool = False,
        timeout: int = 120,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        command = [
            self.git_binary,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.pager=cat",
            "-c",
            "diff.external=",
            "-c",
            "protocol.ext.allow=never",
            *args,
        ]
        try:
            process = subprocess.run(
                command,
                cwd=str(cwd or self.workspace_root),
                capture_output=True,
                text=True,
                timeout=timeout,
                input=input_text,
                env=self.env,
            )
        except FileNotFoundError as exc:  # pragma: no cover - environment dependent
            raise WorkspaceError(f"git binary not found: {self.git_binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceError(f"git command timed out: {' '.join(command)}") from exc
        if check and process.returncode != 0:
            raise WorkspaceError(
                f"git command failed ({process.returncode}): {' '.join(command)}\n"
                f"stdout: {process.stdout}\nstderr: {process.stderr}"
            )
        return process

    def _git_common_dir(self) -> Path | None:
        result = self._git(["rev-parse", "--git-common-dir"], cwd=self.workspace_root)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        common = Path(result.stdout.strip())
        if not common.is_absolute():
            common = (self.workspace_root / common).resolve()
        return common

    @staticmethod
    def _hash_file(path: Path) -> str | None:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    def _git_metadata_snapshot(self, task_id: str | None = None) -> dict:
        common = self._git_common_dir()
        if common is None:
            return {}
        config = common / "config"
        config_hash = self._hash_file(config) if config.exists() else None
        attributes = common / "info" / "attributes"
        attributes_hash = self._hash_file(attributes) if attributes.exists() else None
        hooks = common / "hooks"
        hook_hashes: dict[str, str] = {}
        if hooks.is_dir():
            for entry in sorted(hooks.rglob("*")):
                if entry.is_file() and not entry.is_symlink():
                    digest = self._hash_file(entry)
                    if digest is not None:
                        hook_hashes[entry.relative_to(hooks).as_posix()] = digest
        snapshot = {
            "config": config_hash,
            "attributes": attributes_hash,
            "hooks": hook_hashes,
            "worktree_git_file": None,
            "worktree_gitdir": None,
            "gitdir_commondir": None,
        }
        if task_id is None:
            return snapshot
        try:
            worktree = self.worktree_path(task_id)
        except WorkspaceError:
            return snapshot
        snapshot["worktree_git_file"] = self._hash_file(worktree / ".git")
        try:
            gitdir = self._parse_worktree_gitdir(worktree)
        except WorkspaceError:
            gitdir = None
        if gitdir is not None:
            snapshot["worktree_gitdir"] = str(gitdir)
            snapshot["gitdir_commondir"] = self._hash_file(gitdir / "commondir")
        return snapshot

    def _git_guard_path(self, task_id: str) -> Path:
        safe = _SAFE_TASK_ID.sub("_", task_id)
        return self.git_guard_dir / f"{safe}.json"

    # ------------------------------------------------------------------
    # task identity / worktree integrity
    # ------------------------------------------------------------------
    @staticmethod
    def validate_task_id(task_id: str) -> str:
        """Return a safe ticket id or raise ``WorkspaceError``.

        The same rule as ``TaskRequest`` keeps every caller (MCP, CLI,
        internal resumptions) from smuggling path components into
        ``worktrees/<task_id>`` or into a git branch name.
        """

        text = str(task_id or "").strip()
        if not _TASK_ID_RE.fullmatch(text):
            raise WorkspaceError(
                "invalid task_id: expected ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
            )
        if ".." in text:
            raise WorkspaceError("invalid task_id: '..' is not allowed")
        if text.endswith("."):
            raise WorkspaceError("invalid task_id: trailing '.' is not allowed")
        if text in _RESERVED_TASK_IDS:
            raise WorkspaceError(f"invalid task_id: {text!r} is reserved")
        return text

    def _parse_worktree_gitdir(self, worktree: Path) -> Path:
        """Validate ``<worktree>/.git`` and return the recorded gitdir.

        A worktree's ``.git`` is an ordinary file inside the agent-writable
        directory.  Rewriting it lets the agent point mesh-side git commands
        at an attacker-controlled gitdir (whose config can execute a clean
        filter), so mesh only accepts a regular ``gitdir:`` file whose target
        is ``<common>/worktrees/<name>`` and whose backlink points back at
        this exact worktree.
        """

        gitfile = worktree / ".git"
        if gitfile.is_symlink() or not gitfile.is_file():
            raise WorkspaceError(f"worktree .git is not a regular file: {gitfile}")
        try:
            content = gitfile.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise WorkspaceError(f"cannot read worktree .git file {gitfile}: {exc}") from exc
        stripped = content.strip()
        if not stripped.lower().startswith("gitdir:") or "\n" in stripped:
            raise WorkspaceError(f"worktree .git has an invalid header: {gitfile}")
        raw = stripped.split(":", 1)[1].strip()
        if not raw:
            raise WorkspaceError(f"worktree .git has an empty gitdir target: {gitfile}")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = worktree / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise WorkspaceError(f"cannot resolve worktree gitdir {raw!r}: {exc}") from exc
        if not resolved.is_dir():
            raise WorkspaceError(f"worktree gitdir is not a directory: {resolved}")
        common = self._git_common_dir()
        if common is None:
            raise WorkspaceError("cannot resolve the repository common git directory")
        admin_root = (common / "worktrees").resolve()
        if resolved == admin_root or not self._is_relative_to(resolved, admin_root):
            raise WorkspaceError(
                f"worktree gitdir is outside {admin_root}: {resolved}"
            )
        backlink = resolved / "gitdir"
        if backlink.exists():
            try:
                expected_raw = backlink.read_text(encoding="utf-8", errors="replace").strip()
            except OSError as exc:
                raise WorkspaceError(f"cannot read worktree gitdir backlink: {exc}") from exc
            if expected_raw:
                expected = Path(expected_raw)
                if not expected.is_absolute():
                    expected = resolved / expected
                try:
                    expected_path = expected.resolve()
                except OSError:
                    expected_path = expected
                if expected_path != gitfile.resolve():
                    raise WorkspaceError(
                        "worktree gitdir backlink does not point at this worktree"
                    )
        return resolved

    def _validated_worktree_gitdir(self, task_id: str, *, path: Path | None = None) -> Path:
        """Validate a task worktree's ``.git`` pointer before any git command."""

        worktree = Path(path) if path is not None else self.worktree_path(task_id)
        if not worktree.exists():
            raise WorkspaceError(f"worktree does not exist for task {task_id}: {worktree}")
        return self._parse_worktree_gitdir(worktree)

    def _recognised_legacy_worktree(self, task_id: str, worktree: Path, branch: str) -> bool:
        """True for a pre-marker worktree that provably belongs to this ticket.

        Upgrade path only.  Everything is checked before the mesh writes a
        marker: the ``.git`` pointer must pass full validation, the baseline
        metadata must match task/branch/path, the checked-out branch must be
        the ticket branch and the repository toplevel must be the worktree
        itself.  Any failed check leaves the directory untouched.
        """

        try:
            self._validated_worktree_gitdir(task_id, path=worktree)
        except WorkspaceError:
            return False
        marker = self._read_baseline_marker(task_id)
        if not marker:
            return False
        if str(marker.get("task_id") or "") != task_id:
            return False
        if str(marker.get("branch") or "") != branch:
            return False
        recorded_path = marker.get("worktree_path")
        if recorded_path:
            try:
                if Path(str(recorded_path)).expanduser().resolve() != worktree.resolve():
                    return False
            except OSError:
                return False
        current = self._git(["symbolic-ref", "--short", "HEAD"], cwd=worktree)
        if current.returncode != 0 or current.stdout.strip() != branch:
            return False
        toplevel = self._git(["rev-parse", "--show-toplevel"], cwd=worktree)
        if toplevel.returncode != 0:
            return False
        try:
            if Path(toplevel.stdout.strip()).resolve() != worktree.resolve():
                return False
        except OSError:
            return False
        return True

    def _validate_worktree_for_path(self, target: Path) -> None:
        """Validate the owning task worktree for ``target`` (if any).

        ``target`` is normally the worktree root, but a caller may pass a
        directory inside it; the outermost component under ``worktrees/`` is
        validated in that case so no git command ever runs against an
        unvalidated worktree.
        """

        try:
            resolved = Path(target).resolve()
            root = self.worktrees_root.resolve()
        except OSError:
            return
        if resolved == root:
            return
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            return
        parts = relative.parts
        if not parts:
            return
        candidate = root / parts[0]
        if not candidate.is_dir():
            return
        try:
            self._validated_worktree_gitdir(parts[0], path=candidate)
        except WorkspaceError:
            # Only fail closed for directories that actually look like a
            # worktree; unrelated entries under the state dir stay ignorable.
            if (candidate / ".git").exists():
                raise

    # ------------------------------------------------------------------
    # worktree marker
    # ------------------------------------------------------------------
    @staticmethod
    def _worktree_marker_path(worktree: Path) -> Path:
        return Path(worktree) / _WORKTREE_MARKER_NAME

    def _read_worktree_marker(self, worktree: Path) -> dict[str, Any] | None:
        path = self._worktree_marker_path(worktree)
        if path.is_symlink() or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_worktree_marker(self, info: WorktreeInfo) -> None:
        path = self._worktree_marker_path(info.path)
        try:
            path.write_text(
                json.dumps(
                    {"task_id": info.task_id, "branch": info.branch},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            path.chmod(0o444)
        except OSError as exc:
            logger.warning("unable to write worktree marker for %s: %s", info.task_id, exc)

    def _record_git_guard(self, task_id: str) -> None:
        guard_path = self._git_guard_path(task_id)
        if guard_path.exists():
            return
        try:
            snapshot = self._git_metadata_snapshot(task_id)
            self.git_guard_dir.mkdir(parents=True, exist_ok=True)
            payload = {"task_id": task_id, "snapshot": snapshot}
            guard_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except (OSError, WorkspaceError) as exc:
            logger.warning("unable to record Git metadata guard for %s: %s", task_id, exc)

    def verify_git_guard(self, task_id: str) -> None:
        self._verify_git_guard(task_id)

    def _verify_git_guard(self, task_id: str) -> None:
        guard_path = self._git_guard_path(task_id)
        if not guard_path.exists():
            logger.warning("no Git metadata guard recorded for %s; applying without comparison", task_id)
            return
        try:
            saved = json.loads(guard_path.read_text(encoding="utf-8")).get("snapshot", {})
        except (OSError, json.JSONDecodeError):
            raise WorkspaceError(f"Git metadata guard for {task_id} is unreadable")
        if saved and "worktree_git_file" not in saved:
            # Guard recorded by a release before worktree ``.git`` hashing
            # existed.  Re-record it once from the current (already validated)
            # state instead of aborting every upgraded ticket.
            logger.warning(
                "upgrading legacy Git metadata guard for task %s", task_id
            )
            try:
                snapshot = self._git_metadata_snapshot(task_id)
                payload = {"task_id": task_id, "snapshot": snapshot}
                guard_path.write_text(
                    json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
                )
            except OSError:
                pass
            return
        current = self._git_metadata_snapshot(task_id)
        if saved == current:
            return
        changes: list[str] = []
        if saved.get("config") != current.get("config"):
            changes.append(".git/config changed")
        if saved.get("attributes") != current.get("attributes"):
            changes.append(".git/info/attributes changed")
        old_hooks = saved.get("hooks") or {}
        new_hooks = current.get("hooks") or {}
        for name in sorted(set(old_hooks) | set(new_hooks)):
            if old_hooks.get(name) != new_hooks.get(name):
                action = "added" if name not in old_hooks else "removed" if name not in new_hooks else "modified"
                changes.append(f".git/hooks/{name} {action}")
        raise WorkspaceError(
            f"Git metadata changed since task {task_id} started: {'; '.join(changes)}"
        )

    def is_git_repo(self, path: Path | None = None) -> bool:
        target = path or self.workspace_root
        if not target.exists():
            return False
        result = self._git(["rev-parse", "--is-inside-work-tree"], cwd=target)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _is_repo_toplevel(self) -> bool:
        result = self._git(["rev-parse", "--show-toplevel"])
        if result.returncode != 0:
            return False
        try:
            return Path(result.stdout.strip()).resolve() == self.workspace_root
        except OSError:
            return False

    def ensure_repository(self) -> None:
        """Create a repository + baseline commit when the workspace has none.

        Worktrees require at least one commit.  The baseline is only created
        when no ``HEAD`` exists, and the mesh state directory is added to
        ``.git/info/exclude`` so ticket data never pollutes user commits.
        """

        self.workspace_root.mkdir(parents=True, exist_ok=True)
        if not self.is_git_repo() or not self._is_repo_toplevel():
            if not self.auto_init_repo:
                raise WorkspaceError(
                    f"workspace is not a Git repository: {self.workspace_root}; "
                    "run `git init` there or set auto_init_repo: true in harness-mesh config"
                )
            self._git(["init", "-q"], check=True)
        self._ensure_git_excludes()
        head = self._git(["rev-parse", "--verify", "HEAD"])
        if head.returncode != 0:
            self._git(["add", "-A", "--", ".", *sensitive_git_pathspecs()], check=True)
            self._git(
                [
                    "-c",
                    "user.name=Harness Mesh",
                    "-c",
                    "user.email=mesh@localhost",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "harness-mesh: baseline for worktree isolation",
                ],
                check=True,
            )

    def _ensure_git_excludes(self) -> None:
        git_dir = self._git(["rev-parse", "--git-dir"], cwd=self.workspace_root)
        if git_dir.returncode != 0:
            return
        git_dir_path = Path(git_dir.stdout.strip())
        if not git_dir_path.is_absolute():
            git_dir_path = (self.workspace_root / git_dir_path).resolve()
        info_dir = git_dir_path / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude_file = info_dir / "exclude"
        existing = exclude_file.read_text(encoding="utf-8", errors="replace") if exclude_file.exists() else ""
        entries: list[str] = []
        relative_state = self.state_dir.relative_to(self.workspace_root) if self._is_relative_to(
            self.state_dir, self.workspace_root
        ) else None
        entries.append(f"/{relative_state.as_posix()}/" if relative_state else str(self.state_dir))
        # ``state_dir`` comes from configuration; a newline would inject
        # arbitrary ignore patterns (for example ``*``) into the repository.
        entries = [entry for entry in entries if entry.strip() and "\n" not in entry and "\r" not in entry]
        existing_lines = set(existing.splitlines())
        additions = [entry for entry in entries if entry not in existing_lines]
        if additions:
            with exclude_file.open("a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                for entry in additions:
                    handle.write(entry + "\n")

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
            return True
        except ValueError:
            return False

    def _safe_id(self, task_id: str) -> str:
        # The id is both a directory name and a branch component.  Validate
        # instead of collapsing characters: collapsing lets distinct ids such
        # as "victim_ticket" and "victim/ticket" share one worktree.
        return self.validate_task_id(task_id)

    def worktree_path(self, task_id: str) -> Path:
        return self.worktrees_root / self._safe_id(task_id)

    def branch_name(self, task_id: str) -> str:
        return f"harness-mesh/{self._safe_id(task_id)}"

    # ------------------------------------------------------------------
    # worktree lifecycle
    # ------------------------------------------------------------------
    def _meta_dir(self) -> Path:
        return self.worktrees_root / ".meta"

    def _baseline_marker(self, task_id: str) -> Path:
        return self._meta_dir() / f"{self._safe_id(task_id)}.json"

    def _read_baseline_marker(self, task_id: str) -> dict[str, Any] | None:
        path = self._baseline_marker(task_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_baseline_marker(self, info: WorktreeInfo) -> None:
        path = self._baseline_marker(info.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "task_id": info.task_id,
                    "branch": info.branch,
                    "worktree_path": str(info.path),
                    "baseline_commit": info.baseline_commit,
                    "created_at": utc_now(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def baseline_commit(self, task_id: str) -> str | None:
        marker = self._read_baseline_marker(task_id)
        if marker and marker.get("baseline_commit"):
            return str(marker["baseline_commit"])
        path = self.worktree_path(task_id)
        if path.exists():
            self._validated_worktree_gitdir(task_id, path=path)
            result = self._git(["rev-parse", "HEAD"], cwd=path)
            if result.returncode == 0:
                return result.stdout.strip()
        return None

    def baseline_for_path(self, path: str | Path) -> str | None:
        target = Path(path).expanduser().resolve()
        try:
            if target.parent == self.worktrees_root.resolve():
                return self.baseline_commit(target.name)
        except OSError:
            return None
        return None

    def create_worktree(
        self,
        task_id: str,
        *,
        base_ref: str | None = None,
        sync_uncommitted: bool = True,
    ) -> WorktreeInfo:
        task_id = self.validate_task_id(task_id)
        self.ensure_repository()
        path = self.worktree_path(task_id)
        branch = self.branch_name(task_id)
        if path.exists():
            if path.is_symlink() or not path.is_dir():
                raise WorkspaceError(f"worktree path is not a plain directory: {path}")
            if (path / ".git").exists():
                marker = self._read_worktree_marker(path)
                if marker is None:
                    # Worktrees created before the marker existed are adopted
                    # once, but only after strict validation: .git pointer,
                    # expected branch, matching baseline metadata and toplevel.
                    if not self._recognised_legacy_worktree(task_id, path, branch):
                        raise WorkspaceError(
                            "worktree directory exists without a matching marker file "
                            f"({_WORKTREE_MARKER_NAME}) or compatible baseline metadata: {path}"
                        )
                    logger.warning(
                        "adopting marker-less worktree for task %s after validation", task_id
                    )
                    self._write_worktree_marker(
                        WorktreeInfo(task_id=task_id, path=path, branch=branch)
                    )
                elif (
                    str(marker.get("task_id") or "") != task_id
                    or str(marker.get("branch") or "") != branch
                ):
                    raise WorkspaceError(
                        f"worktree marker mismatch ({_WORKTREE_MARKER_NAME}): {path}"
                    )
                self._validated_worktree_gitdir(task_id, path=path)
                baseline = self._read_baseline_marker(task_id) or {}
                baseline_commit = baseline.get("baseline_commit")
                if not baseline_commit:
                    head = self._git(["rev-parse", "HEAD"], cwd=path)
                    baseline_commit = head.stdout.strip() if head.returncode == 0 else None
                self._record_git_guard(task_id)
                return WorktreeInfo(
                    task_id=task_id,
                    path=path,
                    branch=branch,
                    reused=True,
                    baseline_commit=str(baseline_commit) if baseline_commit else None,
                )
            # Stale directory from a crashed run: remove only when it is safely
            # contained inside our own worktrees root.
            self._safe_remove_dir(path, self.worktrees_root)
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        ref = base_ref or self.baseline_ref
        branch_exists = (
            self._git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]).returncode == 0
        )

        def _add() -> subprocess.CompletedProcess:
            if branch_exists:
                return self._git(["worktree", "add", str(path), branch])
            return self._git(["worktree", "add", "-b", branch, str(path), ref])

        result = _add()
        if result.returncode != 0:
            self._git(["worktree", "prune"])
            branch_exists = (
                self._git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]).returncode == 0
            )
            result = _add()
        if result.returncode != 0:
            raise WorkspaceError(
                f"failed to create worktree for {task_id}: {result.stderr.strip() or result.stdout.strip()}"
            )
        info = WorktreeInfo(task_id=task_id, path=path, branch=branch, reused=False)
        # ``git worktree add`` just wrote ``.git``; anything else means the
        # directory was tampered with between creation and use.
        self._validated_worktree_gitdir(task_id, path=path)
        if sync_uncommitted:
            self._sync_uncommitted_changes(path)
            self._commit_worktree_baseline(task_id, path, "harness-mesh: baseline sync")
        head = self._git(["rev-parse", "HEAD"], cwd=path)
        info.baseline_commit = head.stdout.strip() if head.returncode == 0 else None
        info.escaping_symlinks = self._normalize_worktree_symlinks(path)
        self._write_baseline_marker(info)
        self._write_lineage_marker(path, task_id)
        self._write_worktree_marker(info)
        self._record_git_guard(task_id)
        return info

    def _write_lineage_marker(self, worktree_path: Path, task_id: str) -> None:
        marker = worktree_path / ".harness-mesh-lineage.json"
        try:
            marker.write_text(json.dumps({"task_id": task_id}), encoding="utf-8")
            marker.chmod(0o444)
        except OSError as exc:
            logger.warning("unable to write lineage marker for %s: %s", task_id, exc)

    def _recreate_symlink(
        self,
        source: Path,
        destination: Path,
        source_root: Path,
        destination_root: Path,
    ) -> bool:
        """Recreate an internal symlink as a relative link inside the destination."""

        if not source.is_symlink() or not source.exists():
            logger.warning("skipping symlink %s: dangling or missing target", source)
            return False
        try:
            resolved = source.resolve()
        except OSError:
            logger.warning("skipping symlink %s: cannot resolve target", source)
            return False
        if is_sensitive_path(resolved):
            logger.warning("skipping symlink %s: target name is sensitive", source)
            return False
        try:
            relative_target = resolved.relative_to(source_root.resolve())
        except ValueError:
            logger.warning("skipping symlink %s: target escapes source workspace", source)
            return False
        destination_target = destination_root.resolve() / relative_target
        if not self._is_relative_to(destination_target, destination_root.resolve()):
            logger.warning("skipping symlink %s: destination target escapes workspace", source)
            return False
        link_target = os.path.relpath(destination_target, destination.parent)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or destination.exists():
            if destination.is_dir() and not destination.is_symlink():
                raise WorkspaceError(f"cannot replace directory with symlink: {destination}")
            destination.unlink()
        os.symlink(link_target, destination)
        return True

    def _normalize_worktree_symlinks(self, worktree_path: Path) -> list[str]:
        """Rewrite absolute links inside the worktree and report escaping links."""

        worktree_root = worktree_path.resolve()
        primary_root = self.workspace_root.resolve()
        escaping: list[str] = []
        for entry in sorted(worktree_path.rglob("*")):
            if not entry.is_symlink():
                continue
            if not entry.exists():
                escaping.append(entry.relative_to(worktree_path).as_posix())
                continue
            try:
                resolved = entry.resolve(strict=False)
            except OSError:
                escaping.append(entry.relative_to(worktree_path).as_posix())
                continue
            if self._is_relative_to(resolved, worktree_root):
                raw_target = os.readlink(entry)
                if Path(raw_target).is_absolute():
                    entry.unlink()
                    os.symlink(os.path.relpath(resolved, entry.parent), entry)
                continue
            if self._is_relative_to(resolved, primary_root) and not is_sensitive_path(resolved):
                relative_target = resolved.relative_to(primary_root)
                destination_target = worktree_root / relative_target
                entry.unlink()
                os.symlink(os.path.relpath(destination_target, entry.parent), entry)
                continue
            relative = entry.relative_to(worktree_path).as_posix()
            escaping.append(relative)
            logger.warning("worktree symlink escapes workspace: %s", relative)
        return sorted(set(escaping))

    def _sync_uncommitted_changes(self, worktree_path: Path) -> None:
        """Copy the primary checkout's uncommitted state into a new worktree.

        Tracked modifications/deletions are applied from a binary-safe diff;
        untracked non-ignored files are copied.  The subsequent baseline commit
        makes this inherited state invisible to ``capture_changes``.
        """

        self._parse_worktree_gitdir(worktree_path)
        diff = self._git(["diff", "HEAD", "--binary", "--no-color"], cwd=self.workspace_root)
        if diff.returncode == 0 and diff.stdout.strip():
            applied = self._git(
                ["apply", "--whitespace=nowarn", "--recount", "--binary", "-"],
                cwd=worktree_path,
                input_text=diff.stdout,
            )
            if applied.returncode != 0:
                self._copy_dirty_tracked_files(worktree_path)

        status = self._git(["status", "--porcelain", "-z"], cwd=self.workspace_root)
        if status.returncode == 0:
            for relative in self._porcelain_paths(status.stdout):
                if self._is_state_path(relative) or _is_meta_file(relative) or is_sensitive_path(relative):
                    continue
                src = self.workspace_root / relative
                dst = worktree_path / relative
                if src.is_symlink():
                    self._recreate_symlink(src, dst, self.workspace_root, worktree_path)
                elif src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                elif not src.exists() and dst.exists():
                    dst.unlink()

    def _copy_dirty_tracked_files(self, worktree_path: Path) -> None:
        status = self._git(["status", "--porcelain", "-z"], cwd=self.workspace_root)
        if status.returncode != 0:
            return
        for relative in self._porcelain_paths(status.stdout):
            if self._is_state_path(relative) or is_sensitive_path(relative):
                continue
            src = self.workspace_root / relative
            dst = worktree_path / relative
            if src.is_symlink():
                self._recreate_symlink(src, dst, self.workspace_root, worktree_path)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            elif not src.exists() and dst.exists():
                dst.unlink()

    def _is_state_path(self, relative: str) -> bool:
        if _is_meta_file(relative):
            return True
        try:
            state_rel = self.state_dir.resolve().relative_to(self.workspace_root)
        except ValueError:
            return False
        return relative == state_rel.as_posix() or relative.startswith(state_rel.as_posix() + "/")

    def _commit_worktree_baseline(self, task_id: str, worktree_path: Path, message: str) -> str | None:
        self._validated_worktree_gitdir(task_id, path=worktree_path)
        self._git(
            ["add", "-A", "--", ".", *sensitive_git_pathspecs(), *_meta_git_excludes()],
            cwd=worktree_path,
        )
        result = self._git(
            [
                "-c",
                "user.name=Harness Mesh",
                "-c",
                "user.email=mesh@localhost",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                message,
            ],
            cwd=worktree_path,
        )
        if result.returncode != 0:
            raise WorkspaceError(
                f"failed to commit baseline for {task_id}: {result.stderr.strip() or result.stdout.strip()}"
            )
        head = self._git(["rev-parse", "HEAD"], cwd=worktree_path)
        return head.stdout.strip() if head.returncode == 0 else None

    def inherit_diff(self, task_id: str, diff: str, *, label: str = "upstream dependency") -> bool:
        """Apply an upstream task diff into a worktree and fold it into baseline."""

        task_id = self.validate_task_id(task_id)
        if not str(diff).strip():
            return False
        path = self.worktree_path(task_id)
        self._validated_worktree_gitdir(task_id, path=path)
        if not path.exists():
            raise WorkspaceError(f"worktree does not exist for task {task_id}")
        filtered, skipped = _filter_diff_text(diff)
        if skipped:
            logger.info(
                "inherited %s diff skipped sensitive paths: %s", label, ", ".join(skipped)
            )
        if not filtered.strip():
            return False
        self._check_diff_symlinks(filtered, path, path)
        applied = self._git(
            ["apply", "--whitespace=nowarn", "--recount", "--binary", "-"],
            cwd=path,
            input_text=filtered,
        )
        if applied.returncode != 0:
            return False
        baseline = self._commit_worktree_baseline(
            task_id, path, f"harness-mesh: inherit {label}"
        )
        info = WorktreeInfo(task_id=task_id, path=path, branch=self.branch_name(task_id))
        info.baseline_commit = baseline
        self._write_baseline_marker(info)
        return True

    def remove_worktree(self, task_id: str, *, delete_branch: bool = False) -> None:
        task_id = self.validate_task_id(task_id)
        path = self.worktree_path(task_id)
        branch = self.branch_name(task_id)
        if path.exists():
            self._git(["worktree", "remove", "--force", str(path)])
            if path.exists():
                self._safe_remove_dir(path, self.worktrees_root)
        self._git(["worktree", "prune"])
        marker = self._baseline_marker(task_id)
        if marker.exists():
            marker.unlink()
        if delete_branch:
            self._git(["branch", "-D", branch])

    def list_worktrees(self) -> list[dict[str, str]]:
        result = self._git(["worktree", "list", "--porcelain"])
        if result.returncode != 0:
            return []
        items: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                if current:
                    items.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current:
            items.append(current)
        return items

    # ------------------------------------------------------------------
    # change capture
    # ------------------------------------------------------------------
    @staticmethod
    def _porcelain_paths(payload: str) -> list[str]:
        entries = str(payload).split("\0")
        paths: list[str] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry or len(entry) < 4:
                continue
            status = entry[:2]
            file_path = entry[3:]
            if status[0] in ("R", "C") and index < len(entries):
                # The original path is stored in the next NUL separated entry.
                index += 1
            if file_path:
                paths.append(file_path)
        return paths

    def _status_paths(self, target: Path) -> list[str]:
        result = self._git(["status", "--porcelain", "-z"], cwd=target)
        if result.returncode != 0:
            return []
        return self._porcelain_paths(result.stdout)

    def _raw_changed_paths(self, target: Path, ref: str | None) -> tuple[list[str], bool]:
        """Changed paths from ``git diff --name-status -z`` (paths unfiltered)."""

        args = ["diff", "--name-status", "-z"]
        if ref:
            args.append(ref)
        args.append("--")
        result = self._git(args, cwd=target)
        if result.returncode != 0:
            return [], False
        return _parse_name_status_z(result.stdout), True

    def _expand_directory_entries(self, target: Path, paths: list[str]) -> list[str]:
        """Expand porcelain's collapsed untracked directories into files.

        ``git status --porcelain`` reports a fully untracked directory as one
        entry such as ``deep/``; filtering that single entry would hide
        sensitive files nested inside it.
        """

        expanded: list[str] = []
        for path in paths:
            if path.endswith("/"):
                directory = target / path
                try:
                    is_directory = directory.is_dir() and not directory.is_symlink()
                except OSError:
                    is_directory = False
                if is_directory:
                    for child in sorted(directory.rglob("*")):
                        if child.is_file() or child.is_symlink():
                            expanded.append(child.relative_to(target).as_posix())
                    continue
            expanded.append(path)
        return expanded

    def _visible_paths(self, target: Path, ref: str | None, paths: list[str]) -> list[str]:
        """Filter mesh-owned and sensitive paths out of a changed-path list."""

        candidates = [
            path
            for path in paths
            if path and not self._is_state_path(path) and not _is_meta_file(path)
        ]
        visible, _skipped = filter_sensitive_paths(candidates)
        return visible

    def _diff_visible_paths(self, target: Path, ref: str | None, paths: list[str]) -> str:
        """Generate a diff restricted to ``paths`` (literal pathspecs only)."""

        if not paths:
            return ""
        base_args = ["--literal-pathspecs", "diff", "--no-ext-diff", "--no-color"]
        if ref:
            base_args.append(ref)
        if self._supports_pathspec_file is not False:
            result = self._git(
                [*base_args, "--pathspec-from-file=-", "--pathspec-file-nul"],
                cwd=target,
                input_text="\0".join(paths),
            )
            if result.returncode == 0:
                self._supports_pathspec_file = True
                return result.stdout
            if self._supports_pathspec_file is None and "pathspec-from-file" in (
                result.stderr or ""
            ):
                self._supports_pathspec_file = False
        # Fallback for git builds without ``--pathspec-from-file`` support for
        # ``git diff``.  Paths are separated by ``--`` and the process uses
        # ``--literal-pathspecs``, so names beginning with ``-`` or ``:`` are
        # treated as filenames, never as options/pathspec magic.  Large
        # changes are split into chunks to stay well below ARG_MAX; the
        # resulting per-chunk patches concatenate into one valid patch.
        chunks: list[list[str]] = []
        current: list[str] = []
        current_size = 0
        for path in paths:
            size = len(path) + 1
            if current and current_size + size > _DIFF_ARGV_CHUNK_CHARS:
                chunks.append(current)
                current = []
                current_size = 0
            current.append(path)
            current_size += size
        if current:
            chunks.append(current)
        output: list[str] = []
        for chunk in chunks:
            result = self._git([*base_args, "--", *chunk], cwd=target)
            if result.returncode != 0:
                return ""
            output.append(result.stdout)
        return "".join(output)

    def modified_files(self, path: str | Path) -> list[str]:
        target = Path(path)
        self._validate_worktree_for_path(target)
        if not self.is_git_repo(target):
            return []
        status_paths = self._expand_directory_entries(target, self._status_paths(target))
        visible = self._visible_paths(target, None, status_paths)
        return sorted(visible)

    def collect_diff(
        self,
        path: str | Path,
        *,
        stage_untracked: bool = True,
        base_ref: str | None = None,
    ) -> str:
        target = Path(path)
        self._validate_worktree_for_path(target)
        if not self.is_git_repo(target):
            return ""
        ref = base_ref or self.baseline_for_path(target) or "HEAD"
        # ``git add -N`` records intent-to-add so that brand-new files appear in
        # the diff with their full content.  It does not stage content,
        # therefore it is safe inside an isolated worktree.
        excludes = [*sensitive_git_pathspecs(), *_meta_git_excludes()]
        if stage_untracked:
            self._git(["add", "-N", "--", ".", *excludes], cwd=target)
        raw, ok = self._raw_changed_paths(target, ref)
        if ok:
            return self._diff_visible_paths(target, ref, self._visible_paths(target, ref, raw))
        # A repository without a usable baseline commit: diff the index/worktree
        # instead, still restricted to the visible paths.
        raw, _ = self._raw_changed_paths(target, None)
        return self._diff_visible_paths(target, None, self._visible_paths(target, None, raw))

    def diff_files(self, path: str | Path, *, base_ref: str | None = None) -> list[str]:
        target = Path(path)
        self._validate_worktree_for_path(target)
        if not self.is_git_repo(target):
            return []
        ref = base_ref or self.baseline_for_path(target) or "HEAD"
        raw, ok = self._raw_changed_paths(target, ref)
        if not ok:
            return []
        return sorted(self._visible_paths(target, ref, raw))

    def collect_changes(
        self,
        path: str | Path,
        *,
        stage_untracked: bool = True,
        base_ref: str | None = None,
    ) -> Changes:
        target = Path(path)
        self._validate_worktree_for_path(target)
        ref = base_ref or self.baseline_for_path(target)
        diff = self.collect_diff(target, stage_untracked=stage_untracked, base_ref=ref)
        status_paths = self._expand_directory_entries(target, self._status_paths(target))
        raw_files = {
            path
            for path in [*status_paths, *self._raw_changed_paths(target, ref)[0]]
            if path and not self._is_state_path(path) and not _is_meta_file(path)
        }
        visible, skipped = filter_sensitive_paths(sorted(raw_files))
        expanded: list[str] = []
        for path in skipped:
            candidate = target / path
            try:
                is_directory = candidate.is_dir() and not candidate.is_symlink()
            except OSError:
                is_directory = False
            if is_directory:
                for child in sorted(candidate.rglob("*")):
                    if child.is_file() or child.is_symlink():
                        expanded.append(child.relative_to(target).as_posix())
            else:
                expanded.append(path)
        return Changes(
            worktree_path=str(target),
            modified_files=sorted(visible),
            diff=diff,
            base_ref=ref or "HEAD",
            skipped_sensitive=sorted(set(expanded) | set(skipped)),
        )

    def capture_checkpoint(
        self,
        task: TaskRequest,
        workdir: str | Path,
        harness: str,
        model: str,
        reason: str,
        stdout_tail: str = "",
        *,
        assignment_id: str | None = None,
        max_diff_chars: int = 20_000,
        stage_untracked: bool = True,
    ) -> ContinuationCheckpoint:
        target = Path(workdir)
        changes = self.collect_changes(target, stage_untracked=stage_untracked)
        diff, _ = cap_text(redact_text(changes.diff), max_diff_chars)
        tail, _ = cap_text(redact_text((stdout_tail or "")[-8000:]), 4000)
        return ContinuationCheckpoint(
            interrupted_harness=harness,
            interrupted_model=model,
            interruption_reason=reason,
            partial_modified_files=list(changes.modified_files),
            partial_git_diff=diff,
            partial_stdout_tail=tail,
            captured_at=utc_now(),
            worktree_path=str(target),
            last_assignment_id=assignment_id,
        )

    # ------------------------------------------------------------------
    # apply changes back to the primary workspace
    # ------------------------------------------------------------------
    def apply_worktree_changes(
        self,
        task_id: str,
        *,
        workspace_root: str | Path | None = None,
        strategy: str = "patch",
        fallback_to_copy: bool = False,
    ) -> ApplyResult:
        task_id = self.validate_task_id(task_id)
        source = self.worktree_path(task_id)
        target = Path(workspace_root).expanduser().resolve() if workspace_root else self.workspace_root
        if not source.exists():
            raise WorkspaceError(f"worktree does not exist for task {task_id}: {source}")
        if source.resolve() == target.resolve():
            return ApplyResult(task_id=task_id, strategy=strategy, detail="already in the primary workspace")
        self._validated_worktree_gitdir(task_id, path=source)
        self._verify_git_guard(task_id)
        changes = self.collect_changes(source)
        self._check_diff_symlinks(changes.diff, source, target)
        if strategy == "copy":
            applied = self._copy_changes(source, target, changes.modified_files)
            return ApplyResult(
                task_id=task_id,
                strategy=strategy,
                applied_files=applied,
                detail="copied files",
                warnings=list(changes.skipped_sensitive),
            )
        if strategy not in {"patch", "merge"}:
            raise ValueError(f"unsupported apply strategy: {strategy}")
        if not changes.diff.strip():
            return ApplyResult(task_id=task_id, strategy=strategy, detail="no changes to apply")

        files = list(changes.modified_files)
        if strategy == "merge":
            # ``git apply --3way`` writes conflict markers into the working tree
            # before it reports the conflict, so a dirty target cannot be
            # restored reliably.  Require the affected files to be clean first.
            self._ensure_clean_merge_target(target, files)

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        patch_args = ["apply", "--whitespace=nowarn", "--recount"]
        if strategy == "merge":
            patch_args.append("--3way")
        preflight = self._git([*patch_args, "--check", "-"], cwd=target, input_text=changes.diff)
        if preflight.returncode != 0:
            conflicts = self._conflict_files(preflight.stderr + preflight.stdout, files)
            raise WorkspaceError(
                f"patch preflight failed for task {task_id}; conflicting files: "
                f"{', '.join(conflicts) if conflicts else 'unknown'}; primary workspace left untouched"
            )
        pre_existing = {path for path in files if (target / path).exists() or (target / path).is_symlink()}
        pre_existing_links = {path for path in files if (target / path).is_symlink()}
        result = self._git([*patch_args, "-"], cwd=target, input_text=changes.diff)
        if result.returncode == 0:
            self._verify_applied_symlinks(target, files, pre_existing_links)
            return ApplyResult(
                task_id=task_id,
                strategy=strategy,
                applied_files=files,
                detail="patch applied",
                warnings=list(changes.skipped_sensitive),
            )
        conflict_detail = result.stderr.strip() or result.stdout.strip()
        # The real apply can still fail after a successful preflight (that is
        # exactly what a conflicting ``git apply --3way`` does).  Restore the
        # affected files before surfacing the error instead of claiming the
        # primary workspace was untouched.
        self._rollback_failed_apply(target, files, pre_existing)
        if strategy == "merge":
            raise WorkspaceError(
                f"merge conflict for task {task_id}; primary workspace rolled back: {conflict_detail[:1000]}"
            )
        if not fallback_to_copy:
            raise WorkspaceError(
                f"failed to apply patch for task {task_id}; primary workspace rolled back: {conflict_detail[:1000]}"
            )
        applied = self._copy_changes(source, target, changes.modified_files)
        return ApplyResult(
            task_id=task_id,
            strategy=strategy,
            applied_files=applied,
            used_fallback=True,
            detail=f"patch failed, rolled back and copied files instead: {result.stderr.strip()[:500]}",
        )

    @staticmethod
    def _conflict_files(output: str, changed_files: Iterable[str]) -> list[str]:
        conflicts: list[str] = []
        for match in re.findall(r"patch failed: ([^:\n]+):", output):
            name = match.strip()
            if name and name not in conflicts:
                conflicts.append(name)
        if not conflicts:
            for name in changed_files:
                if name and name in output and name not in conflicts:
                    conflicts.append(name)
        return conflicts

    def _ensure_clean_merge_target(self, target: Path, files: Iterable[str]) -> None:
        literal = [str(item) for item in files if str(item)]
        if not literal:
            return
        result = self._git(
            ["--literal-pathspecs", "status", "--porcelain", "-z", "--", *literal],
            cwd=target,
        )
        if result.returncode != 0:
            return
        entries = [entry for entry in result.stdout.split("\0") if entry.strip()]
        names: list[str] = []
        for entry in entries:
            name = entry[3:] if len(entry) > 3 else entry.strip()
            if name and name not in names:
                names.append(name)
        if names:
            raise WorkspaceError(
                "merge strategy requires a clean target workspace for the affected files; "
                "uncommitted changes in: " + ", ".join(names[:10])
            )

    def _tracked_paths(self, target: Path, files: Iterable[str]) -> set[str]:
        literal = [str(item) for item in files if str(item)]
        if not literal:
            return set()
        result = self._git(
            ["--literal-pathspecs", "ls-files", "-z", "--", *literal],
            cwd=target,
        )
        if result.returncode != 0:
            return set()
        return {entry for entry in result.stdout.split("\0") if entry}

    def _rollback_failed_apply(
        self,
        target: Path,
        files: Iterable[str],
        pre_existing: set[str],
    ) -> None:
        literal = sorted({str(item) for item in files if str(item)})
        if not literal:
            return
        tracked = self._tracked_paths(target, literal)
        self._git(["--literal-pathspecs", "reset", "-q", "--", *literal], cwd=target)
        tracked_present = [path for path in literal if path in tracked]
        if tracked_present:
            self._git(
                ["--literal-pathspecs", "checkout", "--", *tracked_present],
                cwd=target,
            )
        for relative in literal:
            if relative in pre_existing:
                continue
            created = target / relative
            try:
                if created.is_symlink() or created.exists():
                    created.unlink()
            except OSError:
                pass

    def _verify_applied_symlinks(
        self,
        target: Path,
        files: Iterable[str],
        pre_existing_links: Iterable[str],
    ) -> None:
        candidates = {str(item) for item in files if str(item)}
        previous = {str(item) for item in pre_existing_links if str(item)}
        if not candidates:
            return
        result = self._git(
            ["--literal-pathspecs", "diff", "--raw", "-z", "--no-ext-diff"], cwd=target
        )
        if result.returncode != 0:
            return
        root = target.resolve()
        for record in _parse_raw_z(result.stdout):
            path = record.get("path") or ""
            if path not in candidates or path in previous:
                continue
            if record.get("new_mode") != "120000":
                continue
            if str(record.get("status") or "").startswith("D"):
                continue
            candidate = target / path
            if not candidate.is_symlink():
                continue
            try:
                raw_target = os.readlink(candidate)
            except OSError:
                continue
            target_path = Path(raw_target)
            if not target_path.is_absolute():
                target_path = candidate.parent / target_path
            try:
                resolved = target_path.resolve(strict=False)
            except OSError:
                resolved = target_path
            if not self._is_relative_to(resolved, root):
                try:
                    candidate.unlink()
                except OSError:
                    pass
                raise WorkspaceError(
                    f"applied symlink {path} points outside the target workspace: {raw_target}"
                )

        # ``git diff --raw`` does not show untracked files (``git apply``
        # writes without touching the index), so newly created links are also
        # checked directly.
        for path in sorted(candidates - previous):
            candidate = target / path
            try:
                if not candidate.is_symlink():
                    continue
                raw_target = os.readlink(candidate)
            except OSError:
                continue
            target_path = Path(raw_target)
            if not target_path.is_absolute():
                target_path = candidate.parent / target_path
            try:
                resolved = target_path.resolve(strict=False)
            except OSError:
                resolved = target_path
            if not self._is_relative_to(resolved, root):
                try:
                    candidate.unlink()
                except OSError:
                    pass
                raise WorkspaceError(
                    f"applied symlink {path} points outside the target workspace: {raw_target}"
                )

    def _check_diff_symlinks(self, diff: str, source: Path, target_root: Path) -> None:
        """Reject any diff block that creates a symlink escaping ``target_root``.

        Paths are decoded from the diff headers (C-style quoting included) via
        the shared block parser; parsing the raw text for ``+++ b/`` was
        bypassable with a non-ASCII filename.
        """

        root = Path(target_root).resolve()
        for block in _parse_diff_blocks(diff):
            if str(block.get("new_mode") or "") != "120000":
                continue
            path = block.get("path")
            if not path:
                raise WorkspaceError("symlink patch block has no target path")
            for raw_value in block.get("plus_lines") or []:
                raw_target = str(raw_value).strip()
                if not raw_target:
                    raise WorkspaceError(f"symlink {path} has an empty target")
                candidate = Path(raw_target)
                target_path = candidate if candidate.is_absolute() else (root / path).parent / candidate
                try:
                    resolved = target_path.resolve(strict=False)
                except OSError:
                    resolved = target_path
                if not self._is_relative_to(resolved, root):
                    raise WorkspaceError(
                        f"symlink {path} points outside the target workspace: {raw_target}"
                    )

    def _copy_changes(self, source: Path, target: Path, files: Iterable[str]) -> list[str]:
        applied: list[str] = []
        target_resolved = target.resolve()
        for relative in files:
            rel_path = Path(relative)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                raise WorkspaceError(f"refusing unsafe worktree path: {relative}")
            if is_sensitive_path(rel_path) or self._is_state_path(relative):
                continue
            src_path = source / rel_path
            dst_path = target / rel_path
            if src_path.is_symlink():
                if self._recreate_symlink(src_path, dst_path, source, target):
                    applied.append(relative)
                continue
            src = src_path.resolve()
            dst = dst_path.resolve()
            if not self._is_relative_to(src, source.resolve()) or not self._is_relative_to(dst, target_resolved):
                raise WorkspaceError(f"refusing path escaping workspace: {relative}")
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                applied.append(relative)
            elif src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                applied.append(relative)
            elif dst.exists():
                # File was deleted in the worktree.
                dst.unlink()
                applied.append(relative)
        return applied

    def _safe_remove_dir(self, path: Path, root: Path) -> None:
        target = Path(path)
        try:
            root_resolved = Path(root).resolve()
            resolved = target.resolve()
        except OSError as exc:
            raise WorkspaceError(f"cannot resolve path for removal: {exc}") from exc
        if resolved == root_resolved:
            raise WorkspaceError(f"refusing to remove the worktrees root itself: {resolved}")
        try:
            relative = resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise WorkspaceError(
                f"refusing to remove directory outside worktrees root: {resolved}"
            ) from exc
        if not relative.parts or ".." in relative.parts:
            raise WorkspaceError(f"refusing to remove directory outside worktrees root: {resolved}")
        if target.is_symlink():
            # Only unlink the link; rmtree() would follow it and delete its target.
            target.unlink()
            return
        shutil.rmtree(resolved)

    def write_temp_patch(self, content: str) -> Path:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False, dir=str(self.tmp_dir), encoding="utf-8"
        )
        with handle:
            handle.write(content)
        return Path(handle.name)


__all__ = [
    "ApplyResult",
    "Changes",
    "WorkspaceError",
    "WorkspaceManager",
    "WorktreeInfo",
]
