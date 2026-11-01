"""Configuration loading, difficulty heuristics and hand-off prompt assembly."""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .models import (
    AssignmentRecord,
    ContinuationCheckpoint,
    TaskDifficulty,
    TaskRequest,
)


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


class HarnessConfig(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    enabled: bool = True
    display_name: str = ""
    binary: str = ""
    binary_aliases: list[str] = Field(default_factory=list)
    binary_paths: list[str] = Field(default_factory=list)
    version_args: list[str] = Field(default_factory=lambda: ["--version"])
    auto_approve: bool = True
    model_list_args: list[str] = Field(default_factory=list)
    auth_style: str = "bearer"  # bearer | anthropic | gemini
    base_url_env: list[str] = Field(default_factory=list)
    api_key_env: list[str] = Field(default_factory=list)
    default_base_url: str = ""
    models_endpoint: str = "/v1/models"
    models_response_shape: str = "openai"  # openai | gemini
    config_files: list[str] = Field(default_factory=list)
    env_model_variables: list[str] = Field(default_factory=list)
    execute_template: list[str] = Field(default_factory=list)
    skill_injection: str = "prompt"
    timeout_seconds: int = 1800
    extra: dict[str, Any] = Field(default_factory=dict)

    def binary_candidates(self) -> list[str]:
        candidates: list[str] = []
        for candidate in [self.binary, *self.binary_aliases]:
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        return candidates


def _is_executable(path: str | Path) -> bool:
    try:
        candidate = Path(path)
        return candidate.is_file() and os.access(candidate, os.X_OK)
    except OSError:
        return False


def _binary_sort_key(path: str) -> tuple[tuple[int, ...], float]:
    versions = tuple(int(item) for item in re.findall(r"\d+", path))
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return versions, mtime


def _resolve_explicit_only(
    cfg: HarnessConfig, which: Any, home_dir: str | Path | None
) -> str | None:
    home = Path(home_dir).expanduser() if home_dir else Path.home()
    for candidate in cfg.binary_candidates():
        found = which(candidate)
        if found:
            return found
        if any(token in candidate for token in ("/", os.sep)) or candidate.startswith(("~", ".")):
            expanded = str(home) + candidate[1:] if candidate.startswith("~") else candidate
            if _is_executable(expanded):
                return str(Path(expanded).expanduser().resolve())
    return None


def resolve_harness_binary(
    cfg: HarnessConfig,
    which_fn: Any = None,
    state_dir: str | Path | None = None,
    home_dir: str | Path | None = None,
    *,
    allow_system_paths: bool | None = None,
) -> str | None:
    """Resolve a harness binary from PATH, configured macOS bundle paths and shims.

    When a test injects a custom ``which_fn`` the resolver deliberately does not
    scan the host's ``/Applications`` paths, so unit tests cannot accidentally
    pick up the developer machine's real harnesses.
    """

    which = which_fn or shutil.which
    injected_which = which_fn is not None and which_fn is not shutil.which
    if os.environ.get("HARNESS_MESH_DISABLE_SYSTEM_BINARIES", "").lower() in {"1", "true", "yes"}:
        return _resolve_explicit_only(cfg, which, home_dir)
    if allow_system_paths is None:
        allow_system_paths = not injected_which
    home = Path(home_dir).expanduser() if home_dir else Path.home()

    def _expand(value: str) -> str:
        if value.startswith("~"):
            return str(home) + value[1:]
        return value

    for candidate in cfg.binary_candidates():
        found = which(candidate)
        if found:
            return found
        # Explicit relative/temp paths are always honoured, even under a test
        # injected which function.
        if any(token in candidate for token in ("/", os.sep)) or candidate.startswith(("~", ".")):
            expanded = _expand(candidate)
            if _is_executable(expanded):
                return str(Path(expanded).expanduser().resolve())

    if allow_system_paths:
        matches: list[str] = []
        for pattern in cfg.binary_paths:
            expanded = _expand(pattern)
            try:
                matches.extend(glob.glob(expanded, recursive=True))
            except OSError:
                continue
        executables = [match for match in matches if _is_executable(match)]
        if executables:
            executables.sort(key=_binary_sort_key, reverse=True)
            return str(Path(executables[0]).resolve())

    # macOS ZCode ships as an Electron app with a CJS entry point; create a
    # tiny wrapper only when the real executable is requested via the host.
    if allow_system_paths and cfg.extra.get("macos_electron_bin") and cfg.extra.get("macos_cjs_entry"):
        electron = Path(_expand(str(cfg.extra["macos_electron_bin"])))
        cjs_entry = Path(_expand(str(cfg.extra["macos_cjs_entry"])))
        if electron.is_file() and cjs_entry.is_file() and state_dir:
            provider_config = str(
                cfg.extra.get(
                    "macos_builtin_provider_config",
                    "/Applications/ZCode.app/Contents/Resources/config/provider/zcode-builtin.json",
                )
            )
            shim = Path(state_dir).expanduser() / "bin" / "zcode"
            try:
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_text(
                    "#!/bin/sh\n"
                    f'ELECTRON_RUN_AS_NODE=1 ZCODE_BUILTIN_PROVIDER_CONFIG_FILE={shlex.quote(provider_config)} '
                    f'exec {shlex.quote(str(electron))} {shlex.quote(str(cjs_entry))} "$@"\n',
                    encoding="utf-8",
                )
                shim.chmod(0o755)
                return str(shim)
            except OSError:
                return None
    return None


class MeshConfig(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    state_dir: str = ".harness-mesh"
    summary_max_chars: int = 1500
    diff_preview_max_chars: int = 4000
    default_timeout_seconds: int = 1800
    max_call_depth: int = 3
    max_failover_attempts: int = 4
    transient_retries: int = 1
    quota_cooldown_seconds: int = 300
    quota_probe_interval_seconds: int = 60
    model_cache_ttl_seconds: int = 300
    allowed_workspace_roots: list[str] = Field(default_factory=list)
    suspend_when_no_candidate: bool = True
    failover_on_error: bool = True
    failover_priority: list[str] = Field(
        default_factory=lambda: ["claude_code", "codex", "deepseek", "antigravity", "zcode"]
    )
    difficulty_effort_map: dict[str, str] = Field(
        default_factory=lambda: {
            "low": "low",
            "medium": "medium",
            "high": "high",
            "critical": "high",
        }
    )
    difficulty_timeout_seconds: dict[str, int] = Field(
        default_factory=lambda: {
            "low": 600,
            "medium": 1200,
            "high": 1800,
            "critical": 3600,
        }
    )
    skill_roots: list[str] = Field(default_factory=list)
    plugin_roots: list[str] = Field(default_factory=list)
    harnesses: dict[str, HarnessConfig] = Field(default_factory=dict)

    def enabled_harnesses(self) -> list[str]:
        return [name for name, cfg in self.harnesses.items() if cfg.enabled]

    def harness_names(self) -> list[str]:
        return list(self.harnesses.keys())

    def get_harness(self, name: str) -> HarnessConfig | None:
        return self.harnesses.get(name)

    def ordered_candidates(self) -> list[str]:
        ordered: list[str] = []
        for name in self.failover_priority:
            if name in self.harnesses and name not in ordered:
                ordered.append(name)
        for name in self.harnesses:
            if name not in ordered:
                ordered.append(name)
        return ordered

    def state_path(self, workspace_root: str | Path) -> Path:
        candidate = Path(os.path.expanduser(self.state_dir))
        if candidate.is_absolute():
            return candidate
        return Path(workspace_root).resolve() / candidate

    def effort_for(self, difficulty: TaskDifficulty | str) -> str:
        value = difficulty.value if isinstance(difficulty, TaskDifficulty) else str(difficulty)
        return self.difficulty_effort_map.get(value, "medium")

    def timeout_for(self, difficulty: TaskDifficulty | str) -> int:
        value = difficulty.value if isinstance(difficulty, TaskDifficulty) else str(difficulty)
        return int(self.difficulty_timeout_seconds.get(value, self.default_timeout_seconds))


def _default_harnesses() -> dict[str, HarnessConfig]:
    """Built-in connection templates with the local macOS production paths.

    Model identifiers are intentionally absent: inventory comes from
    :mod:`harness_mesh.discovery` at runtime.
    """

    return {
        "claude_code": HarnessConfig(
            display_name="Claude Code",
            binary="claude",
            binary_paths=[
                "~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude",
                "~/.local/bin/claude",
                "/opt/homebrew/bin/claude",
                "/usr/local/bin/claude",
            ],
            model_list_args=["models", "list"],
            auto_approve=True,
            auth_style="anthropic",
            base_url_env=["ANTHROPIC_BASE_URL"],
            api_key_env=["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
            default_base_url="https://api.anthropic.com",
            config_files=[
                "~/.claude/cache/model-catalog/*.json",
                "~/.claude.json",
                "~/.claude/settings.json",
                "{workspace}/.claude/settings.json",
                "{workspace}/.claude/settings.local.json",
            ],
            env_model_variables=[
                "ANTHROPIC_MODEL",
                "ANTHROPIC_SMALL_FAST_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL",
            ],
            execute_template=[
                "claude",
                "-p",
                "{prompt}",
                "--output-format",
                "json",
                "--model",
                "{model}",
                "--effort",
                "{effort}",
                "--append-system-prompt",
                "{system_prompt}",
                "--mcp-config",
                "{mcp_config}",
            ],
            skill_injection="system_prompt",
        ),
        "codex": HarnessConfig(
            display_name="OpenAI Codex CLI",
            binary="codex",
            binary_paths=[
                "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                "~/.codex/plugins/.plugin-appserver/codex-cli/bin/codex",
                "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
                "/opt/homebrew/bin/codex",
                "/usr/local/bin/codex",
            ],
            auto_approve=True,
            auth_style="bearer",
            base_url_env=["OPENAI_BASE_URL"],
            api_key_env=["OPENAI_API_KEY", "CODEX_API_KEY"],
            default_base_url="https://api.openai.com",
            config_files=[
                "~/.codex/config.toml",
                "~/.codex/models_cache.json",
                "~/.codex/auth.json",
                "{workspace}/.codex/config.toml",
            ],
            env_model_variables=["CODEX_MODEL", "OPENAI_MODEL"],
            execute_template=[
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--cd",
                "{workdir}",
                "--json",
                "--model",
                "{model}",
                "{prompt}",
            ],
            skill_injection="prompt",
        ),
        "deepseek": HarnessConfig(
            display_name="DeepSeek Harness",
            binary="dsh",
            binary_aliases=["deepseek"],
            binary_paths=[
                "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh",
                "~/.dsh/bin/dsh",
                "/opt/homebrew/bin/dsh",
                "/usr/local/bin/dsh",
            ],
            model_list_args=[],
            auto_approve=True,
            auth_style="bearer",
            base_url_env=["DEEPSEEK_BASE_URL", "DSH_BASE_URL"],
            api_key_env=["DEEPSEEK_API_KEY", "DSH_API_KEY"],
            default_base_url="https://api.deepseek.com",
            config_files=[
                "{dsh_home}/profiles/desktop/cordis.patch.yml",
                "{dsh_home}/profiles/default/cordis.patch.yml",
                "{dsh_home}/profiles/headless/cordis.patch.yml",
                "~/.deepseek/config.json",
                "{workspace}/.deepseek.yaml",
            ],
            env_model_variables=["DEEPSEEK_MODEL", "DSH_MODEL"],
            execute_template=[
                "dsh",
                "--profile",
                "headless",
                "--json",
                "{prompt}",
            ],
            skill_injection="native",
            extra={
                "fallback_binary": "deepseek",
                "builtin_runtime": True,
                "builtin_api_key_env": "DEEPSEEK_API_KEY",
                "builtin_base_url": "https://api.deepseek.com",
                "builtin_max_turns": 12,
                "desktop_profile": "desktop",
                "headless_profile": "headless",
                "overlay_enabled": True,
            },
        ),
        "antigravity": HarnessConfig(
            display_name="Google Antigravity",
            binary="agy",
            binary_paths=[
                "/Applications/Gemini.app/Contents/Helpers/agy",
                "~/.local/bin/agy",
                "/opt/homebrew/bin/agy",
                "/usr/local/bin/agy",
            ],
            model_list_args=["models"],
            auto_approve=True,
            auth_style="gemini",
            base_url_env=["ANTIGRAVITY_BASE_URL", "GEMINI_API_BASE_URL"],
            api_key_env=["GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTIGRAVITY_API_KEY"],
            default_base_url="https://generativelanguage.googleapis.com",
            models_endpoint="/v1beta/models",
            models_response_shape="gemini",
            config_files=[
                "~/.gemini/antigravity-cli/settings.json",
                "~/.gemini/config/config.json",
                "~/.gemini/settings.json",
            ],
            env_model_variables=["ANTIGRAVITY_MODEL", "GEMINI_MODEL"],
            execute_template=[
                "agy",
                "-p",
                "{prompt}",
                "--output-format",
                "json",
                "--model",
                "{model}",
                "--effort",
                "{effort}",
            ],
            skill_injection="native",
            extra={"sdk_module": "google.antigravity", "sdk_enabled": True},
        ),
        "zcode": HarnessConfig(
            display_name="ZCode",
            binary="zcode",
            binary_paths=[
                "~/.local/bin/zcode",
                "/opt/homebrew/bin/zcode",
                "/usr/local/bin/zcode",
            ],
            model_list_args=[],
            auto_approve=True,
            auth_style="bearer",
            base_url_env=["ZCODE_BASE_URL"],
            api_key_env=["ZCODE_API_KEY"],
            default_base_url="",
            config_files=[
                "~/.zcode/v2/config.json",
                "~/.zcode/v2/provider_config.json",
                "~/.zcode/v2/setting.json",
                "~/.zcode/config.yaml",
                "~/.zcode/settings.json",
                "~/.config/zcode/config.json",
            ],
            env_model_variables=["ZCODE_MODEL"],
            execute_template=[
                "zcode",
                "-p",
                "{prompt}",
                "--cwd",
                "{workdir}",
                "--json",
            ],
            skill_injection="mixed",
            extra={
                "macos_electron_bin": "/Applications/ZCode.app/Contents/MacOS/ZCode",
                "macos_cjs_entry": "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
                "macos_builtin_provider_config": (
                    "/Applications/ZCode.app/Contents/Resources/config/provider/zcode-builtin.json"
                ),
                "supports_mcp_flag": False,
            },
        ),
    }


def default_config() -> MeshConfig:
    return MeshConfig(
        skill_roots=[
            "~/.dsh/skills",
            "~/.gemini/config/skills",
            "~/.gemini/skills",
            "~/.agents/skills",
            "~/.claude/skills",
            "{workspace}/.harness-mesh/skills",
        ],
        plugin_roots=[
            "{workspace}",
            "{workspace}/.harness-mesh/plugins",
            "~/.claude",
            "~/.codex",
            "~/.dsh",
            "~/.gemini",
        ],
        harnesses=_default_harnesses(),
    )


def find_config_path(workspace_root: str | Path | None = None) -> Path | None:
    env_path = os.environ.get("HARNESS_MESH_CONFIG")
    if env_path:
        candidate = Path(os.path.expanduser(env_path))
        if candidate.exists():
            return candidate
    root = Path(workspace_root or os.getcwd()).resolve()
    candidates = [
        root / ".harness-mesh" / "config.yaml",
        root / "config" / "harnesses.yaml",
        root / "config" / "harnesses.example.yaml",
        Path(__file__).resolve().parents[2] / "config" / "harnesses.example.yaml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_config(path: str | Path | None = None, workspace_root: str | Path | None = None) -> MeshConfig:
    """Load YAML configuration on top of the built-in connection templates."""

    resolved: Path | None
    if path is None:
        resolved = find_config_path(workspace_root)
    else:
        resolved = Path(os.path.expanduser(str(path)))

    if resolved is None or not resolved.exists():
        return default_config()

    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"harness-mesh config must be a mapping: {resolved}")

    defaults = default_config()
    base = defaults.model_dump()
    harness_defaults = base.pop("harnesses")

    for key, value in raw.items():
        if key == "harnesses":
            continue
        if key in MeshConfig.model_fields:
            base[key] = value

    harness_overrides = raw.get("harnesses") or {}
    merged_harnesses: dict[str, HarnessConfig] = {}
    for name, default_harness in harness_defaults.items():
        override = harness_overrides.get(name) or {}
        merged_harnesses[name] = _merge_harness_config(default_harness, override)
    for name, override in harness_overrides.items():
        if name not in merged_harnesses:
            merged_harnesses[name] = _merge_harness_config({}, override)

    # Preserve any user supplied ordering for harnesses by inserting existing
    # defaults first and appending genuinely new harness entries.
    base["harnesses"] = merged_harnesses
    return MeshConfig(**base)


def _merge_harness_config(
    default_harness: HarnessConfig | dict[str, Any], override: Mapping[str, Any]
) -> HarnessConfig:
    if isinstance(default_harness, HarnessConfig):
        data = default_harness.model_dump()
    else:
        data = HarnessConfig().model_dump()
        data.update({k: v for k, v in default_harness.items() if k in HarnessConfig.model_fields})
    extra = dict(data.get("extra") or {})
    for key, value in (override or {}).items():
        if key == "extra":
            extra.update(value or {})
        elif key in HarnessConfig.model_fields:
            data[key] = value
        else:
            extra[key] = value
    data["extra"] = extra
    return HarnessConfig(**data)


def render_path(template: str, workspace_root: str | Path, extra: Mapping[str, Any] | None = None) -> str:
    values: dict[str, Any] = {
        "workspace": str(Path(workspace_root).resolve()),
        "home": str(Path.home()),
        "dsh_home": os.environ.get("DSH_HOME", str(Path.home() / ".dsh")),
    }
    if extra:
        values.update(extra)
    try:
        rendered = template.format_map(_SafeFormatDict(values))
    except (KeyError, ValueError):
        rendered = template
    return str(Path(os.path.expanduser(rendered)))


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:  # pragma: no cover - defensive
        return "{" + key + "}"


# ---------------------------------------------------------------------------
# Difficulty heuristics
# ---------------------------------------------------------------------------

_COMPLEX_TERMS = {
    # weight 2 / phrase
    2: [
        "architecture",
        "architectural",
        "架构",
        "refactor",
        "refactoring",
        "重构",
        "migration",
        "migrate",
        "迁移",
        "security",
        "secure",
        "安全",
        "concurrency",
        "concurrent",
        "并发",
        "distributed",
        "分布式",
        "algorithm",
        "算法",
        "compiler",
        "编译器",
        "protocol",
        "协议",
        "encryption",
        "加密",
        "cross-module",
        "cross module",
        "跨模块",
        "end-to-end",
        "端到端",
        "production-grade",
        "production grade",
        "生产级",
        "complete implementation",
        "完整实现",
        "fault tolerance",
        "容错",
        "state machine",
        "状态机",
        "scheduler",
        "调度器",
        "orchestration",
        "编排",
    ],
    # weight 1 / phrase
    1: [
        "test",
        "tests",
        "测试",
        "integration",
        "集成",
        "performance",
        "性能",
        "optimize",
        "optimise",
        "优化",
        "api",
        "接口",
        "schema",
        "数据模型",
        "database",
        "数据库",
        "cli",
        "command line",
        "命令行",
        "sdk",
        "module",
        "模块",
        "plugin",
        "插件",
        "adapter",
        "适配器",
        "implement",
        "implementation",
        "实现",
        "feature",
        "功能",
        "bug",
        "修复",
        "review",
        "审查",
        "deploy",
        "部署",
        "config",
        "配置",
    ],
}

_SIMPLE_TERMS = [
    "typo",
    "拼写",
    "rename",
    "重命名",
    "comment",
    "注释",
    "readme",
    "文档",
    "docstring",
    "formatting",
    "格式化",
    "whitespace",
    "version bump",
    "版本号",
    "一行",
]

_CRITICAL_MARKERS = [
    "data loss",
    "数据丢失",
    "security vulnerability",
    "安全漏洞",
    "outage",
    "故障",
    "critical",
    "严重",
    "不可用",
    "race condition",
    "竞态",
]


def evaluate_difficulty(
    objective: str,
    context_files: Iterable[str] | None = None,
    context_notes: str = "",
    target_harness: str | None = None,
) -> TaskDifficulty:
    """Deterministically score an objective into a four-level difficulty tier.

    The heuristics use scope signals (file count, objective size, semantic
    complexity words and de-scoping words) rather than model names, so the
    result remains stable across harnesses.
    """

    text = f"{objective}\n{context_notes}".lower()
    files = [str(item) for item in (context_files or [])]
    score = 1

    if len(files) >= 8:
        score += 3
    elif len(files) >= 4:
        score += 2
    elif len(files) >= 2:
        score += 1

    if len(objective) > 4000:
        score += 2
    elif len(objective) > 1200:
        score += 1

    for weight, terms in _COMPLEX_TERMS.items():
        matched_terms = {term for term in terms if term in text}
        # Each distinct phrase contributes once; overlapping phrases in the same
        # weight group are capped to avoid runaway scores on wordy objectives.
        score += weight * min(len(matched_terms), 4)

    simple_hits = sum(1 for term in _SIMPLE_TERMS if term in text)
    score -= min(simple_hits, 3)

    if any(marker in text for marker in _CRITICAL_MARKERS):
        score += 1

    if target_harness:
        # Cross-harness work carries extra coordination cost.
        score += 0
        if target_harness not in {"claude_code", "codex", "deepseek", "antigravity", "zcode"}:
            score += 1

    score = max(0, score)
    if score >= 7:
        return TaskDifficulty.CRITICAL
    if score >= 4:
        return TaskDifficulty.HIGH
    if score >= 2:
        return TaskDifficulty.MEDIUM
    return TaskDifficulty.LOW


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def cap_text(text: str, limit: int, marker: str = "\n... [truncated by harness-mesh] ...\n") -> tuple[str, bool]:
    if limit <= 0 or text is None:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    if limit <= len(marker):
        return text[:limit], True
    available = limit - len(marker)
    head_len = available - available // 2
    tail_len = available // 2
    head = text[:head_len]
    tail = text[-tail_len:] if tail_len > 0 else ""
    return f"{head}{marker}{tail}", True


def _format_file_list(files: Iterable[str]) -> str:
    rows = [f"- {name}" for name in files]
    return "\n".join(rows) if rows else "- (none recorded)"


def build_handoff_prompt(
    task: TaskRequest,
    checkpoint: ContinuationCheckpoint | None = None,
    *,
    next_harness: str | None = None,
    next_model: str | None = None,
    skills_prompt: str = "",
    assignment_history: list[AssignmentRecord] | None = None,
    checkpoint_diff_max_chars: int = 6000,
    reasoning_effort: str | None = None,
    project_instructions: str = "",
    project_memory: str = "",
    context_previews: str = "",
    upstream_outputs: str = "",
) -> str:
    """Build the prompt for one assignment (initial or continuation).

    Continuation prompts carry a ``<continuation_checkpoint>`` block and a
    structured Recovery Note modelled on dsh-superagent's
    ``assignmentInterrupted`` protocol.  The note forces the incoming agent to
    inspect the worktree (``git status`` / ``git diff``) *before* touching any
    external state, which prevents redoing or clobbering partial work.
    """

    sections: list[str] = []
    sections.append(
        "You are executing one Assignment of a harness-mesh Ticket. "
        "Complete the objective inside the provided working directory and report what you changed."
    )
    sections.append(
        "<ticket>\n"
        f"task_id: {task.task_id}\n"
        f"objective: {task.objective}\n"
        f"difficulty: {task.difficulty.value if isinstance(task.difficulty, TaskDifficulty) else task.difficulty}\n"
        f"workspace_mode: {_enum_value(task.workspace_mode)}\n"
        f"target_harness: {task.target_harness}\n"
        f"next_harness: {next_harness or task.target_harness}\n"
        f"next_model: {next_model or task.model or '(harness default)'}\n"
        f"reasoning_effort: {reasoning_effort or '(harness default)'}\n"
        f"call_depth: {task.call_depth}\n"
        f"trace_chain: {json.dumps(list(task.trace_chain), ensure_ascii=False)}\n"
        "</ticket>"
    )
    next_depth = task.call_depth + 1
    next_trace = list(task.trace_chain)
    if next_harness and next_harness not in next_trace:
        next_trace.append(next_harness)
    sections.append(
        "<dispatch_context>\n"
        "If this assignment dispatches a child ticket through harness-mesh, pass "
        f"call_depth={next_depth} and trace_chain={json.dumps(next_trace, ensure_ascii=False)}. "
        "The MCP dispatch tool defaults these values from HARNESS_MESH_CALL_DEPTH and "
        "HARNESS_MESH_TRACE_CHAIN, which are set for your process.\n"
        "</dispatch_context>"
    )

    if _enum_value(task.workspace_mode) == "read_only":
        sections.append(
            "<workspace_mode_note>\n"
            "This is a READ-ONLY assignment. Do not create, modify, delete or rename any file, "
            "do not run mutating commands, and do not attempt to apply patches. Inspect and report only.\n"
            "</workspace_mode_note>"
        )

    if task.context_files:
        sections.append("<context_files>\n" + _format_file_list(task.context_files) + "\n</context_files>")
    if task.context_notes:
        sections.append("<context_notes>\n" + task.context_notes + "\n</context_notes>")
    if project_instructions.strip():
        sections.append(project_instructions.strip())
    if project_memory.strip():
        sections.append(project_memory.strip())
    if context_previews.strip():
        sections.append(context_previews.strip())
    if upstream_outputs.strip():
        sections.append(upstream_outputs.strip())

    if assignment_history:
        rows = []
        for record in assignment_history:
            rows.append(
                f"- assignment {record.attempt}: {record.harness}/{record.model} "
                f"status={record.status} reason={record.error_reason or 'n/a'}"
            )
        sections.append("<assignment_history>\n" + "\n".join(rows) + "\n</assignment_history>")

    if checkpoint is not None:
        diff, _ = cap_text(checkpoint.partial_git_diff or "", checkpoint_diff_max_chars)
        stdout_tail, _ = cap_text(checkpoint.partial_stdout_tail or "", 3000)
        checkpoint_block = [
            "<continuation_checkpoint>",
            f"interrupted_harness: {checkpoint.interrupted_harness}",
            f"interrupted_model: {checkpoint.interrupted_model}",
            f"interrupted_by: {checkpoint.interrupted_harness}/{checkpoint.interrupted_model}",
            f"interruption_reason: {checkpoint.interruption_reason}",
            f"captured_at: {checkpoint.captured_at}",
            f"worktree_path: {checkpoint.worktree_path or '(current working directory)'}",
            f"last_assignment_id: {checkpoint.last_assignment_id or 'unknown'}",
            "partial_modified_files:",
            _format_file_list(checkpoint.partial_modified_files),
            "<partial_git_diff>",
            diff or "(no tracked diff was captured)",
            "</partial_git_diff>",
            "<partial_stdout_tail>",
            stdout_tail or "(no captured output)",
            "</partial_stdout_tail>",
            "</continuation_checkpoint>",
        ]
        sections.append("\n".join(checkpoint_block))
        sections.append(
            "<recovery_note>\n"
            "This assignment continues an interrupted ticket in the SAME working directory. "
            "The previous agent already produced the files and diff listed above. "
            "Before performing ANY external side effect or edit, you MUST:\n"
            "1. Run `git status --porcelain` and confirm the partial files still exist.\n"
            "2. Run `git diff HEAD` (and inspect untracked files) to understand the partial work.\n"
            "3. Read each file listed in partial_modified_files and preserve correct edits.\n"
            "4. Continue from the exact point of interruption; do NOT restart from scratch, "
            "do NOT revert or overwrite the previous agent's work unless it is provably wrong.\n"
            "5. Only finish the remaining work, then summarise the combined result.\n"
            "</recovery_note>"
        )

    if skills_prompt.strip():
        sections.append(skills_prompt.strip())

    sections.append(
        "<output_contract>\n"
        "Work directly in the current directory. Keep changes scoped to the objective. "
        "At the end, state the files changed and any remaining risk. Do not claim completion "
        "unless the remaining work is actually finished.\n"
        "</output_contract>"
    )
    return "\n\n".join(sections)


def build_initial_prompt(task: TaskRequest, **kwargs: Any) -> str:
    return build_handoff_prompt(task, task.checkpoint, **kwargs)


def build_recovery_note(checkpoint: ContinuationCheckpoint) -> str:
    """Return only the Recovery Note for callers that need it standalone."""

    pseudo_task = TaskRequest(
        task_id="recovery",
        target_harness=checkpoint.interrupted_harness,
        objective="resume interrupted assignment",
        workspace_root=".",
    )
    prompt = build_handoff_prompt(pseudo_task, checkpoint)
    start = prompt.find("<recovery_note>")
    end = prompt.find("</recovery_note>")
    if start >= 0 and end >= 0:
        return prompt[start : end + len("</recovery_note>")]
    return prompt


def config_to_json(config: MeshConfig) -> str:
    return json.dumps(config.model_dump(mode="json"), indent=2, ensure_ascii=False)


__all__ = [
    "HarnessConfig",
    "MeshConfig",
    "build_handoff_prompt",
    "build_initial_prompt",
    "build_recovery_note",
    "cap_text",
    "config_to_json",
    "default_config",
    "evaluate_difficulty",
    "find_config_path",
    "load_config",
    "render_path",
]
