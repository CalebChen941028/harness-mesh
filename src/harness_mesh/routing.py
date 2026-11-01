"""Task-kind inference, tier selection and market-aware model routing.

The router asks this module for a decision only after quota, authorisation,
adapter availability and recursion filters have run.  The planner itself is a
pure function over candidates, so the ordering rules can be tested without a
state store or a network.
"""

from __future__ import annotations

import fnmatch
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .config import RoutingConfig
from .market import MarketCatalog, MarketModel, blended_price, match_model, normalize_model_identity
from .models import (
    DiscoveredModel,
    RoutingMode,
    RoutingTier,
    TaskDifficulty,
    TaskKind,
)

_EFFORT_RANK = {"minimal": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}
_TIER_EFFORT = {
    RoutingTier.PREMIUM.value: "high",
    RoutingTier.BALANCED.value: "medium",
    RoutingTier.VALUE.value: "low",
}
_PREMIUM_KINDS = {TaskKind.PLAN.value, TaskKind.AUDIT.value, TaskKind.REVIEW.value}
_BALANCED_KINDS = {TaskKind.REFACTOR.value, TaskKind.DEBUG.value}

_KIND_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        TaskKind.AUDIT.value,
        (
            "audit",
            "security review",
            "security audit",
            "审计",
            "复审",
            "稽查",
            "安全审查",
            "安全审计",
        ),
    ),
    (
        TaskKind.REVIEW.value,
        ("review", "code review", "评审", "审查", "代码审查"),
    ),
    (
        TaskKind.PLAN.value,
        (
            "plan",
            "design",
            "architecture",
            "proposal",
            "strategy",
            "方案",
            "设计",
            "架构",
            "规划",
            "提案",
            "策略",
        ),
    ),
    (TaskKind.REFACTOR.value, ("refactor", "重构")),
    (TaskKind.DEBUG.value, ("debug", "fix", "bug", "调试", "修复", "排查")),
    (TaskKind.TEST.value, ("test", "tests", "pytest", "coverage", "测试", "用例", "覆盖")),
    (TaskKind.DOCS.value, ("docs", "documentation", "readme", "文档", "注释")),
    (
        TaskKind.IMPLEMENT.value,
        ("implement", "build", "feature", "实现", "开发", "功能"),
    ),
)


def _contains(text: str, phrase: str) -> bool:
    if phrase.isascii():
        import re

        return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None
    return phrase in text


def infer_task_kind(
    objective: str,
    difficulty: TaskDifficulty | str = TaskDifficulty.MEDIUM,
    explicit: TaskKind | str = TaskKind.AUTO,
) -> TaskKind:
    """Classify an objective into a routing task kind.

    English keywords are matched at word boundaries so ``review`` does not fire
    inside ``preview``.  When nothing matches, the existing difficulty tier
    decides: critical reads as an audit, high as a refactor, the rest as an
    implementation.
    """

    if isinstance(explicit, TaskKind):
        if explicit != TaskKind.AUTO:
            return explicit
    elif str(explicit) != TaskKind.AUTO.value:
        return TaskKind(str(explicit))

    text = (objective or "").lower()
    for kind, terms in _KIND_TERMS:
        if any(_contains(text, term) for term in terms):
            return TaskKind(kind)

    value = difficulty.value if isinstance(difficulty, TaskDifficulty) else str(difficulty)
    if value == TaskDifficulty.CRITICAL.value:
        return TaskKind.AUDIT
    if value == TaskDifficulty.HIGH.value:
        return TaskKind.REFACTOR
    return TaskKind.IMPLEMENT


def tier_for(task_kind: TaskKind | str, difficulty: TaskDifficulty | str) -> RoutingTier:
    kind = task_kind.value if isinstance(task_kind, TaskKind) else str(task_kind)
    level = difficulty.value if isinstance(difficulty, TaskDifficulty) else str(difficulty)
    if kind in _PREMIUM_KINDS or level == TaskDifficulty.CRITICAL.value:
        return RoutingTier.PREMIUM
    if kind in _BALANCED_KINDS or level == TaskDifficulty.HIGH.value:
        return RoutingTier.BALANCED
    return RoutingTier.VALUE


