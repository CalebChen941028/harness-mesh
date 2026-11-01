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
from .security import SENSITIVE_FILE_PATTERNS, is_sensitive_name, is_sensitive_path

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


@dataclass
class Changes:
    worktree_path: str
    modified_files: list[str] = field(default_factory=list)
    diff: str = ""
    base_ref: str = "HEAD"


@dataclass
class ApplyResult:
    task_id: str
    strategy: str
    applied_files: list[str] = field(default_factory=list)
    used_fallback: bool = False
    detail: str = ""
    branch: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "strategy": self.strategy,
            "applied_files": self.applied_files,
            "used_fallback": self.used_fallback,
            "detail": self.detail,
            "branch": self.branch,
        }


_SAFE_TASK_ID = re.compile(r"[^A-Za-z0-9._-]+")


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
        command = [self.git_binary, "-c", "core.hooksPath=/dev/null", *args]
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

    def _git_metadata_snapshot(self) -> dict:
        common = self._git_common_dir()
        if common is None:
            return {}
        config = common / "config"
        config_hash = self._hash_file(config) if config.exists() else None
        hooks = common / "hooks"
        hook_hashes: dict[str, str] = {}
        if hooks.is_dir():
            for entry in sorted(hooks.rglob("*")):
                if entry.is_file() and not entry.is_symlink():
                    digest = self._hash_file(entry)
                    if digest is not None:
                        hook_hashes[entry.relative_to(hooks).as_posix()] = digest
        return {"config": config_hash, "hooks": hook_hashes}

    def _git_guard_path(self, task_id: str) -> Path:
        safe = _SAFE_TASK_ID.sub("_", task_id)
        return self.git_guard_dir / f"{safe}.json"

    def _record_git_guard(self, task_id: str) -> None:
        try:
            snapshot = self._git_metadata_snapshot()
            self.git_guard_dir.mkdir(parents=True, exist_ok=True)
            payload = {"task_id": task_id, "snapshot": snapshot}
            self._git_guard_path(task_id).write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("unable to record Git metadata guard for %s: %s", task_id, exc)

    def _verify_git_guard(self, task_id: str) -> None:
        guard_path = self._git_guard_path(task_id)
        if not guard_path.exists():
            logger.warning("no Git metadata guard recorded for %s; applying without comparison", task_id)
            return
        try:
            saved = json.loads(guard_path.read_text(encoding="utf-8")).get("snapshot", {})
        except (OSError, json.JSONDecodeError):
            raise WorkspaceError(f"Git metadata guard for {task_id} is unreadable")
        current = self._git_metadata_snapshot()
        if saved == current:
            return
        changes: list[str] = []
        if saved.get("config") != current.get("config"):
            changes.append(".git/config changed")
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
            self._git(["add", "-A", "--", "."], check=True)
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
        entries.extend(SENSITIVE_FILE_PATTERNS)
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
        cleaned = _SAFE_TASK_ID.sub("_", task_id.strip())
        return cleaned or "task"

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
        self.ensure_repository()
        self._record_git_guard(task_id)
        path = self.worktree_path(task_id)
        branch = self.branch_name(task_id)
        if path.exists():
            if (path / ".git").exists():
                marker = self._read_baseline_marker(task_id) or {}
                baseline = marker.get("baseline_commit")
                if not baseline:
                    head = self._git(["rev-parse", "HEAD"], cwd=path)
                    baseline = head.stdout.strip() if head.returncode == 0 else None
                return WorktreeInfo(
                    task_id=task_id,
                    path=path,
                    branch=branch,
                    reused=True,
                    baseline_commit=str(baseline) if baseline else None,
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
        if sync_uncommitted:
            self._sync_uncommitted_changes(path)
            self._commit_worktree_baseline(task_id, path, "harness-mesh: baseline sync")
        head = self._git(["rev-parse", "HEAD"], cwd=path)
        info.baseline_commit = head.stdout.strip() if head.returncode == 0 else None
        self._write_baseline_marker(info)
        return info

    def _recreate_symlink(self, source: Path, destination: Path, source_root: Path) -> bool:
        """Recreate an internal symlink, never copying its target's content."""

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
        if not self._is_relative_to(resolved, source_root.resolve()):
            logger.warning("skipping symlink %s: target escapes source workspace", source)
            return False
        link_target = os.readlink(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or destination.exists():
            if destination.is_dir() and not destination.is_symlink():
                raise WorkspaceError(f"cannot replace directory with symlink: {destination}")
            destination.unlink()
        os.symlink(link_target, destination)
        return True

    def _sync_uncommitted_changes(self, worktree_path: Path) -> None:
        """Copy the primary checkout's uncommitted state into a new worktree.

        Tracked modifications/deletions are applied from a binary-safe diff;
        untracked non-ignored files are copied.  The subsequent baseline commit
        makes this inherited state invisible to ``capture_changes``.
        """

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
            for relative in self._parse_porcelain_paths(status.stdout):
                if self._is_state_path(relative) or is_sensitive_name(relative):
                    continue
                src = self.workspace_root / relative
                dst = worktree_path / relative
                if src.is_symlink():
                    self._recreate_symlink(src, dst, self.workspace_root)
                elif src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                elif not src.exists() and dst.exists():
                    dst.unlink()

    def _copy_dirty_tracked_files(self, worktree_path: Path) -> None:
        status = self._git(["status", "--porcelain", "-z"], cwd=self.workspace_root)
        if status.returncode != 0:
            return
        for relative in self._parse_porcelain_paths(status.stdout):
            if self._is_state_path(relative) or is_sensitive_name(relative):
                continue
            src = self.workspace_root / relative
            dst = worktree_path / relative
            if src.is_symlink():
                self._recreate_symlink(src, dst, self.workspace_root)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            elif not src.exists() and dst.exists():
                dst.unlink()

    def _parse_porcelain_paths(self, payload: str) -> list[str]:
        entries = payload.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry or len(entry) < 4:
                continue
            status = entry[:2]
            relative = entry[3:]
            if status[0] in ("R", "C") and index < len(entries):
                # The original path follows in the next NUL-separated entry.
                index += 1
            paths.append(relative)
        return paths

    def _is_state_path(self, relative: str) -> bool:
        try:
            state_rel = self.state_dir.resolve().relative_to(self.workspace_root)
        except ValueError:
            return False
        return relative == state_rel.as_posix() or relative.startswith(state_rel.as_posix() + "/")

    def _commit_worktree_baseline(self, task_id: str, worktree_path: Path, message: str) -> str | None:
        self._git(["add", "-A", "--", "."], cwd=worktree_path)
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

        if not diff.strip():
            return False
        path = self.worktree_path(task_id)
        if not path.exists():
            raise WorkspaceError(f"worktree does not exist for task {task_id}")
        applied = self._git(
            ["apply", "--whitespace=nowarn", "--recount", "--binary", "-"],
            cwd=path,
            input_text=diff,
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
    def modified_files(self, path: str | Path) -> list[str]:
        target = Path(path)
        if not self.is_git_repo(target):
            return []
        result = self._git(["status", "--porcelain", "-z"], cwd=target)
        if result.returncode != 0:
            return []
        entries = result.stdout.split("\0")
        files: set[str] = set()
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            if len(entry) < 4:
                continue
            status = entry[:2]
            file_path = entry[3:]
            if status[0] in ("R", "C") and index < len(entries):
                # The original path is stored in the next NUL separated entry.
                index += 1
            files.add(file_path)
        return sorted(files)

    def collect_diff(
        self,
        path: str | Path,
        *,
        stage_untracked: bool = True,
        base_ref: str | None = None,
    ) -> str:
        target = Path(path)
        if not self.is_git_repo(target):
            return ""
        ref = base_ref or self.baseline_for_path(target) or "HEAD"
        # ``git add -N`` records intent-to-add so that brand-new files appear in
        # the diff with their full content.  It does not stage content,
        # therefore it is safe inside an isolated worktree.
        if stage_untracked:
            self._git(["add", "-N", "--", "."], cwd=target)
        result = self._git(["diff", ref, "--no-color", "--", "."], cwd=target)
        if result.returncode != 0:
            # A repository without commits: show the staged/worktree diff instead.
            result = self._git(["diff", "--no-color"], cwd=target)
        return result.stdout

    def diff_files(self, path: str | Path, *, base_ref: str | None = None) -> list[str]:
        target = Path(path)
        if not self.is_git_repo(target):
            return []
        ref = base_ref or self.baseline_for_path(target) or "HEAD"
        result = self._git(["diff", "--name-only", ref, "--", "."], cwd=target)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def collect_changes(
        self,
        path: str | Path,
        *,
        stage_untracked: bool = True,
        base_ref: str | None = None,
    ) -> Changes:
        target = Path(path)
        ref = base_ref or self.baseline_for_path(target)
        diff = self.collect_diff(target, stage_untracked=stage_untracked, base_ref=ref)
        files = set(self.modified_files(target))
        files.update(self.diff_files(target, base_ref=ref))
        return Changes(
            worktree_path=str(target),
            modified_files=sorted(files),
            diff=diff,
            base_ref=ref or "HEAD",
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
        diff, _ = cap_text(changes.diff, max_diff_chars)
        tail, _ = cap_text((stdout_tail or "")[-8000:], 4000)
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
        source = self.worktree_path(task_id)
        target = Path(workspace_root).expanduser().resolve() if workspace_root else self.workspace_root
        if not source.exists():
            raise WorkspaceError(f"worktree does not exist for task {task_id}: {source}")
        if source.resolve() == target.resolve():
            return ApplyResult(task_id=task_id, strategy=strategy, detail="already in the primary workspace")
        self._verify_git_guard(task_id)
        changes = self.collect_changes(source)
        if strategy == "copy":
            applied = self._copy_changes(source, target, changes.modified_files)
            return ApplyResult(task_id=task_id, strategy=strategy, applied_files=applied, detail="copied files")
        if strategy not in {"patch", "merge"}:
            raise ValueError(f"unsupported apply strategy: {strategy}")
        if not changes.diff.strip():
            return ApplyResult(task_id=task_id, strategy=strategy, detail="no changes to apply")

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        patch_args = ["apply", "--whitespace=nowarn", "--recount"]
        if strategy == "merge":
            patch_args.append("--3way")
        preflight = self._git([*patch_args, "--check", "-"], cwd=target, input_text=changes.diff)
        if preflight.returncode != 0:
            conflicts = self._conflict_files(preflight.stderr + preflight.stdout, changes.modified_files)
            raise WorkspaceError(
                f"patch preflight failed for task {task_id}; conflicting files: "
                f"{', '.join(conflicts) if conflicts else 'unknown'}; primary workspace left untouched"
            )
        result = self._git([*patch_args, "-"], cwd=target, input_text=changes.diff)
        if result.returncode == 0:
            return ApplyResult(
                task_id=task_id,
                strategy=strategy,
                applied_files=list(changes.modified_files),
                detail="patch applied",
            )
        conflict_detail = result.stderr.strip() or result.stdout.strip()
        if strategy == "merge":
            # A failed 3-way merge must never be "resolved" by copying whole
            # files over the primary checkout: that silently destroys local
            # changes. Surface the conflict instead.
            raise WorkspaceError(
                f"merge conflict for task {task_id}; primary workspace left untouched: {conflict_detail[:1000]}"
            )
        if not fallback_to_copy:
            raise WorkspaceError(
                f"failed to apply patch for task {task_id}: {conflict_detail[:1000]}"
            )
        applied = self._copy_changes(source, target, changes.modified_files)
        return ApplyResult(
            task_id=task_id,
            strategy=strategy,
            applied_files=applied,
            used_fallback=True,
            detail=f"patch failed, copied files instead: {result.stderr.strip()[:500]}",
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

    def _copy_changes(self, source: Path, target: Path, files: Iterable[str]) -> list[str]:
        applied: list[str] = []
        target_resolved = target.resolve()
        for relative in files:
            rel_path = Path(relative)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                raise WorkspaceError(f"refusing unsafe worktree path: {relative}")
            if is_sensitive_name(rel_path.name):
                continue
            src_path = source / rel_path
            dst_path = target / rel_path
            if src_path.is_symlink():
                if self._recreate_symlink(src_path, dst_path, source):
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
        resolved = path.resolve()
        if not self._is_relative_to(resolved, root.resolve()):
            raise WorkspaceError(f"refusing to remove directory outside worktrees root: {resolved}")
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
