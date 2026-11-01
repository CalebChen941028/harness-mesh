"""One-command installation of Harness-Mesh into supported desktop harnesses.

The installer is deliberately explicit: it edits only the MCP/plugin surfaces
listed here, writes backups before first modification, and is idempotent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

from . import __version__
from .config import MeshConfig, find_config_path, resolve_harness_binary

Runner = Callable[[list[str], Path, dict[str, str]], tuple[int, str, str]]

MCP_SERVER_NAME = "harness-mesh"
DSH_ENTRY_ID = "mcp-harness-mesh"
DSH_SERVER_NAME = "harness_mesh"
ZCODE_MARKET_NAME = "harness-mesh-local"
ZCODE_PLUGIN_NAME = "harness-mesh"
DSH_BLOCK_START = "# >>> harness-mesh MCP integration (managed by hmesh install)"
DSH_BLOCK_END = "# <<< harness-mesh MCP integration"


@dataclass
class InstallResult:
    harness: str
    action: str
    detail: str = ""
    commands: list[list[str]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    @property
    def installed(self) -> bool:
        return self.action in {"installed", "updated", "ready"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "action": self.action,
            "detail": self.detail,
            "commands": self.commands,
            "files": self.files,
        }


class HarnessInstaller:
    def __init__(
        self,
        config: MeshConfig,
        workspace_root: str | Path,
        *,
        home_dir: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] | None = None,
        runner: Runner | None = None,
        config_path: str | Path | None = None,
        dry_run: bool = False,
        allow_system_paths: bool | None = None,
    ):
        self.config = config
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.env = dict(env if env is not None else os.environ)
        self.home_dir = Path(home_dir or self.env.get("HOME") or Path.home()).expanduser().resolve()
        self.which = which or shutil.which
        self.runner = runner or self._default_runner
        self.dry_run = bool(dry_run)
        self.state_dir = config.state_path(self.workspace_root)
        resolved_config = Path(config_path).expanduser() if config_path else find_config_path(self.workspace_root)
        self.config_path = resolved_config if resolved_config and resolved_config.exists() else None
        self.python = sys.executable
        self.mcp_args = ["-m", "harness_mesh.mcp_server"]
        if allow_system_paths is None:
            allow_system_paths = self.which is shutil.which
        self.allow_system_paths = allow_system_paths

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def install(
        self,
        harnesses: Iterable[str] | None = None,
        *,
        uninstall: bool = False,
    ) -> list[InstallResult]:
        requested = list(harnesses or [])
        if not requested:
            requested = [name for name, cfg in self.config.harnesses.items() if cfg.enabled]
        results: list[InstallResult] = []
        for harness in requested:
            if harness not in self.config.harnesses:
                results.append(InstallResult(harness, "failed", "unknown harness"))
                continue
            if not self.config.harnesses[harness].enabled and not uninstall:
                results.append(InstallResult(harness, "skipped", "harness disabled"))
                continue
            if uninstall:
                result = self._uninstall_harness(harness)
            else:
                result = self._install_harness(harness)
            if self.dry_run:
                mapping = {
                    "installed": "would_install",
                    "uninstalled": "would_uninstall",
                }
                result.action = mapping.get(result.action, result.action)
                result.detail = f"[dry-run] {result.detail}".strip()
            results.append(result)
        return results

    def _install_harness(self, harness: str) -> InstallResult:
        if harness == "zcode":
            return self._install_zcode()
        if harness == "deepseek":
            return self._install_dsh()
        binary = self._resolve_binary(harness)
        if binary is None:
            return InstallResult(harness, "skipped", "binary not found")
        if harness in {"claude_code", "codex", "antigravity"}:
            return self._install_mcp_cli(harness, binary)
        return InstallResult(harness, "skipped", "no installer available")

    def _uninstall_harness(self, harness: str) -> InstallResult:
        if harness == "zcode":
            return self._uninstall_zcode()
        if harness == "deepseek":
            return self._uninstall_dsh()
        binary = self._resolve_binary(harness)
        if binary is None:
            return InstallResult(harness, "skipped", "binary not found")
        if harness in {"claude_code", "codex", "antigravity"}:
            return self._uninstall_mcp_cli(harness, binary)
        return InstallResult(harness, "skipped", "no installer available")

    # ------------------------------------------------------------------
    # generic helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _default_runner(argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str, str]:
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            return completed.returncode, completed.stdout, completed.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)

    def _run(self, argv: list[str], commands: list[list[str]]) -> tuple[int, str, str]:
        commands.append(list(argv))
        if self.dry_run:
            return 0, "[dry-run]", ""
        return self.runner(argv, self.workspace_root, self._command_env())

    def _command_env(self) -> dict[str, str]:
        env = dict(self.env)
        env.setdefault("HARNESS_MESH_WORKSPACE", str(self.workspace_root))
        if self.config_path is not None:
            env.setdefault("HARNESS_MESH_CONFIG", str(self.config_path))
        return env

    def _mcp_env(self) -> dict[str, str]:
        env = {"HARNESS_MESH_WORKSPACE": str(self.workspace_root)}
        if self.config_path is not None:
            env["HARNESS_MESH_CONFIG"] = str(self.config_path)
        return env

    def _resolve_binary(self, harness: str) -> str | None:
        cfg = self.config.harnesses[harness]
        return resolve_harness_binary(
            cfg,
            self.which,
            self.state_dir,
            self.home_dir,
            allow_system_paths=self.allow_system_paths,
        )

    def _env_flags(self, style: str) -> list[str]:
        flags: list[str] = []
        for key, value in self._mcp_env().items():
            if style == "claude":
                flags.extend(["-e", f"{key}={value}"])
            else:
                flags.extend(["--env", f"{key}={value}"])
        return flags

    # ------------------------------------------------------------------
    # Claude / Codex / Antigravity
    # ------------------------------------------------------------------
    def _install_mcp_cli(self, harness: str, binary: str) -> InstallResult:
        commands: list[list[str]] = []
        remove = [binary, "mcp", "remove", MCP_SERVER_NAME]
        self._run(remove, commands)
        if harness == "claude_code":
            # Claude's ``--env`` option is variadic and swallows the server
            # name; add-json is unambiguous and health-checked.
            payload = json.dumps(
                {
                    "type": "stdio",
                    "command": self.python,
                    "args": list(self.mcp_args),
                    "env": self._mcp_env(),
                }
            )
            add = [binary, "mcp", "add-json", "-s", "user", MCP_SERVER_NAME, payload]
        elif harness == "antigravity":
            add = [
                binary,
                "mcp",
                "add",
                *self._env_flags("generic"),
                MCP_SERVER_NAME,
                self.python,
                *self.mcp_args,
            ]
        else:
            add = [
                binary,
                "mcp",
                "add",
                *self._env_flags("generic"),
                MCP_SERVER_NAME,
                "--",
                self.python,
                *self.mcp_args,
            ]
        code, stdout, stderr = self._run(add, commands)
        if code == 0:
            return InstallResult(
                harness,
                "installed",
                "MCP server registered",
                commands=commands,
            )
        return InstallResult(
            harness,
            "failed",
            (stderr or stdout).strip()[:1000] or f"exit {code}",
            commands=commands,
        )

    def _uninstall_mcp_cli(self, harness: str, binary: str) -> InstallResult:
        commands: list[list[str]] = []
        code, stdout, stderr = self._run([binary, "mcp", "remove", MCP_SERVER_NAME], commands)
        if code == 0 or "not found" in (stderr + stdout).lower():
            return InstallResult(harness, "uninstalled", "MCP server removed", commands=commands)
        return InstallResult(
            harness,
            "failed",
            (stderr or stdout).strip()[:1000] or f"exit {code}",
            commands=commands,
        )

    # ------------------------------------------------------------------
    # ZCode plugin marketplace
    # ------------------------------------------------------------------
    def _zcode_manifest(self) -> dict[str, Any]:
        return {
            "name": ZCODE_PLUGIN_NAME,
            "version": __version__,
            "description": (
                "Cross-harness scheduling: model discovery, quota failover, worktree isolation "
                "and shared project context."
            ),
            "description_i18n": {
                "zh-CN": "跨 Harness 调度：模型发现、额度换棒、Worktree 隔离与共享项目上下文。"
            },
            "author": {"name": "Harness-Mesh Contributors"},
            "license": "MIT",
            "skills": "./skills",
            "mcpServers": {
                MCP_SERVER_NAME: {
                    "command": self.python,
                    "args": list(self.mcp_args),
                    "env": self._mcp_env(),
                    "startup_timeout_sec": 60,
                }
            },
        }

    def _zcode_marketplace(self) -> dict[str, Any]:
        return {
            "name": ZCODE_MARKET_NAME,
            "plugins": [
                {
                    "name": ZCODE_PLUGIN_NAME,
                    "source": f"./plugins/{ZCODE_PLUGIN_NAME}",
                    "version": __version__,
                    "description": "Cross-harness scheduling tools over MCP.",
                    "displayName": "Harness Mesh",
                    "displayName_i18n": {"zh-CN": "跨 Harness 调度"},
                    "description_i18n": {"zh-CN": "通过 MCP 调度多个编码 Harness。"},
                    "category": "developer-tools",
                }
            ],
        }

    def _zcode_files(self) -> list[Path]:
        market = self.state_dir / "zcode-plugin"
        plugin = market / "plugins" / ZCODE_PLUGIN_NAME
        skill = plugin / "skills" / ZCODE_PLUGIN_NAME / "SKILL.md"
        manifest = plugin / ".zcode-plugin" / "plugin.json"
        marketplace = market / "marketplace.json"
        if self.dry_run:
            return [marketplace, manifest, skill]
        (manifest.parent).mkdir(parents=True, exist_ok=True)
        skill.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(self._zcode_manifest(), indent=2, ensure_ascii=False), encoding="utf-8")
        marketplace.write_text(
            json.dumps(self._zcode_marketplace(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        skill.write_text(
            "---\n"
            "name: harness-mesh\n"
            "description: Use Harness-Mesh MCP tools to inspect local harness models, dispatch tickets, "
            "read paged diffs/transcripts and share project context.\n"
            "---\n\n"
            "# Harness-Mesh\n\n"
            "Start with `list_harnesses`, then use `dispatch_task` and `get_task_status`. "
            "Use `read_task_resource` for large diffs/transcripts and `apply_task_patch` only after review. "
            "Record durable architecture facts with `update_project_context`.\n",
            encoding="utf-8",
        )
        return [marketplace, manifest, skill]

    def _install_zcode(self) -> InstallResult:
        commands: list[list[str]] = []
        files = self._zcode_files()
        binary = self._resolve_binary("zcode")
        if binary is None:
            return InstallResult(
                "zcode",
                "ready",
                "local plugin/marketplace generated; add the marketplace in ZCode UI",
                commands=commands,
                files=[str(path) for path in files],
            )
        market = files[0].parent
        self._run([binary, "plugins", "marketplace", "add", str(market), "--scope", "project"], commands)
        self._run([binary, "plugins", "uninstall", ZCODE_PLUGIN_NAME, "-s", "project", "--force"], commands)
        code, stdout, stderr = self._run(
            [binary, "plugins", "install", f"{ZCODE_PLUGIN_NAME}@{ZCODE_MARKET_NAME}", "-s", "project"],
            commands,
        )
        if code == 0:
            return InstallResult(
                "zcode",
                "installed",
                "ZCode plugin installed",
                commands=commands,
                files=[str(path) for path in files],
            )
        return InstallResult(
            "zcode",
            "failed",
            (stderr or stdout).strip()[:1000] or f"exit {code}",
            commands=commands,
            files=[str(path) for path in files],
        )

    def _uninstall_zcode(self) -> InstallResult:
        commands: list[list[str]] = []
        binary = self._resolve_binary("zcode")
        if binary is not None:
            self._run([binary, "plugins", "uninstall", ZCODE_PLUGIN_NAME, "-s", "project", "--force"], commands)
            self._run([binary, "plugins", "marketplace", "remove", ZCODE_MARKET_NAME], commands)
        market = self.state_dir / "zcode-plugin"
        if not self.dry_run and market.exists():
            shutil.rmtree(market, ignore_errors=True)
        return InstallResult("zcode", "uninstalled", "ZCode plugin removed", commands=commands)

    # ------------------------------------------------------------------
    # DeepSeek Harness Cordis patch
    # ------------------------------------------------------------------
    def _dsh_entry(self) -> dict[str, Any]:
        return {
            "insert": [
                {
                    "id": DSH_ENTRY_ID,
                    "name": "@deepseek-ai/dsh-mcp-client",
                    "config": {
                        "serverName": DSH_SERVER_NAME,
                        "transport": "stdio",
                        "command": self.python,
                        "args": list(self.mcp_args),
                        "env": self._mcp_env(),
                    },
                }
            ]
        }

    def _dsh_patch_files(self) -> list[Path]:
        profiles = self.home_dir / ".dsh" / "profiles"
        files: list[Path] = []
        for name in ("desktop", "headless"):
            candidate = profiles / name / "cordis.patch.yml"
            if candidate.exists() or name == "desktop":
                files.append(candidate)
        return files

    def _install_dsh(self) -> InstallResult:
        if self._resolve_binary("deepseek") is None:
            return InstallResult("deepseek", "skipped", "binary not found")
        files: list[Path] = []
        entry = self._dsh_entry()
        for path in self._dsh_patch_files():
            if self.dry_run:
                files.append(path)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            backup = path.with_suffix(path.suffix + ".harness-mesh.bak")
            if path.exists() and not backup.exists():
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            _upsert_dsh_block(path, entry)
            files.append(path)
        if not files:
            return InstallResult("deepseek", "skipped", "no DSH profile directory found")
        return InstallResult(
            "deepseek",
            "installed",
            "dsh-mcp-client entry installed; restart DeepSeek Harness",
            files=[str(path) for path in files],
        )

    def _uninstall_dsh(self) -> InstallResult:
        files: list[Path] = []
        for path in self._dsh_patch_files():
            if not path.exists():
                continue
            if not self.dry_run:
                _remove_dsh_block(path)
            files.append(path)
        return InstallResult("deepseek", "uninstalled", "DSH MCP entry removed", files=[str(path) for path in files])


# ----------------------------------------------------------------------
# DSH patch editing without destroying unrelated entries/comments
# ----------------------------------------------------------------------
def _dsh_block(entry: Mapping[str, Any]) -> str:
    return (
        DSH_BLOCK_START
        + "\n"
        + yaml.safe_dump([dict(entry)], sort_keys=False, allow_unicode=True).strip()
        + "\n"
        + DSH_BLOCK_END
        + "\n"
    )


def _remove_dsh_block_text(text: str) -> str:
    managed = re.compile(
        rf"(?ms)^{re.escape(DSH_BLOCK_START)}.*?^{re.escape(DSH_BLOCK_END)}\n?"
    )
    text = managed.sub("", text)
    # Legacy (unwrapped) entries written by earlier 0.3.0 builds.
    legacy = re.compile(rf"(?ms)^- id: {re.escape(DSH_ENTRY_ID)}\n(?:^[ \t]+[^\n]*\n?)*")
    text = legacy.sub("", text)
    # An empty flow marker cannot coexist with a block sequence.
    text = re.sub(r"(?m)^[ \t]*\[\][ \t]*$", "", text).rstrip()
    meaningful = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not meaningful:
        text = (text + "\n" if text else "") + "[]"
    return text.rstrip() + "\n"


def _upsert_dsh_block(path: Path, entry: Mapping[str, Any]) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = _remove_dsh_block_text(text).rstrip()
    # Drop the empty flow list before appending a block sequence.
    text = re.sub(r"(?m)^[ \t]*\[\][ \t]*$", "", text).rstrip()
    block = _dsh_block(entry)
    path.write_text((text + "\n\n" if text else "") + block, encoding="utf-8")


def _remove_dsh_block(path: Path) -> None:
    if not path.exists():
        return
    text = _remove_dsh_block_text(path.read_text(encoding="utf-8"))
    path.write_text(text, encoding="utf-8")


__all__ = ["HarnessInstaller", "InstallResult", "MCP_SERVER_NAME"]
