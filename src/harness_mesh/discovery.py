"""Runtime model discovery and adaptive capability tiering.

No model identifier is hard-coded as an allowed/available model.  The engine
combines three waterfall layers at runtime:

1. Local activation state (``~/.claude``, ``~/.codex``, ``$DSH_HOME``,
   ``~/.gemini``, ``~/.zcode`` plus environment variables).
2. Live ``/v1/models`` (or Gemini ``/v1beta/models``) query with the currently
   configured credential.
3. Native CLI enumeration plus the ``<cli_active_default>`` placeholder for
   OAuth-activated CLIs that never expose a literal model name.

Every result is scored from semantic family/role features and mapped onto the
``low`` / ``medium`` / ``high`` / ``critical`` tiers.  A single authorised
model is bound to all four tiers; multiple models are distributed by rank.
"""

from __future__ import annotations

import asyncio
import glob
import importlib.util
import json
import os
import re
import shutil
import time
import tomllib
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

import httpx
import yaml

from .config import HarnessConfig, MeshConfig, resolve_harness_binary
from .models import DiscoveredModel
from .storage import StateStore

PLACEHOLDER_MODEL = "<cli_active_default>"
TIER_NAMES = ("low", "medium", "high", "critical")
SOURCE_PRIORITY = {
    "local_config": 5,
    "env_var": 4,
    "api_models": 3,
    "cli_probe": 2,
    "cli_active_default": 1,
}

_MODEL_KEY_HINTS = {
    "model",
    "models",
    "model_id",
    "modelid",
    "model_name",
    "modelname",
    "default_model",
    "defaultmodel",
    "model_list",
    "modellist",
    "model_catalog",
    "modelcatalog",
    "small_fast_model",
    "haiku_model",
    "sonnet_model",
    "opus_model",
    "additionalmodeloptionscache",
    "personalmodelids",
    "modelorder",
}
_MODEL_SUFFIXES = ("_model", "_model_id", "_modelid")
_AUTH_KEY_HINTS = {
    "api_key",
    "apikey",
    "api-key",
    "x-api-key",
    "token",
    "access_token",
    "accesstoken",
    "auth_token",
    "openai_api_key",
    "anthropic_api_key",
    "deepseek_api_key",
    "gemini_api_key",
    "google_api_key",
    "zcode_api_key",
    "openai_base_url",
    "anthropic_base_url",
    "deepseek_base_url",
    "gemini_base_url",
    "zcode_base_url",
    "base_url",
    "baseurl",
    "api_base",
    "api_base_url",
    "endpoint",
}

_HIGH_FEATURES: list[tuple[str, float]] = [
    ("gpt-6", 20.0),
    ("gpt-5", 18.0),
    ("fable", 18.0),
    ("argon", 14.0),
    ("astra", 12.0),
    ("sol", 10.0),
    ("terra", 8.0),
    ("glm-5", 10.0),
    ("gpt5", 18.0),
    ("o4", 16.0),
    ("o3", 15.0),
    ("opus", 16.0),
    ("ultra", 12.0),
    ("max", 9.0),
    ("pro", 8.0),
    ("405b", 10.0),
    ("235b", 8.0),
    ("reasoner", 10.0),
    ("thinking", 7.0),
    ("deepseek-r1", 9.0),
    ("r1", 6.0),
    ("sonnet", 7.0),
    ("large", 6.0),
    ("70b", 4.0),
]
_LOW_FEATURES: list[tuple[str, float]] = [
    ("nano", -11.0),
    ("mini", -8.0),
    ("haiku", -9.0),
    ("flash", -7.0),
    ("lite", -8.0),
    ("small", -8.0),
    ("instant", -6.0),
    ("fast", -4.0),
    ("turbo", -3.0),
    ("1.5b", -11.0),
    ("3b", -9.0),
    ("7b", -6.0),
    ("8b", -6.0),
    ("preview", -2.0),
    ("beta", -2.0),
    ("experimental", -3.0),
    ("exp", -2.0),
]

_REASONING_FEATURES = ("reasoner", "thinking", "think", "o1", "o3", "o4", "r1", "reasoning")
_CONTEXT_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d{2,3})\s*[kK](?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])(\d(?:\.\d)?)\s*[mM](?![A-Za-z0-9])"
)


