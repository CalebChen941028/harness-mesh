"""Read-only model market: list prices, evaluation scores and offline snapshots.

Three public sources are supported, each behind the same narrow HTTP client:

* OpenRouter's ``/api/v1/models`` catalogue (per-token prices).
* models.dev's ``/api.json`` catalogue (per-million-token costs).
* Artificial Analysis' free language-model endpoint (intelligence index).

The module never sends prompts, file paths or credentials to a source.  Only
the Artificial Analysis key is transmitted, and only in the ``x-api-key``
header of a request to ``artificialanalysis.ai``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import math
import os
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .config import MarketConfig, MarketOverrideConfig
from .storage import StateStore

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = frozenset({"openrouter.ai", "models.dev", "artificialanalysis.ai"})
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_REDIRECTS = 3
MAX_TEXT_LENGTH = 300

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
MODELSDEV_URL = "https://models.dev/api.json"
# The documented Pro endpoint returns 403 for Free keys.  The ``/free`` sibling
# accepts every key tier and carries the headline indices we need (see
# https://artificialanalysis.ai/api/v2/openapi, LLMModelsFreeResponse).
ARTIFICIAL_ANALYSIS_URL = "https://artificialanalysis.ai/api/v2/language/models/free"
ARTIFICIAL_ANALYSIS_MIN_TTL = 43_200

SOURCE_ORDER = ("openrouter", "models_dev", "artificial_analysis")
_SOURCE_PRIORITY = {name: index for index, name in enumerate(SOURCE_ORDER)}
_INTELLIGENCE_SOURCES = {"artificial_analysis", "heuristic", "override"}
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BRACKETS = re.compile(r"\[[^\]]*\]")
_DATE_SUFFIX_8 = re.compile(r"-\d{8}$")
_DATE_SUFFIX_4 = re.compile(r"-\d{4}$")


class MarketError(RuntimeError):
    """A market source could not be read safely or parsed."""


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


class MarketModel(BaseModel):
    """One canonical model entry merged from the available market sources."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    key: str
    vendor: str = ""
    name: str = ""
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None
    cache_read_per_mtok: float | None = None
    context_window: int | None = None
    reasoning: bool = False
    tool_call: bool = False
    release_date: str | None = None
    intelligence: float | None = None
    intelligence_source: str | None = None
    speed_tps: float | None = None
    sources: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    fetched_at: float = Field(default_factory=time.time)