class RouteCandidate(BaseModel):
    """One quotable ``harness/model`` pair produced before ranking."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    harness: str
    model: str
    base_model: str = ""
    effort_variant: str | None = None
    intelligence: float
    intelligence_source: str = "heuristic"
    market_key: str | None = None
    confidence: str = "none"
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None
    blended_price: float | None = None
    reasoning: bool = False


class RoutingChoice(BaseModel):
    harness: str
    model: str
    effort: str | None = None
    intelligence: float | None = None
    intelligence_source: str | None = None
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None
    blended_price: float | None = None
    market_key: str | None = None
    confidence: str | None = None


class RoutingAlternative(BaseModel):
    harness: str
    model: str
    score: float
    intelligence: float
    intelligence_source: str
    blended_price: float | None = None


class RoutingData(BaseModel):
    sources: list[str] = Field(default_factory=list)
    fetched_at: float | None = None
    stale: bool = False
    expired: bool = False
    confidence: str | None = None
    matched_key: str | None = None


class RoutingDecision(BaseModel):
    """The auditable record written to the task result and the transcript."""

    mode: str = RoutingMode.AUTO.value
    task_kind: str = TaskKind.AUTO.value
    tier: str = RoutingTier.VALUE.value
    chosen: RoutingChoice | None = None
    alternatives: list[RoutingAlternative] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    data: RoutingData = Field(default_factory=RoutingData)


def _full_identity(model_id: str) -> str:
    text = model_id.lower().strip()
    import re

    text = re.sub(r"\[[^\]]*\]", "", text)
    text = text.split("/")[-1].replace(".", "-")
    text = re.sub(r"-\d{8}$", "", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


def _effort_rank(effort: str | None) -> int:
    if effort is None:
        return 1
    return _EFFORT_RANK.get(effort.lower(), 1)


def _matches_hint(candidate: RouteCandidate, hints: Sequence[str]) -> bool:
    targets = [
        candidate.model.lower(),
        (candidate.market_key or "").lower(),
        (candidate.base_model or "").lower(),
    ]
    for hint in hints:
        if not hint:
            continue
        pattern = str(hint).lower()
        if any(fnmatch.fnmatchcase(target, pattern) for target in targets if target):
            return True
    return False


def _choose_member(
    members: Sequence[tuple[DiscoveredModel, str | None]],
    tier: str,
    preferred_effort: str | None,
) -> tuple[DiscoveredModel, str | None]:
    if preferred_effort:
        for model, effort in members:
            if effort == preferred_effort:
                return model, effort
    target = _TIER_EFFORT.get(tier)
    explicit = [(model, effort) for model, effort in members if effort is not None]
    for model, effort in explicit:
        if effort == target:
            return model, effort
    if explicit:
        if tier == RoutingTier.PREMIUM.value:
            return max(explicit, key=lambda item: (_effort_rank(item[1]), -len(item[0].model_id)))
        if tier == RoutingTier.VALUE.value:
            return min(explicit, key=lambda item: (_effort_rank(item[1]), len(item[0].model_id)))
        return min(
            explicit,
            key=lambda item: (abs(_effort_rank(item[1]) - 2), -_effort_rank(item[1]), len(item[0].model_id)),
        )
    ordered = sorted(members, key=lambda item: (len(item[0].model_id), item[0].model_id))
    return ordered[0]


def build_candidates(
    models: Sequence[DiscoveredModel],
    market_models: Sequence[MarketModel],
    *,
    tier: str,
    aliases: Mapping[str, str] | None = None,
    available: Callable[[str, str], bool] | None = None,
    preferred_effort: str | None = None,
) -> list[RouteCandidate]:
    """Collapse effort variants into one candidate per base model.

    The market match decides whether a trailing ``-high``/``-max`` is a
    reasoning level or part of the real model name, so ``qwen3.8-max`` is not
    mistaken for an effort variant.
    """

    groups: dict[tuple[str, str], list[tuple[DiscoveredModel, str | None, str]]] = {}
    for model in models:
        if not model.authorized:
            continue
        if available is not None and not available(model.harness, model.model_id):
            continue
        found = match_model(model.model_id, market_models, aliases=aliases)
        _, effort = normalize_model_identity(model.model_id)
        group_key = found.key or f"local:{normalize_model_identity(model.model_id)[0]}"
        groups.setdefault((model.harness, group_key), []).append((model, effort, found.key or ""))

    candidates: list[RouteCandidate] = []
    for (harness, _group), members in groups.items():
        chosen_model, chosen_effort = _choose_member(
            [(item[0], item[1]) for item in members], tier, preferred_effort
        )
        found = match_model(chosen_model.model_id, market_models, aliases=aliases)
        market = _lookup(found.key, market_models)
        intelligence = market.intelligence if market and market.intelligence is not None else None
        source = (market.intelligence_source if market else None) or "heuristic"
        if intelligence is None:
            intelligence = float(chosen_model.capability_score)
            source = "heuristic"
        # Only an explicit reasoning suffix belongs in ``reasoning_effort``.
        if chosen_effort is not None and found.effort_variant is None and len(members) == 1:
            chosen_effort = None
        candidates.append(
            RouteCandidate(
                harness=harness,
                model=chosen_model.model_id,
                base_model=normalize_model_identity(chosen_model.model_id)[0],
                effort_variant=chosen_effort,
                intelligence=round(float(intelligence), 2),
                intelligence_source=source,
                market_key=found.key,
                confidence=found.confidence,
                input_per_mtok=market.input_per_mtok if market else None,
                output_per_mtok=market.output_per_mtok if market else None,
                blended_price=blended_price(market) if market else None,
                reasoning=bool((market and market.reasoning) or chosen_model.supports_reasoning),
            )
        )
    candidates.sort(key=lambda item: (item.harness, item.model))
    return candidates


def _lookup(key: str | None, models: Sequence[MarketModel]) -> MarketModel | None:
    if not key:
        return None
    for model in models:
        if model.key == key:
            return model
    return None


class RoutePlanner:
    """Rank candidates for one of the three tiers and explain the choice."""

    def __init__(
        self,
        *,
        floors: Mapping[str, float] | None = None,
        premium_hints: Sequence[str] = (),
        value_hints: Sequence[str] = (),
        strict_hints: bool = False,
        pins: Mapping[str, Any] | None = None,
    ):
        self.floors = {"low": 0.55, "medium": 0.70, **(dict(floors or {}))}
        self.premium_hints = [str(item) for item in premium_hints if str(item)]
        self.value_hints = [str(item) for item in value_hints if str(item)]
        self.strict_hints = bool(strict_hints)
        self.pins = dict(pins or {})

    @classmethod
    def from_config(cls, config: RoutingConfig | None) -> "RoutePlanner":
        if config is None:
            return cls()
        return cls(
            floors=config.floors,
            premium_hints=config.premium_hints,
            value_hints=config.value_hints,
            strict_hints=config.strict_hints,
            pins=config.pins,
        )

    def _pin_for(self, task_kind: str) -> tuple[str, str, str | None] | None:
        pin = self.pins.get(task_kind)
        if pin is None:
            return None
        if hasattr(pin, "harness"):
            return str(pin.harness), str(pin.model), getattr(pin, "effort", None)
        if isinstance(pin, Mapping) and pin.get("harness") and pin.get("model"):
            return str(pin["harness"]), str(pin["model"]), pin.get("effort")
        return None

    def plan(
        self,
        candidates: Sequence[RouteCandidate],
        *,
        task_kind: TaskKind | str,
        difficulty: TaskDifficulty | str,
        mode: RoutingMode | str = RoutingMode.AUTO,
        tier: str | None = None,
        catalog: MarketCatalog | None = None,
        max_price_per_mtok: float | None = None,
        min_intelligence: float | None = None,
    ) -> RoutingDecision:
        kind = task_kind.value if isinstance(task_kind, TaskKind) else str(task_kind)
        level = difficulty.value if isinstance(difficulty, TaskDifficulty) else str(difficulty)
        chosen_tier = tier or tier_for(kind, level).value
        mode_value = mode.value if isinstance(mode, RoutingMode) else str(mode)
        reasons = [f"task kind {kind} at {level} difficulty maps to the {chosen_tier} tier"]
        data = RoutingData(
            sources=list(catalog.sources) if catalog else [],
            fetched_at=catalog.fetched_at if catalog else None,
            stale=bool(catalog.stale) if catalog else False,
            expired=bool(catalog.expired) if catalog else False,
        )

        pool = [candidate for candidate in candidates]
        filtered = [candidate for candidate in pool if self._passes(candidate, max_price_per_mtok, min_intelligence)]
        if len(filtered) != len(pool):
            reasons.append(
                f"{len(pool) - len(filtered)} candidate(s) failed the price or intelligence constraint"
            )
        if not filtered:
            reasons.append("no candidate satisfied the active routing constraints")
            return RoutingDecision(
                mode=mode_value,
                task_kind=kind,
                tier=chosen_tier,
                reasons=reasons,
                data=data,
            )

        pin = self._pin_for(kind)
        if pin is not None:
            harness, model_name, effort = pin
            for candidate in filtered:
                if candidate.harness == harness and candidate.model == model_name:
                    reasons.append(f"task kind {kind} is pinned to {harness}/{model_name}")
                    return self._decision(
                        mode_value,
                        kind,
                        chosen_tier,
                        [(candidate, self._utility(candidate, chosen_tier))],
                        reasons,
                        data,
                        effort=effort or candidate.effort_variant,
                    )
            reasons.append(f"pin {harness}/{model_name} is not available; routing normally")

        hints = self._hints_for(chosen_tier)
        if self.strict_hints and hints:
            hinted = [candidate for candidate in filtered if _matches_hint(candidate, hints)]
            if not hinted:
                reasons.append(
                    f"strict hints {hints} matched no candidate; no model was routed"
                )
                return RoutingDecision(
                    mode=mode_value,
                    task_kind=kind,
                    tier=chosen_tier,
                    reasons=reasons,
                    data=data,
                )
            reasons.append(f"strict hints kept {len(hinted)} of {len(filtered)} candidate(s)")
            filtered = hinted
        elif hints:
            matched = [candidate for candidate in filtered if _matches_hint(candidate, hints)]
            if matched:
                reasons.append(
                    f"soft hints matched {', '.join(sorted({item.model for item in matched}))}"
                )

        ranked, extra_reasons = self._rank(filtered, chosen_tier, level)
        reasons.extend(extra_reasons)
        if not ranked:
            reasons.append("no candidate could be ranked")
            return RoutingDecision(
                mode=mode_value,
                task_kind=kind,
                tier=chosen_tier,
                reasons=reasons,
                data=data,
            )
        return self._decision(mode_value, kind, chosen_tier, ranked, reasons, data)

    @staticmethod
    def _passes(
        candidate: RouteCandidate,
        max_price: float | None,
        min_intelligence: float | None,
    ) -> bool:
        if min_intelligence is not None and candidate.intelligence < float(min_intelligence):
            return False
        if max_price is not None:
            if candidate.blended_price is None or candidate.blended_price > float(max_price):
                return False
        return True

    def _hints_for(self, tier: str) -> list[str]:
        if tier == RoutingTier.PREMIUM.value:
            return list(self.premium_hints)
        if tier == RoutingTier.VALUE.value:
            return list(self.value_hints)
        return [*self.premium_hints, *self.value_hints]

    def _premium_intelligence(self, candidate: RouteCandidate) -> float:
        intelligence = candidate.intelligence
        if _matches_hint(candidate, self.premium_hints):
            intelligence = min(100.0, intelligence + 5.0)
        return intelligence

    def _effective_price(self, candidate: RouteCandidate) -> float | None:
        price = candidate.blended_price
        if price is not None and _matches_hint(candidate, self.value_hints):
            return price * 0.85
        return price

    def _utility(self, candidate: RouteCandidate, tier: str) -> float:
        intelligence = self._premium_intelligence(candidate)
        price = self._effective_price(candidate)
        if tier == RoutingTier.PREMIUM.value:
            return round(intelligence, 4)
        if price is None:
            return round(intelligence, 4)
        if tier == RoutingTier.BALANCED.value:
            return round(intelligence / price, 4) if price > 0 else float("inf")
        return round(-price, 4)

    def _sort_key(self, candidate: RouteCandidate, tier: str) -> tuple[Any, ...]:
        intelligence = self._premium_intelligence(candidate)
        price = self._effective_price(candidate)
        if tier == RoutingTier.PREMIUM.value:
            return (
                intelligence,
                -price if price is not None else float("-inf"),
                intelligence,
                candidate.harness,
                candidate.model,
            )
        if tier == RoutingTier.BALANCED.value:
            efficiency = intelligence / price if price and price > 0 else float("-inf")
            return (efficiency, intelligence, candidate.harness, candidate.model)
        return (
            -price if price is not None else float("-inf"),
            intelligence,
            candidate.harness,
            candidate.model,
        )

    def _rank(
        self, candidates: Sequence[RouteCandidate], tier: str, difficulty: str
    ) -> tuple[list[tuple[RouteCandidate, float]], list[str]]:
        reasons: list[str] = []
        pool = list(candidates)
        priced = [candidate for candidate in pool if candidate.blended_price is not None]
        if tier in {RoutingTier.BALANCED.value, RoutingTier.VALUE.value}:
            if priced:
                if len(priced) != len(pool):
                    reasons.append(
                        f"{len(pool) - len(priced)} candidate(s) without a known price were not ranked by price"
                    )
                pool = priced
            else:
                reasons.append("no candidate has a known price; ranking by intelligence only")

        maximum = max(candidate.intelligence for candidate in pool)
        if tier == RoutingTier.BALANCED.value:
            threshold = maximum * 0.85
            eligible = [candidate for candidate in pool if candidate.intelligence >= threshold] or pool
            reasons.append(
                f"kept {len(eligible)} of {len(pool)} candidate(s) at or above 85% of the best intelligence"
            )
        elif tier == RoutingTier.VALUE.value:
            floor = float(self.floors.get(difficulty, 0.70))
            floor = max(0.0, min(1.0, floor))
            threshold = maximum * floor
            eligible = [candidate for candidate in pool if candidate.intelligence >= threshold] or pool
            reasons.append(
                f"kept {len(eligible)} of {len(pool)} candidate(s) at or above "
                f"{floor:.0%} of the best intelligence"
            )
        else:
            eligible = pool
            reasons.append(f"ranked {len(eligible)} candidate(s) by intelligence")

        scored = [(candidate, self._utility(candidate, tier)) for candidate in eligible]
        scored.sort(key=lambda item: self._sort_key(item[0], tier), reverse=True)
        return scored, reasons

    def _decision(
        self,
        mode: str,
        task_kind: str,
        tier: str,
        ranked: Sequence[tuple[RouteCandidate, float]],
        reasons: list[str],
        data: RoutingData,
        *,
        effort: str | None = None,
    ) -> RoutingDecision:
        best, best_score = ranked[0]
        chosen = RoutingChoice(
            harness=best.harness,
            model=best.model,
            effort=effort if effort is not None else best.effort_variant,
            intelligence=best.intelligence,
            intelligence_source=best.intelligence_source,
            input_per_mtok=best.input_per_mtok,
            output_per_mtok=best.output_per_mtok,
            blended_price=best.blended_price,
            market_key=best.market_key,
            confidence=best.confidence,
        )
        reasons.append(
            f"selected {best.harness}/{best.model} (score {best_score:g}, "
            f"intelligence {best.intelligence:g} from {best.intelligence_source}, "
            f"blended price {best.blended_price if best.blended_price is not None else 'unknown'})"
        )
        reasons.append(
            "prices are API list prices per million tokens, used as a proxy for "
            "subscription quota consumption"
        )
        if data.stale:
            reasons.append(
                "market data is stale; prices and scores may lag the provider listings"
            )
        alternatives = [
            RoutingAlternative(
                harness=candidate.harness,
                model=candidate.model,
                score=round(score, 4) if score not in {float("inf"), float("-inf")} else score,
                intelligence=candidate.intelligence,
                intelligence_source=candidate.intelligence_source,
                blended_price=candidate.blended_price,
            )
            for candidate, score in ranked[1:4]
        ]
        return RoutingDecision(
            mode=mode,
            task_kind=task_kind,
            tier=tier,
            chosen=chosen,
            alternatives=alternatives,
            reasons=reasons,
            data=data.model_copy(
                update={
                    "confidence": best.confidence,
                    "matched_key": best.market_key,
                }
            ),
        )


__all__ = [
    "RouteCandidate",
    "RoutePlanner",
    "RoutingAlternative",
    "RoutingChoice",
    "RoutingData",
    "RoutingDecision",
    "build_candidates",
    "infer_task_kind",
    "tier_for",
]
