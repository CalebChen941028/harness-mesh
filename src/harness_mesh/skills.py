"""Local SKILL.md indexing, auto-matching and MCP plugin compilation."""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .config import MeshConfig
from .models import PluginSpec, SkillSpec, TaskRequest
from .security import redact_env

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_+#.-]+|[\u4e00-\u9fff]{2,}")


def _public_plugin(plugin: PluginSpec) -> dict[str, Any]:
    """Serialize a plugin for tool/CLI output with its env values redacted.

    The generated MCP config is intentionally *not* redacted here: those env
    values are what the worker harness needs to start the plugin server.  Only
    the metadata returned by ``list_available``/MCP tool responses hides them.
    """

    payload = plugin.model_dump(mode="json")
    env = payload.get("env")
    if isinstance(env, dict):
        payload["env"] = redact_env(env)
    # ``server_config`` is the raw .mcp.json entry and carries the same env
    # map that ``env`` was parsed from; redact both copies for output.
    server_config = payload.get("server_config")
    if isinstance(server_config, dict):
        copied = dict(server_config)
        server_env = copied.get("env")
        if isinstance(server_env, dict):
            copied["env"] = redact_env(server_env)
        payload["server_config"] = copied
    return payload


class SkillPluginManager:
    """Indexes skills/plugins from the local harness homes and workspace."""

    def __init__(
        self,
        config: MeshConfig,
        workspace_root: str | Path,
        *,
        home_dir: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ):
        self.config = config
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.env = dict(env if env is not None else os.environ)
        self.home_dir = Path(home_dir or self.env.get("HOME") or Path.home()).expanduser().resolve()
        self.state_dir = config.state_path(self.workspace_root)
        self.mcp_dir = self.state_dir / "mcp"
        self._skills_cache: list[SkillSpec] | None = None
        self._plugins_cache: list[PluginSpec] | None = None
        self._cache_time: float = 0.0

    # ------------------------------------------------------------------
    # path helpers
    # ------------------------------------------------------------------
    def _render_root(self, template: str) -> Path:
        dsh_home = self.env.get("DSH_HOME", str(self.home_dir / ".dsh"))
        try:
            rendered = template.format(
                workspace=str(self.workspace_root),
                home=str(self.home_dir),
                dsh_home=dsh_home,
            )
        except (KeyError, ValueError):
            rendered = template
        if rendered.startswith("~"):
            rendered = str(self.home_dir) + rendered[1:]
        path = Path(rendered)
        if not path.is_absolute():
            path = self.workspace_root / path
        return path

    def skill_roots(self) -> list[Path]:
        values = list(self.config.skill_roots) or [
            "~/.dsh/skills",
            "~/.gemini/config/skills",
            "~/.claude/skills",
            "{workspace}/.harness-mesh/skills",
        ]
        return [self._render_root(value) for value in values]

    def plugin_roots(self) -> list[Path]:
        values = list(self.config.plugin_roots) or ["{workspace}", "~/.claude", "~/.codex", "~/.dsh"]
        return [self._render_root(value) for value in values]

    # ------------------------------------------------------------------
    # scanning
    # ------------------------------------------------------------------
    def scan_skills(self, force: bool = False) -> list[SkillSpec]:
        if self._skills_cache is not None and not force:
            return list(self._skills_cache)
        seen_paths: set[str] = set()
        seen_names: set[str] = set()
        skills: list[SkillSpec] = []
        for root in self.skill_roots():
            if not root.exists() or not root.is_dir():
                continue
            try:
                candidates = sorted(root.rglob("SKILL.md"))
            except OSError:
                continue
            for path in candidates:
                try:
                    resolved = str(path.resolve())
                except OSError:
                    continue
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                skill = self._parse_skill_file(path, root)
                if skill is None or skill.name in seen_names:
                    continue
                seen_names.add(skill.name)
                skills.append(skill)
        skills.sort(key=lambda item: item.name.lower())
        self._skills_cache = skills
        self._cache_time = time.time()
        return list(skills)

    def _parse_skill_file(self, path: Path, root: Path) -> SkillSpec | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        metadata: dict[str, Any] = {}
        body = text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    parsed = yaml.safe_load(parts[1]) or {}
                    if isinstance(parsed, dict):
                        metadata = parsed
                    body = parts[2].lstrip("\n")
                except yaml.YAMLError:
                    metadata = {}
                    body = text
        name = str(metadata.get("name") or path.parent.name).strip()
        if not name:
            name = path.parent.name
        description = str(metadata.get("description") or "").strip()
        if not description:
            for line in body.splitlines():
                cleaned = line.strip()
                if cleaned and not cleaned.startswith("#"):
                    description = cleaned[:240]
                    break
        tags_value = metadata.get("tags") or metadata.get("keywords") or []
        tags = self._coerce_tags(tags_value)
        return SkillSpec(
            name=name,
            path=str(path.resolve()),
            description=description,
            source_root=str(root),
            tags=tags,
            body_preview=body[:800],
            metadata=metadata,
        )

    @staticmethod
    def _coerce_tags(value: Any) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[,;]", value) if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def scan_plugins(self, force: bool = False) -> list[PluginSpec]:
        if self._plugins_cache is not None and not force:
            return list(self._plugins_cache)
        plugins: list[PluginSpec] = []
        seen: set[tuple[str, str]] = set()
        for root in self.plugin_roots():
            if not root.exists() or not root.is_dir():
                continue
            for path in self._plugin_candidate_files(root):
                for plugin in self._parse_plugin_file(path, root):
                    key = (plugin.name, plugin.path)
                    if key in seen:
                        continue
                    seen.add(key)
                    plugins.append(plugin)
        plugins.sort(key=lambda item: item.name.lower())
        self._plugins_cache = plugins
        self._cache_time = time.time()
        return list(plugins)

    def _plugin_candidate_files(self, root: Path) -> list[Path]:
        candidates: list[Path] = []
        direct = [
            root / ".mcp.json",
            root / "mcp.json",
            root / ".claude-plugin" / "plugin.json",
            root / "plugin.json",
        ]
        # Harness-specific configuration stores that may embed MCP servers.
        if root.name in {".claude", "claude"}:
            candidates.append(root.parent / ".claude.json")
            candidates.append(root / "settings.json")
        if root.name == ".codex":
            candidates.append(root / "config.toml")
        if root.name == ".gemini":
            candidates.append(root / "settings.json")
        if root.name in {".dsh", "dsh"}:
            candidates.append(root / "cordis.patch.yml")
            try:
                candidates.extend(sorted(root.glob("profiles/*/cordis.patch.yml")))
            except OSError:
                pass
        candidates.extend(direct)
        for pattern in ("*.mcp.json", "plugins/*/plugin.json", "plugins/*/.mcp.json", "plugins/*/mcp.json"):
            try:
                candidates.extend(sorted(root.glob(pattern)))
            except OSError:
                continue
        unique: list[Path] = []
        for candidate in candidates:
            if candidate.exists() and candidate.is_file() and candidate not in unique:
                unique.append(candidate)
        return unique

    def _parse_plugin_file(self, path: Path, root: Path) -> list[PluginSpec]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        payload: Any = None
        if path.suffix == ".toml":
            try:
                payload = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                return []
        if payload is None:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                try:
                    payload = yaml.safe_load(text)
                except yaml.YAMLError:
                    return []
        if not isinstance(payload, (dict, list)):
            return []
        servers: dict[str, Any] = {}
        if isinstance(payload, dict):
            for key in ("mcpServers", "mcp_servers", "servers", "mcp"):
                value = payload.get(key)
                if isinstance(value, dict):
                    servers.update(value)
            if not servers:
                servers = payload
        plugins: list[PluginSpec] = []
        for name, config in servers.items():
            if not isinstance(name, str) or not isinstance(config, dict):
                continue
            if name in {"name", "version", "description", "author", "license", "keywords"}:
                continue
            command = config.get("command") or config.get("cmd")
            url = config.get("url") or config.get("endpoint")
            if not command and not url:
                continue
            args = config.get("args") or []
            if not isinstance(args, list):
                args = [str(args)]
            env = config.get("env") or {}
            if not isinstance(env, dict):
                env = {}
            plugins.append(
                PluginSpec(
                    name=name,
                    path=str(path.resolve()),
                    kind="mcp",
                    description=str(config.get("description") or ""),
                    command=str(command) if command else None,
                    args=[str(item) for item in args],
                    env={str(key): str(value) for key, value in env.items()},
                    server_config=config,
                    source_root=str(root),
                )
            )
        return plugins

    # ------------------------------------------------------------------
    # matching / resolution
    # ------------------------------------------------------------------
    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {token.lower() for token in _TOKEN_RE.findall(text or "") if len(token) > 1}

    def match_skills(self, query: str, limit: int = 5) -> list[SkillSpec]:
        skills = self.scan_skills()
        query = (query or "").strip()
        if not query:
            return skills[: max(0, limit)]
        tokens = self._tokenize(query)
        lowered = query.lower()
        scored: list[tuple[float, SkillSpec]] = []
        for skill in skills:
            haystack = " ".join(
                [skill.name, skill.description, " ".join(skill.tags), skill.body_preview]
            ).lower()
            score = 0.0
            if lowered and lowered in haystack:
                score += 8.0
            for token in tokens:
                if token in skill.name.lower():
                    score += 6.0
                elif token in " ".join(skill.tags).lower():
                    score += 4.0
                elif token in skill.description.lower():
                    score += 3.0
                elif token in skill.body_preview.lower():
                    score += 1.0
            if score > 0:
                scored.append((score, skill))
        scored.sort(key=lambda item: (-item[0], item[1].name.lower()))
        return [skill for _score, skill in scored[: max(0, limit)]]

    def match_plugins(self, query: str, limit: int = 5) -> list[PluginSpec]:
        plugins = self.scan_plugins()
        tokens = self._tokenize(query)
        if not tokens:
            return plugins[: max(0, limit)]
        scored: list[tuple[float, PluginSpec]] = []
        for plugin in plugins:
            haystack = f"{plugin.name} {plugin.description}".lower()
            score = sum(2.0 for token in tokens if token in haystack)
            if score > 0:
                scored.append((score, plugin))
        scored.sort(key=lambda item: -item[0])
        return [plugin for _score, plugin in scored[: max(0, limit)]]

    def resolve_for_task(
        self, task: TaskRequest, *, max_auto_skills: int = 3
    ) -> tuple[list[SkillSpec], list[PluginSpec]]:
        skills = self.scan_skills()
        plugins = self.scan_plugins()
        selected_skills: list[SkillSpec] = []
        for requested in task.skills:
            matched = self._find_by_name_or_path(skills, requested)
            if matched is not None and matched not in selected_skills:
                selected_skills.append(matched)
        if task.auto_match_skills:
            query = " ".join([task.objective, task.context_notes, " ".join(task.context_files)])
            for skill in self.match_skills(query, limit=max_auto_skills):
                if skill not in selected_skills:
                    selected_skills.append(skill)
        selected_plugins: list[PluginSpec] = []
        for requested in task.plugins:
            matched = self._find_by_name_or_path(plugins, requested)
            if matched is not None and matched not in selected_plugins:
                selected_plugins.append(matched)
        return selected_skills, selected_plugins

    @staticmethod
    def _find_by_name_or_path(items: Iterable[Any], requested: str) -> Any | None:
        requested = (requested or "").strip()
        if not requested:
            return None
        for item in items:
            if item.name == requested:
                return item
        lowered = requested.lower()
        for item in items:
            if item.name.lower() == lowered or item.path == requested:
                return item
        for item in items:
            if lowered in item.name.lower():
                return item
        return None

    # ------------------------------------------------------------------
    # prompt / MCP compilation
    # ------------------------------------------------------------------
    def build_skills_prompt(self, skills: Iterable[SkillSpec]) -> str:
        skill_list = list(skills)
        if not skill_list:
            return ""
        rows: list[str] = ["<activated_skills>"]
        for skill in skill_list:
            rows.append(f"## {skill.name}")
            if skill.description:
                rows.append(skill.description)
            rows.append(f"SKILL.md: {skill.path}")
            if skill.tags:
                rows.append(f"tags: {', '.join(skill.tags)}")
            rows.append("")
        rows.append(
            "Read each SKILL.md above before acting and follow its workflow. "
            "Skills are local instructions; the ticket objective still takes precedence."
        )
        rows.append("</activated_skills>")
        return "\n".join(rows)

    def compile_mcp_config(
        self,
        task_id: str,
        plugins: Iterable[PluginSpec],
        *,
        extra_servers: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> Path | None:
        plugin_list = [plugin for plugin in plugins if plugin.server_config or plugin.command]
        extra = dict(extra_servers or {})
        if not plugin_list and not extra:
            return None
        servers: dict[str, Any] = {}
        for plugin in plugin_list:
            config = dict(plugin.server_config)
            if plugin.command and "command" not in config:
                config["command"] = plugin.command
            if plugin.args and "args" not in config:
                config["args"] = list(plugin.args)
            if plugin.env and "env" not in config:
                config["env"] = dict(plugin.env)
            servers[plugin.name] = config
        for name, config in extra.items():
            servers[str(name)] = dict(config)
        # Env values stay verbatim: this file is how the worker harness starts
        # the plugin server, so redacting it would break the plugin.  The file
        # is protected by 0700/0600 permissions instead.
        safe_id = _SAFE_ID.sub("_", task_id.strip()) or "task"
        self.mcp_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.mcp_dir, 0o700)
        except OSError:
            pass
        path = self.mcp_dir / f".mcp.{safe_id}.json"
        path.write_text(json.dumps({"mcpServers": servers}, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def mcp_config_path(self, task_id: str) -> Path:
        safe_id = _SAFE_ID.sub("_", task_id.strip()) or "task"
        return self.mcp_dir / f".mcp.{safe_id}.json"

    def list_available(self, query: str = "") -> dict[str, Any]:
        skills = self.match_skills(query, limit=50) if query else self.scan_skills()
        plugins = self.match_plugins(query, limit=50) if query else self.scan_plugins()
        return {
            "skills": [skill.model_dump(mode="json") for skill in skills],
            "plugins": [_public_plugin(plugin) for plugin in plugins],
            "skill_roots": [str(root) for root in self.skill_roots()],
            "plugin_roots": [str(root) for root in self.plugin_roots()],
        }

    def invalidate(self) -> None:
        self._skills_cache = None
        self._plugins_cache = None


__all__ = ["SkillPluginManager"]