class MarketCatalog(BaseModel):
    """The merged, read-only view used by routing and the CLI."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    models: list[MarketModel] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    fetched_at: float | None = None
    stale: bool = False
    expired: bool = False
    enabled: bool = True
    warnings: list[str] = Field(default_factory=list)

    def get(self, key: str) -> MarketModel | None:
        for model in self.models:
            if model.key == key:
                return model
        return None

    def keys(self) -> list[str]:
        return [model.key for model in self.models]


class MarketSourceStatus(BaseModel):
    source: str
    status: str
    models: int = 0
    fetched_at: float | None = None
    error: str | None = None


class MarketRefreshResult(BaseModel):
    statuses: list[MarketSourceStatus] = Field(default_factory=list)
    catalog: MarketCatalog = Field(default_factory=MarketCatalog)


class ModelMatch(BaseModel):
    """Result of mapping one harness model id onto the market catalogue."""

    key: str | None = None
    confidence: str = "none"  # exact | normalized | alias | none
    effort_variant: str | None = None
    matched_name: str | None = None


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _text(value: Any, limit: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _price_per_mtok(value: Any, *, per_token: bool) -> float | None:
    number = _finite_number(value)
    if number is None:
        return None
    return round(number * 1_000_000, 9) if per_token else number


def _bool(value: Any) -> bool:
    return value is True


def _iso_date(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = _text(value, 32)
    if _ISO_DATE.match(text):
        try:
            date.fromisoformat(text)
        except ValueError:
            return None
        return text
    return None


def _vendor_slug(vendor: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", vendor.lower())


def _normalize_name(name: str) -> str:
    text = _text(name, 200).lower()
    text = _BRACKETS.sub("", text)
    text = text.replace(".", "-")
    text = _DATE_SUFFIX_8.sub("", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-_ ")


def canonical_model_key(vendor: str, name: str) -> str:
    """Return the stable merge key for a vendor/model pair."""

    vendor_part = _vendor_slug(vendor) or "unknown"
    name_part = _normalize_name(name) or "unknown"
    return f"{vendor_part}/{name_part}"


def _canonical_source(name: str) -> str:
    text = _text(name, 80).lower().replace(".", "_").replace("-", "_")
    return {"modelsdev": "models_dev"}.get(text, text)


def _sources_for(model: MarketModel) -> list[str]:
    present = {_canonical_source(source) for source in model.sources}
    return [source for source in SOURCE_ORDER if source in present]


def _priority(model: MarketModel) -> int:
    sources = _sources_for(model)
    return min((_SOURCE_PRIORITY[source] for source in sources), default=len(SOURCE_ORDER))


def _add_unique(values: list[str], additions: Iterable[str]) -> list[str]:
    for item in additions:
        text = _text(item, MAX_TEXT_LENGTH)
        if text and text not in values:
            values.append(text)
    return values


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _allowed_target(url: httpx.URL) -> bool:
    return url.scheme == "https" and url.host in ALLOWED_HOSTS


def _redirect_target(current: httpx.URL, location: str) -> httpx.URL:
    try:
        target = current.join(location)
    except Exception as exc:  # malformed Location header
        raise MarketError(f"invalid redirect location from {current.host}: {exc}") from exc
    if not _allowed_target(target) or target.host != current.host:
        raise MarketError(
            f"refusing redirect from {current.host} to {target.host or 'unknown host'}: "
            "a market redirect must stay on the same allowlisted host"
        )
    return target


async def fetch_json(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: Mapping[str, str] | None = None,
    max_bytes: int | None = None,
) -> Any:
    """HTTPS GET a JSON document from an allowlisted host with hard limits."""

    current = httpx.URL(str(url))
    if not _allowed_target(current):
        raise MarketError(
            f"refusing market request to {current.scheme}://{current.host or 'unknown'}: "
            "only HTTPS requests to the allowlisted hosts are allowed"
        )
    limit = MAX_RESPONSE_BYTES if max_bytes is None else max_bytes
    request_headers = {
        "User-Agent": f"harness-mesh/{__version__}",
        "Accept": "application/json",
        **(dict(headers) if headers else {}),
    }
    async with httpx.AsyncClient(
        transport=transport,
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers=request_headers,
    ) as client:
        for _redirect in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise MarketError(f"{current.host} returned a redirect without a location")
                    current = _redirect_target(current, location)
                    continue
                if response.status_code != 200:
                    raise MarketError(f"{current.host} returned HTTP {response.status_code}")
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > limit:
                            raise MarketError(
                                f"response from {current.host} exceeds the {limit}-byte limit"
                            )
                    except ValueError:
                        pass
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise MarketError(
                            f"response from {current.host} exceeds the {limit}-byte limit"
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
                try:
                    return json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise MarketError(f"{current.host} returned invalid JSON: {exc}") from exc
        raise MarketError(f"too many redirects while fetching {url}")


# ---------------------------------------------------------------------------
# provider base
# ---------------------------------------------------------------------------


class MarketProvider:
    """One independent market source.  ``fetch`` never receives user data."""

    name = "provider"

    async def fetch(self) -> list[MarketModel]:  # pragma: no cover - interface
        raise NotImplementedError


class OpenRouterProvider(MarketProvider):
    name = "openrouter"

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    async def fetch(self) -> list[MarketModel]:
        payload = await fetch_json(OPENROUTER_URL, transport=self.transport)
        return parse_openrouter(payload)


class ModelsDevProvider(MarketProvider):
    name = "models_dev"

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    async def fetch(self) -> list[MarketModel]:
        payload = await fetch_json(MODELSDEV_URL, transport=self.transport)
        return parse_models_dev(payload)


class ArtificialAnalysisProvider(MarketProvider):
    name = "artificial_analysis"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        env: Mapping[str, str] | None = None,
        key_env: str = "ARTIFICIAL_ANALYSIS_API_KEY",
        transport: httpx.AsyncBaseTransport | None = None,
        max_pages: int = 5,
    ):
        environment = env if env is not None else os.environ
        self.api_key = (api_key or environment.get(key_env) or "").strip()
        self.transport = transport
        self.max_pages = max(1, int(max_pages))
        self.request_budget = self.max_pages
        self.last_page_count = 0

    async def fetch(self) -> list[MarketModel]:
        self.last_page_count = 0
        if not self.api_key:
            return []
        models: list[MarketModel] = []
        page = 1
        while page <= self.max_pages and page <= self.request_budget:
            url = ARTIFICIAL_ANALYSIS_URL if page == 1 else f"{ARTIFICIAL_ANALYSIS_URL}?page={page}"
            payload = await fetch_json(
                url, transport=self.transport, headers={"x-api-key": self.api_key}
            )
            self.last_page_count += 1
            parsed, pagination = parse_artificial_analysis_page(payload)
            models.extend(parsed)
            has_more = bool(pagination.get("has_more"))
            total_pages = pagination.get("total_pages")
            if not has_more:
                break
            if isinstance(total_pages, int) and total_pages > 0 and page >= total_pages:
                break
            page += 1
        return models


# ---------------------------------------------------------------------------
# parsers
# ---------------------------------------------------------------------------


def parse_openrouter(payload: Any, *, fetched_at: float | None = None) -> list[MarketModel]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise MarketError("OpenRouter payload must be an object with a data list")
    now = time.time() if fetched_at is None else fetched_at
    models: list[MarketModel] = []
    seen: set[str] = set()
    for entry in payload["data"]:
        if not isinstance(entry, dict):
            continue
        raw_id = _text(entry.get("id"), 200)
        if not raw_id or ":batch" in raw_id.lower():
            continue
        identifier = raw_id[1:] if raw_id.startswith("~") else raw_id
        if identifier.lower().endswith("-latest"):
            identifier = identifier[: -len("-latest")]
        vendor, _, name = identifier.partition("/")
        name = name or identifier
        key = canonical_model_key(vendor, name)
        if key in seen:
            continue
        seen.add(key)
        pricing = entry.get("pricing") if isinstance(entry.get("pricing"), dict) else {}
        parameters = entry.get("supported_parameters")
        parameters = set(parameters) if isinstance(parameters, list) else set()
        aliases = [raw_id.lstrip("~")]
        if identifier != raw_id:
            aliases.append(identifier)
        models.append(
            MarketModel(
                key=key,
                vendor=_text(vendor, 80) or "unknown",
                name=_text(entry.get("name"), 200) or name,
                input_per_mtok=_price_per_mtok(pricing.get("prompt"), per_token=True),
                output_per_mtok=_price_per_mtok(pricing.get("completion"), per_token=True),
                cache_read_per_mtok=_price_per_mtok(
                    pricing.get("input_cache_read"), per_token=True
                ),
                context_window=_positive_int(entry.get("context_length")),
                reasoning="reasoning" in parameters,
                tool_call="tools" in parameters,
                release_date=_iso_date(entry.get("created")),
                sources=["openrouter"],
                aliases=_add_unique([], aliases),
                fetched_at=now,
            )
        )
    return models


def parse_models_dev(payload: Any, *, fetched_at: float | None = None) -> list[MarketModel]:
    if not isinstance(payload, dict):
        raise MarketError("models.dev payload must be an object keyed by provider")
    now = time.time() if fetched_at is None else fetched_at
    models: list[MarketModel] = []
    for provider_id, provider in payload.items():
        if not isinstance(provider, dict):
            continue
        provider_models = provider.get("models")
        if not isinstance(provider_models, dict):
            continue
        vendor = _text(provider_id, 80) or _text(provider.get("id"), 80) or "unknown"
        for model_id, entry in provider_models.items():
            if not isinstance(entry, dict):
                continue
            name = _text(entry.get("name"), 200) or _text(model_id, 200)
            key = canonical_model_key(vendor, _text(model_id, 200) or name)
            cost = entry.get("cost") if isinstance(entry.get("cost"), dict) else {}
            limit = entry.get("limit") if isinstance(entry.get("limit"), dict) else {}
            models.append(
                MarketModel(
                    key=key,
                    vendor=vendor,
                    name=name,
                    input_per_mtok=_finite_number(cost.get("input")),
                    output_per_mtok=_finite_number(cost.get("output")),
                    cache_read_per_mtok=_finite_number(cost.get("cache_read")),
                    context_window=_positive_int(limit.get("context")),
                    reasoning=_bool(entry.get("reasoning")),
                    tool_call=_bool(entry.get("tool_call")),
                    release_date=_iso_date(entry.get("release_date")),
                    sources=["models.dev"],
                    aliases=_add_unique([], [_text(model_id, 200)]),
                    fetched_at=now,
                )
            )
    return models


def parse_artificial_analysis_page(
    payload: Any, *, fetched_at: float | None = None
) -> tuple[list[MarketModel], dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise MarketError("Artificial Analysis payload must be an object with a data list")
    pagination = payload.get("pagination")
    pagination = pagination if isinstance(pagination, dict) else {}
    now = time.time() if fetched_at is None else fetched_at
    models: list[MarketModel] = []
    for entry in payload["data"]:
        if not isinstance(entry, dict):
            continue
        creator = entry.get("model_creator")
        creator = creator if isinstance(creator, dict) else {}
        vendor = _text(creator.get("name"), 80) or _text(creator.get("id"), 80) or "unknown"
        slug = _text(entry.get("slug"), 200)
        name = _text(entry.get("name"), 200) or slug
        openrouter_id = _text(entry.get("openrouter_api_id"), 200)
        if openrouter_id:
            or_vendor, _, or_name = openrouter_id.partition("/")
            key = canonical_model_key(or_vendor, or_name or openrouter_id)
            aliases = [openrouter_id]
        else:
            key = canonical_model_key(vendor, slug or name)
            aliases = []
        aliases = _add_unique(aliases, [slug] if slug else [])
        evaluations = entry.get("evaluations")
        evaluations = evaluations if isinstance(evaluations, dict) else {}
        pricing = entry.get("pricing")
        pricing = pricing if isinstance(pricing, dict) else {}
        performance = entry.get("performance")
        performance = performance if isinstance(performance, dict) else {}
        intelligence = _finite_number(
            evaluations.get("artificial_analysis_intelligence_index")
        )
        if intelligence is not None:
            intelligence = min(100.0, intelligence)
        models.append(
            MarketModel(
                key=key,
                vendor=vendor,
                name=name,
                input_per_mtok=_finite_number(pricing.get("price_1m_input_tokens")),
                output_per_mtok=_finite_number(pricing.get("price_1m_output_tokens")),
                cache_read_per_mtok=_finite_number(pricing.get("price_1m_cache_hit_tokens")),
                context_window=_positive_int(entry.get("context_window_tokens")),
                reasoning=_bool(entry.get("reasoning_model")),
                release_date=_iso_date(entry.get("release_date")),
                intelligence=intelligence,
                intelligence_source="artificial_analysis" if intelligence is not None else None,
                speed_tps=_finite_number(
                    performance.get("median_output_tokens_per_second")
                ),
                sources=["artificial_analysis"],
                aliases=aliases,
                fetched_at=now,
            )
        )
    return models, pagination


def parse_artificial_analysis(
    payload: Any, *, fetched_at: float | None = None
) -> list[MarketModel]:
    models, _ = parse_artificial_analysis_page(payload, fetched_at=fetched_at)
    return models


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# merging, overrides and heuristic scoring
# ---------------------------------------------------------------------------


def merge_market_models(models: Sequence[MarketModel]) -> list[MarketModel]:
    """Merge entries with the same canonical key.

    Prices are taken from OpenRouter first and models.dev second.  Evaluation
    scores only ever come from Artificial Analysis or a user override; this
    function carries them through and never invents a score.
    """

    merged: dict[str, MarketModel] = {}
    order: list[str] = []
    for incoming in models:
        if incoming.key not in merged:
            merged[incoming.key] = incoming.model_copy(deep=True)
            order.append(incoming.key)
            continue
        current = merged[incoming.key]
        incoming_priority = _priority(incoming)
        current_priority = _priority(current)
        if incoming_priority < current_priority:
            if incoming.name:
                current.name = incoming.name
            if incoming.vendor:
                current.vendor = incoming.vendor
            if incoming.release_date:
                current.release_date = incoming.release_date
        for field in (
            "input_per_mtok",
            "output_per_mtok",
            "cache_read_per_mtok",
            "context_window",
        ):
            if getattr(current, field) is None and getattr(incoming, field) is not None:
                setattr(current, field, getattr(incoming, field))
            elif (
                getattr(incoming, field) is not None
                and incoming_priority < current_priority
                and field in {"input_per_mtok", "output_per_mtok", "cache_read_per_mtok"}
            ):
                setattr(current, field, getattr(incoming, field))
        current.reasoning = current.reasoning or incoming.reasoning
        current.tool_call = current.tool_call or incoming.tool_call
        if incoming.intelligence is not None and incoming.intelligence_source in {
            "artificial_analysis",
            "override",
        }:
            if (
                current.intelligence is None
                or current.intelligence_source != "artificial_analysis"
                or incoming.intelligence_source == "override"
            ):
                current.intelligence = incoming.intelligence
                current.intelligence_source = incoming.intelligence_source
        if current.speed_tps is None and incoming.speed_tps is not None:
            current.speed_tps = incoming.speed_tps
        _add_unique(current.sources, incoming.sources)
        _add_unique(current.aliases, incoming.aliases)
        current.fetched_at = max(current.fetched_at, incoming.fetched_at)
    for key in order:
        model = merged[key]
        model.sources = _sources_for(model)
        if model.intelligence_source not in _INTELLIGENCE_SOURCES:
            model.intelligence_source = None
    return [merged[key] for key in order]


def apply_overrides(
    models: Sequence[MarketModel],
    overrides: Mapping[str, MarketOverrideConfig | Mapping[str, Any]] | None,
) -> list[MarketModel]:
    """Apply user ``market.overrides`` globs (intelligence and prices)."""

    if not overrides:
        return [model.model_copy(deep=True) for model in models]
    normalized: list[tuple[str, dict[str, Any]]] = []
    for pattern, value in overrides.items():
        if isinstance(value, MarketOverrideConfig):
            payload = value.model_dump()
        elif isinstance(value, Mapping):
            payload = dict(value)
        else:
            continue
        normalized.append((str(pattern).lower(), payload))
    output: list[MarketModel] = []
    for model in models:
        copy = model.model_copy(deep=True)
        for pattern, payload in normalized:
            targets = [copy.key.lower(), copy.name.lower(), *(alias.lower() for alias in copy.aliases)]
            if not any(fnmatch.fnmatchcase(target, pattern) for target in targets):
                continue
            intelligence = payload.get("intelligence")
            if isinstance(intelligence, (int, float)) and not isinstance(intelligence, bool):
                copy.intelligence = max(0.0, min(100.0, float(intelligence)))
                copy.intelligence_source = "override"
            for field in ("input_per_mtok", "output_per_mtok", "cache_read_per_mtok"):
                value = payload.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    setattr(copy, field, float(value))
            context_window = payload.get("context_window")
            if isinstance(context_window, int) and not isinstance(context_window, bool):
                copy.context_window = context_window or None
            reasoning = payload.get("reasoning")
            if isinstance(reasoning, bool):
                copy.reasoning = reasoning
        output.append(copy)
    return output


def blended_price(model: MarketModel | None) -> float | None:
    """``(3 * input + output) / 4`` in USD per million tokens, or ``None``."""

    if model is None:
        return None
    if model.input_per_mtok is None or model.output_per_mtok is None:
        return None
    return (3.0 * model.input_per_mtok + model.output_per_mtok) / 4.0


def apply_heuristic_intelligence(
    models: Sequence[MarketModel],
    scorer: Callable[[str], float],
    *,
    now: float | None = None,
) -> list[MarketModel]:
    """Fill missing intelligence scores from capability, price, flags and age.

    The scorer is the existing :class:`~harness_mesh.discovery.ModelDiscoveryEngine`
    heuristic, so no model name, price or score is embedded here.
    """

    output = [model.model_copy(deep=True) for model in models]
    prices = sorted(
        value
        for value in (blended_price(model) for model in output)
        if value is not None and value > 0
    )
    now_date = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).date()

    def percentile(value: float) -> float:
        if not prices:
            return 0.0
        below = sum(1 for item in prices if item < value)
        return below / max(1, len(prices) - 1)

    for model in output:
        if model.intelligence is not None:
            continue
        try:
            capability = float(scorer(model.key))
        except Exception:
            capability = 20.0
        capability = max(1.0, min(100.0, capability))
        price = blended_price(model)
        price_bonus = 8.0 * percentile(price) if price is not None else 0.0
        reasoning_bonus = 4.0 if model.reasoning else 0.0
        recency_bonus = 0.0
        if model.release_date:
            try:
                released = date.fromisoformat(model.release_date)
                years = max(0.0, (now_date - released).days / 365.25)
                recency_bonus = max(0.0, 6.0 - 1.5 * years)
            except ValueError:
                recency_bonus = 0.0
        model.intelligence = round(
            max(1.0, min(100.0, capability + price_bonus + reasoning_bonus + recency_bonus)),
            2,
        )
        model.intelligence_source = "heuristic"
    return output


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

_EFFORT_SUFFIXES = ("minimal", "low", "medium", "high", "xhigh", "max")
_EFFORT_RANK = {"minimal": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}
_CONFIDENCE_RANK = {"none": 0, "normalized": 1, "alias": 2, "exact": 3}


def _split_identity(model_id: str) -> tuple[str, str | None]:
    """Return ``(name_without_effort, effort)`` after harmless normalisation."""

    text = _text(model_id, 300).lower()
    text = _BRACKETS.sub("", text).strip()
    effort: str | None = None
    for suffix in _EFFORT_SUFFIXES:
        marker = f"-{suffix}"
        if text.endswith(marker) and len(text) > len(marker):
            effort = suffix
            text = text[: -len(marker)]
            break
    text = text.strip("-")
    return text, effort


def normalize_model_identity(model_id: str) -> tuple[str, str | None]:
    """Public, pure identity normaliser used by matching tests and routing."""

    name, effort = _split_identity(model_id)
    name = name.split("/")[-1]
    name = name.replace(".", "-")
    name = _DATE_SUFFIX_8.sub("", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name, effort


def is_non_model_id(model_id: str) -> bool:
    """True for placeholders and role ids that are not routable models."""

    text = _text(model_id, 300).lower().strip()
    if not text or (text.startswith("<") and text.endswith(">")):
        return True
    return text.endswith("-auto-review") or text.endswith("-reserve")


def _market_identities(model: MarketModel) -> tuple[list[str], str]:
    raw: list[str] = [model.key]
    raw.extend(model.aliases)
    if "/" in model.key:
        raw.append(model.key.split("/", 1)[1])
    exact = []
    for item in raw:
        text = _BRACKETS.sub("", _text(item, 300).lower())
        if text and text not in exact:
            exact.append(text)
    return exact, _normalize_name(model.key.split("/", 1)[-1])


def match_model(
    model_id: str,
    models: Sequence[MarketModel],
    *,
    aliases: Mapping[str, str] | None = None,
) -> ModelMatch:
    """Map one harness model id onto the catalogue without guessing.

    Exact identities win over normalised ones, an explicit alias wins over
    both, and any ambiguity resolves to ``none`` rather than a wrong match.
    """

    name, effort = _split_identity(model_id)
    if is_non_model_id(model_id):
        return ModelMatch(confidence="none", effort_variant=effort)

    by_key = {model.key: model for model in models}

    if aliases:
        lowered = _text(model_id, 300).lower()
        exact_alias = None
        glob_alias = None
        for pattern, target in aliases.items():
            pattern_text = str(pattern).lower()
            if pattern_text == lowered:
                exact_alias = str(target)
                break
            if glob_alias is None and fnmatch.fnmatchcase(lowered, pattern_text):
                glob_alias = str(target)
        target = exact_alias or glob_alias
        if target:
            if target in by_key:
                return ModelMatch(
                    key=target,
                    confidence="alias",
                    effort_variant=effort,
                    matched_name=by_key[target].name,
                )
            return ModelMatch(confidence="none", effort_variant=effort)

    # 1. exact raw identity (case-insensitive, brackets removed, vendor optional)
    exact_targets: list[str] = []
    lowered = _BRACKETS.sub("", _text(model_id, 300).lower()).strip()
    for model in models:
        identities, _ = _market_identities(model)
        if lowered in identities:
            exact_targets.append(model.key)
    exact_targets = list(dict.fromkeys(exact_targets))
    if len(exact_targets) == 1:
        return _match(exact_targets[0], by_key, "exact", effort)
    if len(exact_targets) > 1:
        return ModelMatch(confidence="none", effort_variant=effort)

    # 2. normalised identity: dot/hyphen equivalence, 8-digit dates, effort
    normalized_name, _ = normalize_model_identity(model_id)
    if not normalized_name:
        return ModelMatch(confidence="none", effort_variant=effort)
    normalized_targets = [
        model.key
        for model in models
        if _normalize_name(model.key.split("/", 1)[-1]) == normalized_name
        or normalized_name in {_normalize_name(alias) for alias in model.aliases}
    ]
    normalized_targets = list(dict.fromkeys(normalized_targets))
    if len(normalized_targets) == 1:
        return _match(normalized_targets[0], by_key, "normalized", effort)
    if len(normalized_targets) > 1:
        return ModelMatch(confidence="none", effort_variant=effort)

    # 3. short snapshot suffixes (``-1017``) only when the folded base is unique
    snapshots = [
        model.key
        for model in models
        if _DATE_SUFFIX_4.sub("", _normalize_name(model.key.split("/", 1)[-1])) == normalized_name
    ]
    snapshots = list(dict.fromkeys(snapshots))
    if len(snapshots) == 1:
        return _match(snapshots[0], by_key, "normalized", effort)
    return ModelMatch(confidence="none", effort_variant=effort)


def _match(
    key: str, by_key: Mapping[str, MarketModel], confidence: str, effort: str | None
) -> ModelMatch:
    model = by_key.get(key)
    return ModelMatch(
        key=key,
        confidence=confidence,
        effort_variant=effort,
        matched_name=model.name if model else None,
    )


def best_match(
    model_ids: Sequence[str],
    models: Sequence[MarketModel],
    *,
    aliases: Mapping[str, str] | None = None,
) -> ModelMatch:
    """Return the strongest match across the effort/context variants of a model."""

    best = ModelMatch(confidence="none")
    for model_id in model_ids:
        candidate = match_model(model_id, models, aliases=aliases)
        if _CONFIDENCE_RANK[candidate.confidence] > _CONFIDENCE_RANK[best.confidence]:
            best = candidate
        elif (
            _CONFIDENCE_RANK[candidate.confidence] == _CONFIDENCE_RANK[best.confidence]
            and best.key is None
            and candidate.key is not None
        ):
            best = candidate
    return best


def list_unmatched(
    harness_models: Mapping[str, Sequence[str]],
    models: Sequence[MarketModel],
    *,
    aliases: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """List harness model ids that need a ``market.aliases`` entry."""

    unmatched: list[dict[str, str]] = []
    for harness, model_ids in harness_models.items():
        for model_id in model_ids:
            if is_non_model_id(model_id):
                continue
            found = match_model(model_id, models, aliases=aliases)
            if found.key is None:
                _, effort = _split_identity(model_id)
                unmatched.append(
                    {
                        "harness": harness,
                        "model_id": model_id,
                        "effort_variant": effort or "",
                    }
                )
    return unmatched


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class MarketService:
    """Fetch, cache and merge the three market sources.

    Routing reads :meth:`catalog`, which never performs a network request.  A
    caller that wants fresh data calls :meth:`refresh` (CLI, MCP background
    task) so dispatch latency never depends on a third-party site.
    """

    def __init__(
        self,
        storage: StateStore,
        config: MarketConfig | None = None,
        *,
        env: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        now_fn: Callable[[], float] | None = None,
        scorer: Callable[[str], float] | None = None,
        providers: Mapping[str, MarketProvider] | None = None,
    ):
        self.storage = storage
        self.config = config or MarketConfig()
        self.env = dict(env) if env is not None else dict(os.environ)
        self.now_fn = now_fn or time.time
        self.scorer = scorer
        self.transport = transport
        self._aa_day = self._day_key(self.now_fn())
        self._aa_calls = 0
        self._background_task: asyncio.Task | None = None
        self._failed_sources: set[str] = set()
        self._warnings: list[str] = []
        if providers is not None:
            self.providers = dict(providers)
        else:
            self.providers = {
                "openrouter": OpenRouterProvider(transport=transport),
                "models_dev": ModelsDevProvider(transport=transport),
                "artificial_analysis": ArtificialAnalysisProvider(
                    env=self.env,
                    key_env=self.config.artificial_analysis_key_env,
                    transport=transport,
                ),
            }

    # -- configuration -------------------------------------------------
    @classmethod
    def from_config(cls, config: Any, storage: StateStore, **kwargs: Any) -> "MarketService":
        market_config = getattr(config, "market", None)
        if not isinstance(market_config, MarketConfig):
            market_config = MarketConfig()
        return cls(storage, market_config, **kwargs)

    @property
    def offline(self) -> bool:
        value = self.env.get("HARNESS_MESH_OFFLINE", "")
        return str(value).lower() in {"1", "true", "yes", "on"}

    @property
    def requires_network(self) -> bool:
        return bool(self.config.enabled) and not self.offline

    def _sources(self) -> list[str]:
        configured = {_canonical_source(source) for source in self.config.sources}
        return [source for source in SOURCE_ORDER if source in configured]

    def _ttl_for(self, source: str) -> float:
        if source == "artificial_analysis":
            return float(
                max(
                    int(self.config.artificial_analysis_ttl_seconds),
                    ARTIFICIAL_ANALYSIS_MIN_TTL,
                )
            )
        return float(max(1, int(self.config.ttl_seconds)))

    def _stale_after_seconds(self) -> float:
        return float(max(1, int(self.config.stale_after_days))) * 86_400

    @staticmethod
    def _day_key(now: float) -> str:
        return datetime.fromtimestamp(now, timezone.utc).date().isoformat()

    def _roll_aa_day(self) -> None:
        today = self._day_key(self.now_fn())
        if today != self._aa_day:
            self._aa_day = today
            self._aa_calls = 0

    # -- refresh -------------------------------------------------------
    async def refresh(
        self,
        *,
        force: bool = False,
        sources: Sequence[str] | None = None,
    ) -> MarketRefreshResult:
        statuses: list[MarketSourceStatus] = []
        warnings: list[str] = []
        selected = list(sources) if sources is not None else self._sources()
        selected = [source for source in SOURCE_ORDER if source in set(selected)]

        if not self.config.enabled:
            for source in selected:
                statuses.append(MarketSourceStatus(source=source, status="disabled"))
            return MarketRefreshResult(
                statuses=statuses,
                catalog=self._build_catalog(warnings=["market data is disabled"]),
            )
        if self.offline:
            for source in selected:
                statuses.append(MarketSourceStatus(source=source, status="offline"))
            return MarketRefreshResult(
                statuses=statuses,
                catalog=self._build_catalog(warnings=["offline mode: cached data only"]),
            )

        for source in selected:
            provider = self.providers.get(source)
            if provider is None:
                statuses.append(
                    MarketSourceStatus(source=source, status="unavailable", error="provider missing")
                )
                continue
            snapshot = self.storage.get_market_snapshot(source)
            age = None if snapshot is None else max(0.0, self.now_fn() - snapshot["fetched_at"])
            if snapshot is not None and not force and age is not None and age <= self._ttl_for(source):
                statuses.append(
                    MarketSourceStatus(
                        source=source,
                        status="cached",
                        models=_snapshot_model_count(snapshot),
                        fetched_at=snapshot["fetched_at"],
                    )
                )
                continue
            if (
                source == "artificial_analysis"
                and hasattr(provider, "api_key")
                and not getattr(provider, "api_key")
            ):
                warnings.append(
                    "artificial_analysis: no API key; using heuristic intelligence"
                )
                statuses.append(
                    MarketSourceStatus(
                        source=source,
                        status="skipped",
                        models=_snapshot_model_count(snapshot) if snapshot else 0,
                        fetched_at=snapshot["fetched_at"] if snapshot else None,
                        error="missing Artificial Analysis API key",
                    )
                )
                continue
            if source == "artificial_analysis":
                self._roll_aa_day()
                if self._aa_calls >= int(self.config.artificial_analysis_daily_limit):
                    self._failed_sources.add(source)
                    statuses.append(
                        MarketSourceStatus(
                            source=source,
                            status="rate_limited",
                            models=_snapshot_model_count(snapshot) if snapshot else 0,
                            fetched_at=snapshot["fetched_at"] if snapshot else None,
                            error="daily Artificial Analysis request cap reached",
                        )
                    )
                    continue
                self._aa_calls += 1
                budget = max(
                    1,
                    int(self.config.artificial_analysis_daily_limit) - self._aa_calls + 1,
                )
                if hasattr(provider, "request_budget"):
                    provider.request_budget = budget  # type: ignore[attr-defined]
            try:
                models = await provider.fetch()
            except Exception as exc:
                self._failed_sources.add(source)
                message = f"{source}: {exc}"
                warnings.append(message)
                if snapshot is not None:
                    statuses.append(
                        MarketSourceStatus(
                            source=source,
                            status="stale",
                            models=_snapshot_model_count(snapshot),
                            fetched_at=snapshot["fetched_at"],
                            error=str(exc)[:500],
                        )
                    )
                else:
                    statuses.append(
                        MarketSourceStatus(
                            source=source, status="failed", error=str(exc)[:500]
                        )
                    )
                continue
            if source == "artificial_analysis":
                pages = int(getattr(provider, "last_page_count", 1) or 1)
                self._aa_calls += max(0, pages - 1)
            self._failed_sources.discard(source)
            fetched_at = self.now_fn()
            payload = {
                "models": [model.model_dump(mode="json") for model in models],
                "fetched_at": fetched_at,
            }
            self.storage.save_market_snapshot(source, fetched_at, payload)
            statuses.append(
                MarketSourceStatus(
                    source=source,
                    status="fetched",
                    models=len(models),
                    fetched_at=fetched_at,
                )
            )

        self._warnings = _dedupe(warnings)
        catalog = self._build_catalog(warnings=warnings)
        return MarketRefreshResult(statuses=statuses, catalog=catalog)

    # -- cached read ---------------------------------------------------
    def catalog(self) -> MarketCatalog:
        return self._build_catalog(warnings=None)

    def _build_catalog(self, *, warnings: list[str] | None = None) -> MarketCatalog:
        warnings = list(warnings if warnings is not None else self._warnings)
        if not self.config.enabled:
            return MarketCatalog(
                enabled=False,
                warnings=warnings or ["market data is disabled"],
            )

        collected: list[MarketModel] = []
        sources: list[str] = []
        fetched_at: list[float] = []
        stale = False
        expired = False
        now = self.now_fn()
        for source in self._sources():
            snapshot = self.storage.get_market_snapshot(source)
            if snapshot is None:
                continue
            payload = snapshot.get("payload")
            raw_models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(raw_models, list):
                warnings.append(f"{source}: cached snapshot has no model list")
                continue
            age = max(0.0, now - snapshot["fetched_at"])
            source_ttl = self._ttl_for(source)
            if age > source_ttl or age > self._stale_after_seconds():
                stale = True
            if age > self._stale_after_seconds():
                expired = True
            if source in self._failed_sources:
                stale = True
            sources.append(source)
            fetched_at.append(snapshot["fetched_at"])
            for entry in raw_models:
                try:
                    collected.append(MarketModel.model_validate(entry))
                except Exception:
                    warnings.append(f"{source}: skipped one malformed cached model")
        merged = merge_market_models(collected)
        merged = apply_overrides(merged, self.config.overrides)
        if self.scorer is not None:
            merged = apply_heuristic_intelligence(merged, self.scorer, now=now)
        merged.sort(key=lambda model: model.key)
        return MarketCatalog(
            models=merged,
            sources=[source for source in SOURCE_ORDER if source in sources],
            fetched_at=max(fetched_at) if fetched_at else None,
            stale=stale,
            expired=expired,
            enabled=True,
            warnings=_dedupe(warnings),
        )

    # -- background refresh -------------------------------------------
    def start_background_refresh(self, interval_seconds: float | None = None) -> asyncio.Task | None:
        """Refresh once asynchronously, then on a TTL cadence.

        Failures are logged and never propagate to dispatch.  The task is
        cancelled by :meth:`stop_background_refresh`.
        """

        if not self.config.enabled or self.offline:
            return None
        if getattr(self, "_background_task", None) is not None:
            return self._background_task
        interval = float(
            interval_seconds if interval_seconds is not None else max(60, int(self.config.ttl_seconds))
        )

        async def loop() -> None:
            while True:
                try:
                    await self.refresh(force=False)
                except asyncio.CancelledError:  # pragma: no cover - cooperative cancel
                    raise
                except Exception as exc:  # never block or slow dispatch
                    logger.warning("market refresh failed: %s", exc)
                await asyncio.sleep(max(30.0, interval))

        self._background_task = asyncio.create_task(loop())
        return self._background_task

    async def stop_background_refresh(self) -> None:
        task = getattr(self, "_background_task", None)
        self._background_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # -- reporting -----------------------------------------------------
    def source_status(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for source in self._sources():
            snapshot = self.storage.get_market_snapshot(source)
            rows.append(
                {
                    "source": source,
                    "fetched_at": snapshot["fetched_at"] if snapshot else None,
                    "models": _snapshot_model_count(snapshot) if snapshot else 0,
                }
            )
        return rows


def _snapshot_model_count(snapshot: Mapping[str, Any] | None) -> int:
    if not snapshot:
        return 0
    payload = snapshot.get("payload")
    models = payload.get("models") if isinstance(payload, dict) else None
    return len(models) if isinstance(models, list) else 0


def _dedupe(values: Sequence[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return output


__all__ = [
    "ALLOWED_HOSTS",
    "ARTIFICIAL_ANALYSIS_URL",
    "ArtificialAnalysisProvider",
    "MAX_RESPONSE_BYTES",
    "MarketCatalog",
    "MarketError",
    "MarketModel",
    "MarketProvider",
    "MarketRefreshResult",
    "MarketService",
    "MarketSourceStatus",
    "ModelMatch",
    "ModelsDevProvider",
    "OpenRouterProvider",
    "SOURCE_ORDER",
    "apply_heuristic_intelligence",
    "apply_overrides",
    "best_match",
    "blended_price",
    "canonical_model_key",
    "fetch_json",
    "is_non_model_id",
    "list_unmatched",
    "match_model",
    "merge_market_models",
    "normalize_model_identity",
    "parse_artificial_analysis",
    "parse_artificial_analysis_page",
    "parse_models_dev",
    "parse_openrouter",
]