class ModelDiscoveryEngine:
    def __init__(
        self,
        config: MeshConfig,
        storage: StateStore,
        workspace_root: str | Path | None = None,
        *,
        home_dir: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
        which: Callable[[str], str | None] | None = None,
        command_runner: Callable[[list[str], Path, int], Awaitable[tuple[int, str, str]]] | None = None,
        module_available: Callable[[str], bool] | None = None,
        cache_ttl_seconds: float | None = None,
        now_fn: Callable[[], float] | None = None,
    ):
        self.config = config
        self.storage = storage
        self.workspace_root = Path(workspace_root or os.getcwd()).expanduser().resolve()
        self.env = dict(env if env is not None else os.environ)
        home_candidate = home_dir or self.env.get("HOME") or Path.home()
        self.home_dir = Path(home_candidate).expanduser().resolve()
        self.http_transport = http_transport
        self.http_client = http_client
        self._which = which or shutil.which
        self._command_runner = command_runner
        self._module_available = module_available or self._default_module_available
        self.cache_ttl_seconds = (
            float(cache_ttl_seconds) if cache_ttl_seconds is not None else float(config.model_cache_ttl_seconds)
        )
        self.now_fn = now_fn or time.time
        self.last_errors: dict[str, list[str]] = {}
        self._role_hints: dict[tuple[str, str], str | None] = {}
        self._score_penalties: dict[tuple[str, str], float] = {}
        self._claude_catalog_models: set[str] = set()
        self._claude_credit_models: set[str] = set()
        self._claude_extra_usage: bool | None = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def discover_for_harness(
        self,
        harness: str,
        workspace_root: str | Path | None = None,
        force_refresh: bool = False,
    ) -> list[DiscoveredModel]:
        harness_cfg = self.config.harnesses.get(harness)
        if harness_cfg is None or not harness_cfg.enabled:
            return []
        root = Path(workspace_root or self.workspace_root).expanduser().resolve()
        self.last_errors.pop(harness, None)

        if not force_refresh and self._cache_is_fresh(harness):
            return self.storage.list_discovered_models(harness)

        found: dict[str, DiscoveredModel] = {}

        try:
            local_models = self._discover_from_local_configs(harness, harness_cfg, root)
        except Exception as exc:  # defensive: one malformed config must not abort discovery
            self._record_error(harness, f"local_config: {exc}")
            local_models = []
        for model in local_models:
            self._merge(found, model)

        try:
            api_models = await self._discover_from_api(harness, harness_cfg, root)
        except Exception as exc:
            self._record_error(harness, f"api_models: {exc}")
            api_models = []
        for model in api_models:
            self._merge(found, model)

        try:
            cli_models = await self._discover_from_cli(harness, harness_cfg, root)
        except Exception as exc:
            self._record_error(harness, f"cli_probe: {exc}")
            cli_models = []
        for model in cli_models:
            self._merge(found, model)

        raw = list(found.values())
        if not raw:
            placeholder = self._active_default_placeholder(harness, harness_cfg)
            if placeholder is not None:
                raw = [placeholder]
        if not raw:
            return self.storage.list_discovered_models(harness)
        self._finalize(harness, raw)
        return self.storage.list_discovered_models(harness)

    async def discover_all(self, force_refresh: bool = False) -> dict[str, list[DiscoveredModel]]:
        result: dict[str, list[DiscoveredModel]] = {}
        for harness in self.config.enabled_harnesses():
            result[harness] = await self.discover_for_harness(harness, force_refresh=force_refresh)
        return result

    async def discover_all_concurrently(self, force_refresh: bool = False) -> dict[str, list[DiscoveredModel]]:
        harnesses = self.config.enabled_harnesses()
        results = await asyncio.gather(
            *(self.discover_for_harness(name, force_refresh=force_refresh) for name in harnesses),
            return_exceptions=True,
        )
        output: dict[str, list[DiscoveredModel]] = {}
        for harness, value in zip(harnesses, results):
            if isinstance(value, BaseException):
                self._record_error(harness, str(value))
                output[harness] = self.storage.list_discovered_models(harness)
            else:
                output[harness] = value
        return output

    def _cache_is_fresh(self, harness: str) -> bool:
        latest = self.storage.latest_discovery_ts(harness)
        if latest <= 0:
            return False
        return (self.now_fn() - latest) < self.cache_ttl_seconds

    def _record_error(self, harness: str, message: str) -> None:
        self.last_errors.setdefault(harness, []).append(message)

    # ------------------------------------------------------------------
    # layer 1: local activation state
    # ------------------------------------------------------------------
    def _discover_from_local_configs(
        self, harness: str, cfg: HarnessConfig, root: Path
    ) -> list[DiscoveredModel]:
        if harness == "claude_code":
            return self._discover_claude(cfg, root)
        if harness == "codex":
            return self._discover_codex(cfg, root)
        if harness == "deepseek":
            return self._discover_deepseek(cfg, root)
        if harness == "antigravity":
            return self._discover_antigravity(cfg, root)
        if harness == "zcode":
            return self._discover_zcode(cfg, root)
        return self._discover_generic(harness, cfg, root)

    def _discover_claude(self, cfg: HarnessConfig, root: Path) -> list[DiscoveredModel]:
        results: list[DiscoveredModel] = []
        self._claude_catalog_models = set()
        self._claude_credit_models = set()
        self._claude_extra_usage = None
        for key in [key for key in self._score_penalties if key[0] == "claude_code"]:
            self._score_penalties.pop(key, None)
        requires_credits: set[str] = set()

        for path in self._config_paths(cfg, root):
            data = self._read_config_file(path)
            if data is None:
                continue
            if "model-catalog" in str(path) and isinstance(data, dict) and "catalog" in data:
                catalog = data.get("catalog") or {}
                state = catalog.get("state") or {}
                active_model = state.get("model")
                for entry in (catalog.get("config") or {}).get("models") or []:
                    if not isinstance(entry, dict):
                        continue
                    model_id = entry.get("id")
                    if not isinstance(model_id, str) or not model_id:
                        continue
                    runtime = entry.get("runtime") or {}
                    family = str(runtime.get("family") or "")
                    role_hint = family if family in {"opus", "sonnet", "haiku", "fable"} else None
                    model = self._make_model(
                        "claude_code",
                        model_id,
                        "local_config",
                        role_hint=role_hint,
                        is_active_default=(model_id == active_model),
                    )
                    if isinstance(runtime.get("max_input_tokens"), int):
                        model.context_window = int(runtime["max_input_tokens"])
                    thinking = entry.get("thinking") or {}
                    if isinstance(thinking, dict) and thinking.get("type") == "effort":
                        model.supports_reasoning = True
                    badge = entry.get("badge") or {}
                    message = str(badge.get("message") or "") if isinstance(badge, dict) else ""
                    if "usage credits" in message.lower():
                        requires_credits.add(model_id)
                    self._claude_catalog_models.add(model_id)
                    results.append(model)
                continue
            if path.name == ".claude.json" and isinstance(data, dict):
                account = data.get("oauthAccount") or {}
                if isinstance(account, dict) and "hasExtraUsageEnabled" in account:
                    self._claude_extra_usage = bool(account.get("hasExtraUsageEnabled"))
            for model_id, role_hint in self._extract_model_entries(data):
                results.append(self._make_model("claude_code", model_id, "local_config", role_hint=role_hint))

        self._claude_credit_models = set(requires_credits)
        if self._claude_extra_usage is False:
            for model in results:
                if model.model_id in requires_credits or "fable" in model.model_id.lower():
                    self._score_penalties[("claude_code", model.model_id)] = -25.0

        for variable in cfg.env_model_variables:
            value = self.env.get(variable)
            if value:
                results.append(
                    self._make_model(
                        "claude_code",
                        value,
                        "env_var",
                        role_hint=self._role_hint(variable),
                    )
                )
        return results

    def _discover_codex(self, cfg: HarnessConfig, root: Path) -> list[DiscoveredModel]:
        results: list[DiscoveredModel] = []
        for path in self._config_paths(cfg, root):
            data = self._read_config_file(path)
            if data is None:
                continue
            provider = "openai"
            if isinstance(data, dict):
                provider = str(data.get("model_provider") or data.get("provider") or "openai")
            for model_id, role_hint in self._extract_model_entries(data):
                results.append(
                    self._make_model("codex", model_id, "local_config", provider=provider, role_hint=role_hint)
                )
        for variable in cfg.env_model_variables:
            value = self.env.get(variable)
            if value:
                results.append(
                    self._make_model("codex", value, "env_var", role_hint=self._role_hint(variable))
                )
        return results

    def _discover_deepseek(self, cfg: HarnessConfig, root: Path) -> list[DiscoveredModel]:
        results: list[DiscoveredModel] = []
        for path in self._deepseek_config_paths(cfg, root):
            data = self._read_config_file(path)
            if data is None:
                continue
            for model_id, role_hint in self._extract_model_entries(data):
                results.append(
                    self._make_model("deepseek", model_id, "local_config", provider="deepseek", role_hint=role_hint)
                )
        for variable in cfg.env_model_variables:
            value = self.env.get(variable)
            if value:
                results.append(
                    self._make_model(
                        "deepseek",
                        value,
                        "env_var",
                        provider="deepseek",
                        role_hint=self._role_hint(variable),
                    )
                )
        return results

    def _discover_antigravity(self, cfg: HarnessConfig, root: Path) -> list[DiscoveredModel]:
        results: list[DiscoveredModel] = []
        for path in self._config_paths(cfg, root):
            data = self._read_config_file(path)
            if data is None:
                continue
            for model_id, role_hint in self._extract_model_entries(data):
                results.append(
                    self._make_model("antigravity", model_id, "local_config", provider="google", role_hint=role_hint)
                )
        for variable in cfg.env_model_variables:
            value = self.env.get(variable)
            if value:
                results.append(
                    self._make_model(
                        "antigravity",
                        value,
                        "env_var",
                        provider="google",
                        role_hint=self._role_hint(variable),
                    )
                )
        return results

    def _discover_zcode(self, cfg: HarnessConfig, root: Path) -> list[DiscoveredModel]:
        results: list[DiscoveredModel] = []
        for path in self._config_paths(cfg, root):
            data = self._read_config_file(path)
            if data is None:
                continue
            provider = "zcode"
            if isinstance(data, dict):
                provider = str(data.get("provider") or "zcode")
            for model_id, role_hint in self._extract_model_entries(data):
                results.append(
                    self._make_model("zcode", model_id, "local_config", provider=provider, role_hint=role_hint)
                )
        for variable in cfg.env_model_variables:
            value = self.env.get(variable)
            if value:
                results.append(self._make_model("zcode", value, "env_var", role_hint=self._role_hint(variable)))
        return results

    def _discover_generic(self, harness: str, cfg: HarnessConfig, root: Path) -> list[DiscoveredModel]:
        results: list[DiscoveredModel] = []
        for path in self._config_paths(cfg, root):
            data = self._read_config_file(path)
            if data is None:
                continue
            for model_id, role_hint in self._extract_model_entries(data):
                results.append(self._make_model(harness, model_id, "local_config", role_hint=role_hint))
        for variable in cfg.env_model_variables:
            value = self.env.get(variable)
            if value:
                results.append(self._make_model(harness, value, "env_var", role_hint=self._role_hint(variable)))
        return results

    def _config_paths(self, cfg: HarnessConfig, root: Path) -> list[Path]:
        paths: list[Path] = []
        expanded_globs: list[Path] = []
        for template in cfg.config_files:
            rendered = str(self._render_path(template, root))
            if not rendered:
                continue
            if any(char in rendered for char in ("*", "?", "[")):
                try:
                    matches = [Path(item) for item in glob.glob(rendered, recursive=True)]
                except OSError:
                    matches = []
                matches.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0.0, reverse=True)
                for match in matches:
                    if match.is_file() and match not in expanded_globs:
                        expanded_globs.append(match)
            else:
                candidate = Path(rendered)
                if candidate not in paths:
                    paths.append(candidate)
        for candidate in expanded_globs:
            if candidate not in paths:
                paths.append(candidate)
        return paths

    def _deepseek_config_paths(self, cfg: HarnessConfig, root: Path) -> list[Path]:
        paths = self._config_paths(cfg, root)
        dsh_home = self._dsh_home()
        profiles_dir = dsh_home / "profiles"
        if profiles_dir.is_dir():
            for profile in sorted(profiles_dir.iterdir()):
                if not profile.is_dir():
                    continue
                for name in ("cordis.patch.yml", "cordis.patch.yaml", "config.yml", "config.yaml"):
                    candidate = profile / name
                    if candidate.exists() and candidate not in paths:
                        paths.append(candidate)
        for candidate in (self.home_dir / ".deepseek" / "config.json", root / ".deepseek.yaml", root / ".deepseek.yml"):
            if candidate.exists() and candidate not in paths:
                paths.append(candidate)
        return paths

    def _render_path(self, template: str, root: Path) -> Path:
        dsh_home = self._dsh_home()
        try:
            rendered = template.format(workspace=str(root), dsh_home=str(dsh_home), home=str(self.home_dir))
        except (KeyError, ValueError):
            rendered = template
        if rendered.startswith("~"):
            rendered = str(self.home_dir) + rendered[1:]
        path = Path(rendered)
        if not path.is_absolute():
            path = root / path
        return path

    def _dsh_home(self) -> Path:
        value = self.env.get("DSH_HOME")
        if value:
            return Path(value).expanduser().resolve()
        return self.home_dir / ".dsh"

    def _read_config_file(self, path: Path) -> Any:
        if not path.exists() or not path.is_file():
            return None
        try:
            if path.suffix == ".toml":
                with path.open("rb") as handle:
                    return tomllib.load(handle)
            text = path.read_text(encoding="utf-8", errors="replace")
            if not text.strip():
                return None
            if path.suffix == ".json":
                return json.loads(text)
            if path.suffix in {".yaml", ".yml"}:
                return yaml.safe_load(text)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return yaml.safe_load(text)
        except (OSError, ValueError, yaml.YAMLError, tomllib.TOMLDecodeError) as exc:
            self._record_error("_config", f"{path}: {exc}")
            return None

    def _extract_model_entries(self, data: Any) -> list[tuple[str, str | None]]:
        entries: list[tuple[str, str | None]] = []

        def visit(node: Any, key_context: str = "") -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    normalized = str(key).lower().replace("-", "_")
                    role_hint = self._role_hint(normalized)
                    if normalized in _MODEL_KEY_HINTS or normalized.endswith(_MODEL_SUFFIXES):
                        keys_are_models = normalized in {
                            "models",
                            "model_list",
                            "model_catalog",
                            "modellist",
                            "modelcatalog",
                        }
                        for model_id in self._coerce_models(value, dict_keys_are_models=keys_are_models):
                            entries.append((model_id, role_hint))
                        # A model mapping may also hide nested metadata blocks.
                        if normalized not in {"models", "model_list", "model_catalog", "modellist", "modelcatalog"}:
                            visit(value, normalized)
                    else:
                        visit(value, normalized)
            elif isinstance(node, list):
                for item in node:
                    visit(item, key_context)

        visit(data)
        seen: set[str] = set()
        unique: list[tuple[str, str | None]] = []
        for model_id, role_hint in entries:
            key = model_id.strip()
            if key and key not in seen:
                seen.add(key)
                unique.append((key, role_hint))
        return unique

    def _coerce_models(self, value: Any, *, dict_keys_are_models: bool = False) -> list[str]:
        models: list[str] = []
        if isinstance(value, str):
            candidate = value.strip()
            if self._looks_like_model_id(candidate):
                models.append(candidate)
        elif isinstance(value, (int, float)):
            return []
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and self._looks_like_model_id(item):
                    models.append(item.strip())
                elif isinstance(item, dict):
                    for key in ("id", "model", "model_id", "name", "slug", "value"):
                        if isinstance(item.get(key), str) and self._looks_like_model_id(item[key]):
                            models.append(item[key].strip())
                            break
        elif isinstance(value, dict):
            if dict_keys_are_models:
                for key in value:
                    if isinstance(key, str) and self._looks_like_model_id(key):
                        models.append(key.strip())
            else:
                for key in ("id", "model", "model_id", "name", "slug", "value"):
                    if isinstance(value.get(key), str) and self._looks_like_model_id(value[key]):
                        models.append(value[key].strip())
                        break
        return models

    @staticmethod
    def _looks_like_model_id(value: str) -> bool:
        if not value or len(value) > 200:
            return False
        if value.startswith(("{", "$", "/", "./", "../", "http://", "https://")):
            return False
        if any(char in value for char in ("\n", "\t", " ", "=")):
            return False
        if value.lower() in {"true", "false", "none", "null", "default", "latest", "auto"}:
            return False
        return bool(re.search(r"[A-Za-z]", value))

    @staticmethod
    def _role_hint(key: str) -> str | None:
        normalized = key.lower().replace("-", "_")
        if "opus" in normalized:
            return "opus"
        if "sonnet" in normalized:
            return "sonnet"
        if "haiku" in normalized:
            return "haiku"
        if "small" in normalized or "fast" in normalized:
            return "small"
        if "reason" in normalized or "think" in normalized:
            return "reasoning"
        return None

    def _make_model(
        self,
        harness: str,
        model_id: str,
        source: str,
        *,
        provider: str = "default",
        role_hint: str | None = None,
        is_active_default: bool = False,
    ) -> DiscoveredModel:
        model_id = model_id.strip()
        if role_hint:
            self._role_hints[(harness, model_id)] = role_hint
        return DiscoveredModel(
            harness=harness,
            model_id=model_id,
            provider=provider,
            source=source,
            is_active_default=is_active_default or model_id == PLACEHOLDER_MODEL,
            discovered_at=self.now_fn(),
            capability_score=self.score_model(model_id, role_hint=role_hint),
            supports_reasoning=self.supports_reasoning(model_id, role_hint=role_hint),
            context_window=self.parse_context_window(model_id),
        )

    def _merge(self, found: dict[str, DiscoveredModel], model: DiscoveredModel) -> None:
        existing = found.get(model.model_id)
        if existing is None:
            found[model.model_id] = model
            return
        if SOURCE_PRIORITY.get(model.source, 0) > SOURCE_PRIORITY.get(existing.source, 0):
            existing.source = model.source
        if model.provider != "default":
            existing.provider = model.provider
        if model.is_active_default:
            existing.is_active_default = True
        if existing.context_window is None and model.context_window is not None:
            existing.context_window = model.context_window
        if model.supports_reasoning:
            existing.supports_reasoning = True

    # ------------------------------------------------------------------
    # layer 2: live model endpoints
    # ------------------------------------------------------------------
    def _resolve_auth(self, harness: str, cfg: HarnessConfig, root: Path) -> dict[str, str | None]:
        base_url = self._first_env(cfg.base_url_env)
        api_key = self._first_env(cfg.api_key_env)
        config_values = self._collect_config_auth(cfg, root)
        if not base_url:
            base_url = config_values.get("base_url")
        if not api_key:
            api_key = config_values.get("api_key")
        if not base_url:
            base_url = cfg.default_base_url or None
        if cfg.auth_style == "gemini":
            if not api_key:
                api_key = self.env.get("GOOGLE_API_KEY") or self.env.get("GEMINI_API_KEY")
        return {"base_url": base_url, "api_key": api_key}

    def _first_env(self, names: Iterable[str]) -> str | None:
        for name in names:
            value = self.env.get(name)
            if value:
                return value
        return None

    def _collect_config_auth(self, cfg: HarnessConfig, root: Path) -> dict[str, str | None]:
        values: dict[str, str | None] = {"base_url": None, "api_key": None}
        for path in self._config_paths(cfg, root):
            data = self._read_config_file(path)
            if data is None:
                continue
            self._walk_auth(data, values)
            if values.get("api_key") and values.get("base_url"):
                break
        return values

    def _walk_auth(self, node: Any, values: dict[str, str | None]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                normalized = str(key).lower().replace("-", "_")
                if isinstance(value, str) and value.strip():
                    if values.get("api_key") is None and (
                        normalized in _AUTH_KEY_HINTS
                        and ("key" in normalized or "token" in normalized)
                    ):
                        values["api_key"] = value.strip()
                    if values.get("base_url") is None and (
                        normalized in {"base_url", "baseurl", "api_base", "api_base_url", "endpoint"}
                        or normalized.endswith("_base_url")
                    ):
                        if value.startswith(("http://", "https://")):
                            values["base_url"] = value.strip()
                else:
                    self._walk_auth(value, values)
        elif isinstance(node, list):
            for item in node:
                self._walk_auth(item, values)

    async def _discover_from_api(
        self, harness: str, cfg: HarnessConfig, root: Path
    ) -> list[DiscoveredModel]:
        auth = self._resolve_auth(harness, cfg, root)
        base_url = auth.get("base_url")
        api_key = auth.get("api_key")
        if not base_url or not api_key:
            return []
        endpoint = cfg.models_endpoint or "/v1/models"
        url = self._join_endpoint(base_url, endpoint)
        headers: dict[str, str] = {}
        params: dict[str, str] = {}
        if cfg.auth_style == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        elif cfg.auth_style == "gemini":
            headers["x-goog-api-key"] = api_key
            params["key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

        created_client = False
        client = self.http_client
        if client is None:
            client = httpx.AsyncClient(
                transport=self.http_transport,
                timeout=httpx.Timeout(12.0),
                follow_redirects=True,
            )
            created_client = True
        try:
            response = await client.get(url, headers=headers, params=params)
            if response.status_code >= 400:
                self._record_error(harness, f"api_models HTTP {response.status_code}: {response.text[:300]}")
                return []
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._record_error(harness, f"api_models: {exc}")
            return []
        finally:
            if created_client:
                await client.aclose()

        model_ids = self.parse_models_payload(payload, cfg.models_response_shape)
        return [
            self._make_model(
                harness,
                model_id,
                "api_models",
                provider=self._infer_provider(harness, model_id),
                role_hint=self._role_hint(model_id),
            )
            for model_id in model_ids
        ]

    @staticmethod
    def _join_endpoint(base_url: str, endpoint: str) -> str:
        base = base_url.rstrip("/")
        if endpoint.startswith(("http://", "https://")):
            return endpoint
        path = endpoint.lstrip("/")
        for version in ("v1beta", "v1alpha", "v1"):
            if base.endswith("/" + version) and path.startswith(version + "/"):
                path = path[len(version) + 1 :]
                break
        return f"{base}/{path}"

    @staticmethod
    def parse_models_payload(payload: Any, shape: str = "openai") -> list[str]:
        models: list[str] = []
        if shape == "gemini":
            entries = payload.get("models", []) if isinstance(payload, dict) else []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                methods = entry.get("supportedGenerationMethods") or entry.get("supported_generation_methods")
                if methods and "generateContent" not in methods:
                    continue
                name = entry.get("name") or entry.get("model")
                if isinstance(name, str) and name:
                    models.append(name.split("/", 1)[1] if name.startswith("models/") else name)
            return list(dict.fromkeys(models))

        entries: Any
        if isinstance(payload, dict):
            entries = payload.get("data") or payload.get("models") or payload.get("model_list") or []
        elif isinstance(payload, list):
            entries = payload
        else:
            entries = []
        if isinstance(entries, dict):
            entries = list(entries.keys())
        for entry in entries or []:
            if isinstance(entry, str):
                models.append(entry)
            elif isinstance(entry, dict):
                name = entry.get("id") or entry.get("model") or entry.get("name") or entry.get("model_id")
                if isinstance(name, str) and name:
                    models.append(name)
        return list(dict.fromkeys(item for item in models if item))

    @staticmethod
    def _infer_provider(harness: str, model_id: str) -> str:
        if "/" in model_id:
            return model_id.split("/", 1)[0]
        return {
            "claude_code": "anthropic",
            "codex": "openai",
            "deepseek": "deepseek",
            "antigravity": "google",
            "zcode": "zcode",
        }.get(harness, "default")

    # ------------------------------------------------------------------
    # layer 3: CLI enumeration and active default fallback
    # ------------------------------------------------------------------
    async def _discover_from_cli(
        self, harness: str, cfg: HarnessConfig, root: Path
    ) -> list[DiscoveredModel]:
        binary = self._resolve_binary(cfg)
        if not binary:
            return []
        results: list[DiscoveredModel] = []
        if cfg.model_list_args:
            code, stdout, stderr = await self._run_command(
                [binary, *cfg.model_list_args], root, timeout=15
            )
            if code == 0 and stdout.strip():
                results.extend(self._parse_cli_models(harness, stdout))
        return results

    @staticmethod
    def _default_module_available(name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            return False

    def _active_default_placeholder(
        self, harness: str, cfg: HarnessConfig
    ) -> DiscoveredModel | None:
        """Return ``<cli_active_default>`` only for an otherwise empty inventory.

        A CLI binary or an importable SDK can hide its active model (OAuth
        activation, SDK-managed routing).  The placeholder is only synthesized
        after local config, API and CLI enumeration have all produced nothing,
        so it never pollutes a real inventory.
        """

        if self._resolve_binary(cfg) is not None:
            return self._make_model(
                harness,
                PLACEHOLDER_MODEL,
                "cli_active_default",
                provider=harness,
                is_active_default=True,
            )
        sdk_module = cfg.extra.get("sdk_module")
        if cfg.extra.get("sdk_enabled", True) and sdk_module:
            if self._module_available(str(sdk_module)):
                return self._make_model(
                    harness,
                    PLACEHOLDER_MODEL,
                    "cli_active_default",
                    provider=harness,
                    is_active_default=True,
                )
        return None

    def _resolve_binary(self, cfg: HarnessConfig) -> str | None:
        return resolve_harness_binary(
            cfg,
            self._which,
            self.config.state_path(self.workspace_root),
            self.home_dir,
            allow_system_paths=(self._which is shutil.which),
        )

    def _parse_cli_models(self, harness: str, stdout: str) -> list[DiscoveredModel]:
        text = stdout.strip()
        payload: Any = None
        if text.startswith(("{", "[")):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
        results: list[DiscoveredModel] = []
        if payload is not None:
            for model_id, role_hint in self._extract_model_entries(payload):
                results.append(self._make_model(harness, model_id, "cli_probe", role_hint=role_hint))
            if results:
                return results
        for raw_line in text.split("\n"):
            # ``agy models`` redraws a spinner with carriage returns.
            cleaned = raw_line.rsplit("\r", 1)[-1].strip()
            lowered = cleaned.lower()
            if not cleaned:
                continue
            if lowered.startswith(("available", "models", "name", "---", "fetching", "loading", "checking", "using")):
                continue
            token = cleaned.split()[0].rstrip(",;:")
            if self._looks_like_model_id(token):
                results.append(self._make_model(harness, token, "cli_probe", role_hint=self._role_hint(token)))
        return results

    async def _run_command(self, args: list[str], cwd: Path, timeout: int = 15) -> tuple[int, str, str]:
        if self._command_runner is not None:
            return await self._command_runner(args, cwd, timeout)
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            return 127, "", str(exc)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return 124, "", "CLI model probe timed out"
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    # ------------------------------------------------------------------
    # scoring and tiering
    # ------------------------------------------------------------------
    def score_model(self, model_id: str, role_hint: str | None = None) -> float:
        text = model_id.lower()
        score = 20.0
        if "<cli_active_default>" in text:
            return 20.0
        if role_hint == "opus":
            score += 16.0
        elif role_hint == "sonnet":
            score += 8.0
        elif role_hint in {"haiku", "small"}:
            score -= 8.0
        elif role_hint == "reasoning":
            score += 6.0
        elif role_hint == "fable":
            score += 12.0
        applied: set[str] = set()
        for feature, delta in _HIGH_FEATURES:
            if feature in text and feature not in applied:
                score += delta
                applied.add(feature)
        for feature, delta in _LOW_FEATURES:
            if feature in text and feature not in applied:
                score += delta
                applied.add(feature)
        # Prefer newer generations within a known family.  Claude-style ids
        # encode the version as ``opus-5-5`` (5.5), where a naive max-number
        # scan would wrongly rank ``opus-4-8`` above it.
        family_version = re.search(
            r"(?:opus|sonnet|haiku|fable)[-_](\d+)(?:[.-](\d+))?", text
        )
        if family_version:
            major = int(family_version.group(1))
            minor = int(family_version.group(2) or 0)
            score += min(major + minor / 10.0, 6.0) * 0.6
        else:
            versions = [
                float(item)
                for item in re.findall(r"(?<![A-Za-z])(\d+(?:\.\d+)?)(?![A-Za-z0-9])", text)
            ]
            if versions:
                score += min(max(versions), 6.0) * 0.6
        return round(max(1.0, min(100.0, score)), 2)

    @staticmethod
    def supports_reasoning(model_id: str, role_hint: str | None = None) -> bool:
        text = model_id.lower()
        if role_hint == "reasoning":
            return True
        return any(feature in text for feature in _REASONING_FEATURES)

    @staticmethod
    def parse_context_window(model_id: str) -> int | None:
        match = _CONTEXT_RE.search(model_id)
        if not match:
            return None
        if match.group(1):
            return int(match.group(1)) * 1000
        if match.group(2):
            return int(float(match.group(2)) * 1_000_000)
        return None

    def classify_and_assign_tiers(self, models: Iterable[DiscoveredModel]) -> dict[str, list[str]]:
        """Rank authorised models and bind them to the four difficulty tiers."""

        model_list = list(models)
        for model in model_list:
            model.assigned_tiers = []
        authorized = [model for model in model_list if model.authorized]
        if not authorized:
            return {}
        ranked = sorted(
            authorized,
            key=lambda item: (item.capability_score, len(item.model_id)),
            reverse=True,
        )
        count = len(ranked)
        assignment: dict[str, list[str]] = {model.model_id: [] for model in ranked}

        if count == 1:
            assignment[ranked[0].model_id] = list(TIER_NAMES)
        elif count == 2:
            assignment[ranked[0].model_id] = ["high", "critical"]
            assignment[ranked[1].model_id] = ["low", "medium"]
        elif count == 3:
            assignment[ranked[0].model_id] = ["critical", "high"]
            assignment[ranked[1].model_id] = ["medium"]
            assignment[ranked[2].model_id] = ["low"]
        else:
            for index, model in enumerate(ranked):
                percentile = index / (count - 1)
                if percentile <= 0.25:
                    assignment[model.model_id] = ["critical"]
                elif percentile <= 0.5:
                    assignment[model.model_id] = ["high"]
                elif percentile <= 0.75:
                    assignment[model.model_id] = ["medium"]
                else:
                    assignment[model.model_id] = ["low"]
            # Guarantee every tier has at least one binding.
            present = {tier for tiers in assignment.values() for tier in tiers}
            for tier in TIER_NAMES:
                if tier not in present:
                    assignment[ranked[0].model_id].append(tier)

        for model in ranked:
            model.assigned_tiers = list(assignment.get(model.model_id, []))
        for model in model_list:
            if not model.authorized:
                model.assigned_tiers = []
        return assignment

    def _finalize(self, harness: str, models: list[DiscoveredModel]) -> None:
        existing = {item.model_id: item for item in self.storage.list_discovered_models(harness)}
        for model in models:
            previous = existing.get(model.model_id)
            if previous is not None and not previous.authorized:
                model.authorized = False
            model.capability_score = round(
                max(
                    1.0,
                    self.score_model(
                        model.model_id, role_hint=self._role_hints.get((harness, model.model_id))
                    )
                    + self._score_penalties.get((harness, model.model_id), 0.0),
                ),
                2,
            )
            model.supports_reasoning = model.supports_reasoning or self.supports_reasoning(
                model.model_id, role_hint=self._role_hints.get((harness, model.model_id))
            )
            if model.context_window is None:
                model.context_window = self.parse_context_window(model.model_id)
            if model.discovered_at <= 0:
                model.discovered_at = self.now_fn()
        tiers = self.classify_and_assign_tiers(models)
        self._apply_claude_subscription_tiers(harness, models)
        tiers = {
            model.model_id: list(model.assigned_tiers)
            for model in models
            if model.authorized
        }
        self.storage.save_discovered_models(models, preserve_unauthorized=True)
        self.storage.set_model_tiers(harness, tiers)

    def _apply_claude_subscription_tiers(self, harness: str, models: list[DiscoveredModel]) -> None:
        """Keep pure-subscription Claude tickets on the intended price tiers.

        The catalog can contain many models where some variants require extra
        usage credits.  In a pure subscription the strongest subscription
        included Opus-class model owns critical/high, the active default
        Sonnet-class model owns medium, and the strongest Haiku-class model
        owns low.  Family metadata comes from the catalog; vendor model ids are
        not hard-coded here.
        """

        if harness != "claude_code" or self._claude_extra_usage is not False:
            return
        authorized = [model for model in models if model.authorized]
        if not authorized:
            return

        def family(model: DiscoveredModel) -> str | None:
            return self._role_hints.get((harness, model.model_id))

        def eligible(candidates: list[DiscoveredModel]) -> list[DiscoveredModel]:
            return [
                model
                for model in candidates
                if model.model_id not in self._claude_credit_models
                and "fable" not in model.model_id.lower()
            ]

        opus = eligible([model for model in authorized if family(model) == "opus"])
        sonnet = eligible([model for model in authorized if family(model) == "sonnet"])
        haiku = eligible([model for model in authorized if family(model) == "haiku"])
        if not (opus and sonnet and haiku):
            return
        ranked_opus = sorted(opus, key=lambda item: item.capability_score, reverse=True)
        ranked_sonnet = sorted(sonnet, key=lambda item: item.capability_score, reverse=True)
        ranked_haiku = sorted(haiku, key=lambda item: item.capability_score, reverse=True)
        active_sonnet = next((model for model in ranked_sonnet if model.is_active_default), ranked_sonnet[0])
        targets = {
            ranked_opus[0].model_id: ["critical", "high"],
            active_sonnet.model_id: ["medium"],
            ranked_haiku[0].model_id: ["low"],
        }
        for model in models:
            model.assigned_tiers = []
        by_id = {model.model_id: model for model in authorized}
        for model_id, tiers in targets.items():
            by_id[model_id].assigned_tiers = list(tiers)

    def tier_models_for(self, models: Iterable[DiscoveredModel]) -> dict[str, str]:
        model_list = list(models)
        authorized = [model for model in model_list if model.authorized]
        if not authorized:
            return {}
        if not any(model.assigned_tiers for model in authorized):
            self.classify_and_assign_tiers(model_list)
        ranked = sorted(authorized, key=lambda item: (item.capability_score, len(item.model_id)), reverse=True)
        mapping: dict[str, str] = {}
        for tier in TIER_NAMES:
            for model in ranked:
                if tier in model.assigned_tiers:
                    mapping[tier] = model.model_id
                    break
        for tier in TIER_NAMES:
            mapping.setdefault(tier, ranked[0].model_id)
        return mapping

    def get_tier_models(self, harness: str) -> dict[str, str]:
        models = self.storage.list_discovered_models(harness)
        if not models:
            return {}
        mapping = self.tier_models_for(models)
        # Persist freshly derived tiers so list_harnesses and MCP clients agree.
        tier_assignment = {model.model_id: list(model.assigned_tiers) for model in models}
        self.storage.set_model_tiers(harness, tier_assignment)
        return mapping

    def authorized_models(self, harness: str) -> list[DiscoveredModel]:
        return self.storage.list_discovered_models(harness, authorized_only=True)

    def resolve_model(self, harness: str, difficulty: str, *, exclude: Iterable[str] = ()) -> DiscoveredModel | None:
        models = self.authorized_models(harness)
        if not models:
            return None
        excluded = set(exclude)
        candidates = [model for model in models if model.model_id not in excluded]
        if not candidates:
            return None
        mapping = self.tier_models_for(models)
        preferred_id = mapping.get(str(difficulty))
        for model in candidates:
            if model.model_id == preferred_id:
                return model
        ranked = sorted(candidates, key=lambda item: item.capability_score, reverse=True)
        return ranked[0]

    def candidates_for(
        self, harness: str, difficulty: str, *, exclude: Iterable[str] = ()
    ) -> list[DiscoveredModel]:
        models = self.authorized_models(harness)
        excluded = set(exclude)
        models = [model for model in models if model.model_id not in excluded]
        if not models:
            return []
        mapping = self.tier_models_for(models)
        target_id = mapping.get(str(difficulty))
        ranked = sorted(models, key=lambda item: item.capability_score, reverse=True)
        if target_id:
            ranked.sort(key=lambda item: item.model_id != target_id)
        return ranked

    def mark_model_unauthorized(self, harness: str, model_id: str) -> dict[str, list[str]]:
        self.storage.mark_model_unauthorized(harness, model_id)
        models = self.storage.list_discovered_models(harness)
        tiers = self.classify_and_assign_tiers(models)
        self.storage.set_model_tiers(harness, tiers)
        return tiers

    def has_cli(self, harness: str) -> bool:
        cfg = self.config.harnesses.get(harness)
        if cfg is None:
            return False
        return self._resolve_binary(cfg) is not None

    def binary_path(self, harness: str) -> str | None:
        cfg = self.config.harnesses.get(harness)
        if cfg is None:
            return None
        return self._resolve_binary(cfg)


__all__ = [
    "ModelDiscoveryEngine",
    "PLACEHOLDER_MODEL",
    "TIER_NAMES",
]
