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
import tempfile
import tomllib
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


@dataclass
class DoctorResult:
    harness: str
    status: str
    detail: str = ""
    command: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "status": self.status,
            "detail": self.detail,
            "command": self.command,
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

    def doctor(self, harnesses: Iterable[str] | None = None) -> list[DoctorResult]:
        requested = list(harnesses or [])
        if not requested:
            requested = [name for name, cfg in self.config.harnesses.items() if cfg.enabled]
        results: list[DoctorResult] = []
        for harness in requested:
            registration = self._read_registration(harness)
            if registration is None:
                results.append(
                    DoctorResult(
                        harness,
                        "not_installed",
                        "no MCP/plugin registration found; run `hmesh install --yes`",
                    )
                )
                continue
            command = str(registration.get("command") or "")
            args = [str(item) for item in registration.get("args") or []]
            if not command:
                results.append(DoctorResult(harness, "failed", "registered command is empty"))
                continue
            resolved = shutil.which(command) if not os.path.isabs(command) else command
            if not resolved or not Path(resolved).exists() or not os.access(resolved, os.X_OK):
                results.append(
                    DoctorResult(
                        harness,
                        "failed",
                        f"registered command is missing or not executable: {command}; "
                        "the venv may have been removed. Reinstall with `hmesh install --yes`.",
                        command,
                    )
                )
                continue
            argv = [str(resolved), *args, "--help"]
            code, stdout, stderr = self._run_doctor(argv)
            if code == 0:
                results.append(DoctorResult(harness, "ok", "registered command starts", command))
            else:
                results.append(
                    DoctorResult(
                        harness,
                        "failed",
                        (stderr or stdout).strip()[:500] or f"command exited {code}; "
                        "reinstall with `hmesh install --yes`",
                        command,
                    )
                )
        return results

    def _run_doctor(self, argv: list[str]) -> tuple[int, str, str]:
        if self.dry_run:
            return 0, "[dry-run]", ""
        try:
            return self.runner(argv, self.workspace_root, self._command_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)

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
                stdin=subprocess.DEVNULL,
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
    # Existing registration discovery / restore
    # ------------------------------------------------------------------
    @staticmethod
    def _normalise_registration(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        command = payload.get("command") or payload.get("cmd")
        if not command:
            return None
        args = payload.get("args") or []
        env = payload.get("env") or {}
        return {
            "command": str(command),
            "args": [str(item) for item in args] if isinstance(args, list) else [],
            "env": {str(key): str(value) for key, value in env.items()} if isinstance(env, dict) else {},
        }

    def _read_registration(self, harness: str) -> dict[str, Any] | None:
        try:
            if harness == "claude_code":
                config_path = self.home_dir / ".claude.json"
                if not config_path.exists():
                    return None
                payload = json.loads(config_path.read_text(encoding="utf-8"))
                return self._normalise_registration((payload.get("mcpServers") or {}).get(MCP_SERVER_NAME))
            if harness == "codex":
                config_path = self.home_dir / ".codex" / "config.toml"
                if not config_path.exists():
                    return None
                payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
                return self._normalise_registration((payload.get("mcp_servers") or {}).get(MCP_SERVER_NAME))
            if harness == "antigravity":
                config_path = self.home_dir / ".gemini" / "config" / "mcp_config.json"
                if not config_path.exists():
                    return None
                payload = json.loads(config_path.read_text(encoding="utf-8"))
                return self._normalise_registration((payload.get("mcpServers") or {}).get(MCP_SERVER_NAME))
            if harness == "zcode":
                manifest = (
                    self.state_dir
                    / "zcode-plugin"
                    / "plugins"
                    / ZCODE_PLUGIN_NAME
                    / ".zcode-plugin"
                    / "plugin.json"
                )
                if not manifest.exists():
                    return None
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                return self._normalise_registration((payload.get("mcpServers") or {}).get(MCP_SERVER_NAME))
            if harness == "deepseek":
                for path in self._dsh_patch_files():
                    if not path.exists():
                        continue
                    text = path.read_text(encoding="utf-8")
                    match = re.search(
                        rf"(?ms)^{re.escape(DSH_BLOCK_START)}\n(.*?)^{re.escape(DSH_BLOCK_END)}",
                        text,
                    )
                    if not match:
                        continue
                    parsed = yaml.safe_load(match.group(1))
                    if not isinstance(parsed, list):
                        continue
                    for item in parsed:
                        if isinstance(item, dict):
                            for row in item.get("insert", []) or []:
                                if isinstance(row, dict) and row.get("id") == DSH_ENTRY_ID:
                                    return self._normalise_registration(row.get("config"))
        except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError, yaml.YAMLError):
            return None
        return None

    def _add_command_for(
        self, harness: str, binary: str, registration: Mapping[str, Any]
    ) -> list[str]:
        command = str(registration.get("command"))
        args = [str(item) for item in registration.get("args") or []]
        env = {str(key): str(value) for key, value in (registration.get("env") or {}).items()}
        if harness == "claude_code":
            payload = json.dumps(
                {"type": "stdio", "command": command, "args": args, "env": env}, ensure_ascii=False
            )
            return [binary, "mcp", "add-json", "-s", "user", MCP_SERVER_NAME, payload]
        env_flags: list[str] = []
        for key, value in env.items():
            env_flags.extend(["--env", f"{key}={value}"])
        if harness == "antigravity":
            return [binary, "mcp", "add", *env_flags, MCP_SERVER_NAME, command, *args]
        return [binary, "mcp", "add", *env_flags, MCP_SERVER_NAME, "--", command, *args]

    # ------------------------------------------------------------------
    # Claude / Codex / Antigravity
    # ------------------------------------------------------------------
    def _install_mcp_cli(self, harness: str, binary: str) -> InstallResult:
        commands: list[list[str]] = []
        original = self._read_registration(harness)
        remove = [binary, "mcp", "remove"]
        if harness == "claude_code":
            remove.extend(["-s", "user"])
        remove.append(MCP_SERVER_NAME)
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
        detail = (stderr or stdout).strip()[:1000] or f"exit {code}"
        if original is not None:
            restore = self._add_command_for(harness, binary, original)
            restore_code, restore_stdout, restore_stderr = self._run(restore, commands)
            if restore_code == 0:
                detail += "; original registration restored"
            else:
                detail += "; restore failed: " + (
                    (restore_stderr or restore_stdout).strip()[:500] or f"exit {restore_code}"
                )
        return InstallResult(harness, "failed", detail, commands=commands)

    def _uninstall_mcp_cli(self, harness: str, binary: str) -> InstallResult:
        commands: list[list[str]] = []
        remove = [binary, "mcp", "remove"]
        if harness == "claude_code":
            remove.extend(["-s", "user"])
        remove.append(MCP_SERVER_NAME)
        code, stdout, stderr = self._run(remove, commands)
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
        failure = ""
        for path in self._dsh_patch_files():
            if self.dry_run:
                files.append(path)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            backup = path.with_suffix(path.suffix + ".harness-mesh.bak")
            original = path.read_text(encoding="utf-8") if path.exists() else ""
            if path.exists() and not backup.exists():
                backup.write_text(original, encoding="utf-8")
            try:
                content = _dsh_patch_content(path, entry)
                _atomic_write_text(path, content)
                _validate_dsh_patch(content)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                if backup.exists():
                    _atomic_write_text(path, backup.read_text(encoding="utf-8"))
                elif path.exists():
                    path.unlink()
                failure = f"failed to update {path}: {exc}"
                break
            files.append(path)
        if failure:
            return InstallResult("deepseek", "failed", failure, files=[str(path) for path in files])
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
class _DshLoader(yaml.SafeLoader):
    """Safe loader that tolerates DSH's ``!!js`` expression scalars."""


def _construct_js_scalar(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    return loader.construct_scalar(node)


_DshLoader.add_constructor("tag:yaml.org,2002:js", _construct_js_scalar)


def _validate_dsh_patch(content: str) -> None:
    yaml.load(content, Loader=_DshLoader)  # noqa: S506 - safe loader with one scalar tag


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", delete=False
    )
    try:
        with handle:
            handle.write(content)
        os.replace(handle.name, path)
    except OSError:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _dsh_patch_content(path: Path, entry: Mapping[str, Any]) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = _remove_dsh_block_text(text).rstrip()
    text = re.sub(r"(?m)^[ \t]*\[\][ \t]*$", "", text).rstrip()
    block = _dsh_block(entry)
    return (text + "\n\n" if text else "") + block


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
    _atomic_write_text(path, _dsh_patch_content(path, entry))


def _remove_dsh_block(path: Path) -> None:
    if not path.exists():
        return
    text = _remove_dsh_block_text(path.read_text(encoding="utf-8"))
    path.write_text(text, encoding="utf-8")


__all__ = ["HarnessInstaller", "InstallResult", "MCP_SERVER_NAME"]
