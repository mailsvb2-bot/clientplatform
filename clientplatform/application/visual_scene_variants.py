from __future__ import annotations

"""Hybrid visual scene variants.

One text-AI call may propose up to five presentation directions. The immutable
VisualSceneContract remains the source of truth, so a variant can change HOW the
scene is staged but never WHAT the owner asked to depict.
"""

from dataclasses import dataclass, replace
import json
import os
from typing import Any

from clientplatform.application.visual_scene_planning import (
    grounded_scene_contract_from_mapping,
)
from clientplatform.domain.visual_scene_contract import (
    VisualSceneContract,
    fallback_scene_contract,
)
from clientplatform.domain.visual_style_intent import VisualStyleIntent
from services.ai.client import OpenAIClient


_VARIANT_COUNT = 5
_MAX_DIRECTION_CHARS = 700
_MAX_DESCRIPTION_CHARS = 360
_MAX_SUPPLEMENT_CHARS = 600
_COMPOSITION_ORDER = (
    "clear_story",
    "cinematic",
    "editorial",
    "focused",
    "sequential",
)
_ALLOWED_COMPOSITIONS = frozenset(_COMPOSITION_ORDER)


@dataclass(frozen=True, slots=True)
class VisualSceneVariant:
    id: str
    title: str
    description: str
    direction: str
    composition: str
    score: int
    source: str = "deterministic"
    user_supplement: str = ""

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "direction": self.direction,
            "composition": self.composition,
            "score": self.score,
            "source": self.source,
            "user_supplement": self.user_supplement,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "VisualSceneVariant":
        if not isinstance(value, dict) or set(value) != {
            "id", "title", "description", "direction", "composition",
            "score", "source", "user_supplement",
        }:
            raise ValueError("visual_scene_variant_invalid")
        variant_id = str(value.get("id") or "").strip()
        title = _clean(value.get("title"), limit=80)
        description = _clean(value.get("description"), limit=_MAX_DESCRIPTION_CHARS)
        direction = _clean(value.get("direction"), limit=1400)
        composition = str(value.get("composition") or "").strip().lower()
        source = str(value.get("source") or "").strip().lower()
        supplement = _clean(
            value.get("user_supplement"),
            limit=_MAX_SUPPLEMENT_CHARS,
            allow_empty=True,
        )
        score = int(value.get("score") or 0)
        if (
            variant_id not in {"v1", "v2", "v3", "v4", "v5"}
            or not title
            or not description
            or not direction
            or composition not in _ALLOWED_COMPOSITIONS
            or source not in {"ai", "deterministic"}
            or not 0 <= score <= 100
        ):
            raise ValueError("visual_scene_variant_invalid")
        return cls(
            id=variant_id,
            title=title,
            description=description,
            direction=direction,
            composition=composition,
            score=score,
            source=source,
            user_supplement=supplement,
        )


def _clean(value: object, *, limit: int, allow_empty: bool = False) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if not text and not allow_empty:
        raise ValueError("visual_scene_variant_invalid")
    if len(text) > limit or any(ord(char) < 32 for char in text):
        raise ValueError("visual_scene_variant_invalid")
    return text


def _fallback_directions(
    contract: VisualSceneContract,
) -> tuple[tuple[str, str, str], ...]:
    if contract.topology in {"transformation", "sequence", "replacement"}:
        fifth = (
            "Последовательная история",
            "Смысл читается по этапам слева направо; главный объект сохраняет узнаваемость.",
            "Use a clean left-to-right narrative progression. Preserve the same "
            "primary subject or environment across moments. Keep the causal event "
            "and requested change readable without labels or arrows.",
        )
    else:
        fifth = (
            "Контекстная история",
            "Главный объект показан в окружении, которое помогает сразу считать действие или идею.",
            "Use one coherent contextual scene. Let environment and physical "
            "interaction clarify the requested meaning without adding new claims.",
        )
    return (
        (
            "Ясная сюжетная сцена",
            "Максимально прямое прочтение идеи с первого взгляда, без декоративного шума.",
            "Prioritize immediate semantic readability. Keep the primary subject, "
            "requested action or relationship and result visually obvious in one glance.",
        ),
        (
            "Кинематографично",
            "Более атмосферная постановка с глубиной, естественным светом и ощущением кадра из истории.",
            "Stage the same required meaning cinematically with believable depth, "
            "natural motivated light and clear visual causality.",
        ),
        (
            "Редакционно",
            "Чистая аккуратная композиция, где главный смысл отделён от второстепенных деталей.",
            "Use a polished editorial composition with controlled detail and strong "
            "visual hierarchy. Keep all mandatory semantic evidence explicit.",
        ),
        (
            "Фокус на главном",
            "Минимум лишнего: главный объект, действие и результат занимают основное внимание.",
            "Keep the composition focused and uncluttered. Give the primary subject "
            "and mandatory interaction enough visual space to read unambiguously.",
        ),
        fifth,
    )


def _score_variant(
    *,
    contract: VisualSceneContract,
    composition: str,
    direction: str,
    style: VisualStyleIntent,
    index: int,
) -> int:
    score = 80
    if composition == "clear_story":
        score += 8
    if (
        contract.topology in {"transformation", "sequence", "replacement"}
        and composition == "sequential"
    ):
        score += 12
    if contract.topology == "static" and composition == "focused":
        score += 8
    if (
        style.composition in {"story_scene", "before_after", "transformation"}
        and composition in {"clear_story", "sequential"}
    ):
        score += 5
    if style.realism in {"photorealistic", "realistic"} and composition == "cinematic":
        score += 4
    if len(direction) > 620:
        score -= 8
    elif len(direction) > 480:
        score -= 3
    score -= index
    return max(0, min(score, 100))


def _safe_direction_for_composition(
    contract: VisualSceneContract,
    composition: str,
) -> str:
    if composition not in _ALLOWED_COMPOSITIONS:
        raise ValueError("visual_scene_variant_invalid")
    directions = _fallback_directions(contract)
    mapping = {
        name: direction
        for name, (_title, _description, direction) in zip(
            _COMPOSITION_ORDER,
            directions,
            strict=True,
        )
    }
    return mapping[composition]


def _fallback_variants(
    *,
    contract: VisualSceneContract,
    style: VisualStyleIntent,
) -> tuple[VisualSceneVariant, ...]:
    compositions = _COMPOSITION_ORDER
    variants: list[VisualSceneVariant] = []
    for index, ((title, description, direction), composition) in enumerate(
        zip(_fallback_directions(contract), compositions, strict=True),
        start=1,
    ):
        variants.append(
            VisualSceneVariant(
                id=f"v{index}",
                title=title,
                description=description,
                direction=_safe_direction_for_composition(contract, composition),
                composition=composition,
                score=_score_variant(
                    contract=contract,
                    composition=composition,
                    direction=direction,
                    style=style,
                    index=index,
                ),
            )
        )
    return tuple(variants)


def _parse_variant_items(
    items: object,
    *,
    contract: VisualSceneContract,
    style: VisualStyleIntent,
) -> tuple[VisualSceneVariant, ...] | None:
    if not isinstance(items, list) or len(items) != _VARIANT_COUNT:
        return None

    variants: list[VisualSceneVariant] = []
    seen_compositions: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or set(item) != {
            "title", "description", "direction", "composition",
        }:
            return None
        try:
            title = _clean(item["title"], limit=80)
            description = _clean(
                item["description"], limit=_MAX_DESCRIPTION_CHARS,
            )
            # Validate the model payload shape/size, but never trust provider-facing
            # prose from an LLM. The actual direction is reconstructed exclusively
            # from a deterministic composition token after semantic validation.
            _clean(item["direction"], limit=_MAX_DIRECTION_CHARS)
        except ValueError:
            return None
        composition = str(item["composition"] or "").strip().lower()
        if (
            composition not in _ALLOWED_COMPOSITIONS
            or composition in seen_compositions
        ):
            return None
        seen_compositions.add(composition)
        safe_direction = _safe_direction_for_composition(contract, composition)
        variants.append(
            VisualSceneVariant(
                id=f"v{index}",
                title=title,
                description=description,
                direction=safe_direction,
                composition=composition,
                score=_score_variant(
                    contract=contract,
                    composition=composition,
                    direction=safe_direction,
                    style=style,
                    index=index,
                ),
                source="ai",
            )
        )
    return tuple(variants)


def _parse_ai_variants(
    raw: str,
    *,
    contract: VisualSceneContract,
    style: VisualStyleIntent,
) -> tuple[VisualSceneVariant, ...] | None:
    try:
        value: Any = json.loads(str(raw or "").strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or set(value) != {"variants"}:
        return None
    return _parse_variant_items(
        value.get("variants"),
        contract=contract,
        style=style,
    )


def build_visual_scene_bundle(
    *,
    request: str,
    semantic_flags: tuple[str, ...],
    style_intent: VisualStyleIntent,
    client: OpenAIClient | None = None,
) -> tuple[VisualSceneContract, str, tuple[VisualSceneVariant, ...]]:
    """Build grounded semantics plus five directions with at most one AI call."""

    owner_request = " ".join(str(request or "").replace("\x00", " ").split()).strip()
    fallback_contract = fallback_scene_contract(
        request=owner_request,
        semantic_flags=semantic_flags,
    )
    fallback_variants = _fallback_variants(
        contract=fallback_contract,
        style=style_intent,
    )
    enabled = (
        str(os.getenv("VISUAL_SCENE_PLANNER_ENABLED", "1")).strip().lower()
        in {"1", "true", "yes", "on"}
        and str(os.getenv("VISUAL_SCENE_VARIANTS_ENABLED", "1")).strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if not enabled:
        return fallback_contract, "deterministic", fallback_variants

    selected = client or OpenAIClient.from_settings()
    if selected is None:
        return fallback_contract, "deterministic", fallback_variants

    system = (
        "You are ClientPlatform's visual scene director. Return one JSON object with "
        "exactly two keys: scene_contract and variants. scene_contract has keys "
        "topology, primary_subject, initial_state, actions, cause, transition, "
        "final_state, explicit_text. Every textual value inside scene_contract except "
        "topology MUST be copied verbatim as an exact contiguous span from the owner "
        "request; use empty string/list when absent. Never add synonyms or facts. "
        "topology is one of static, action, transformation, sequence, comparison, "
        "replacement. variants is exactly five presentation directions for that SAME "
        "meaning. Each variant has title, description, direction, composition. "
        "composition must use each value exactly once: clear_story, cinematic, "
        "editorial, focused, sequential. Variants may change only composition, camera, "
        "lighting, staging, atmosphere and rhythm; they must not remove, replace or "
        "contradict any semantic element. title/description are concise Russian text "
        "for the owner; direction is concise English art direction. Do not request "
        "visible internal labels such as BEFORE, AFTER or ACTION."
    )
    raw = selected.chat(
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "owner_request": owner_request[:1500],
                        "semantic_flags": list(semantic_flags),
                        "style": style_intent.to_mapping(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        temperature=0.35,
        max_tokens=2200,
    )
    try:
        value: Any = json.loads(str(raw or "").strip())
    except json.JSONDecodeError:
        return fallback_contract, "deterministic", fallback_variants
    if not isinstance(value, dict) or set(value) != {"scene_contract", "variants"}:
        return fallback_contract, "deterministic", fallback_variants
    raw_contract = value.get("scene_contract")
    if not isinstance(raw_contract, dict):
        return fallback_contract, "deterministic", fallback_variants
    contract = grounded_scene_contract_from_mapping(
        owner_request=owner_request,
        semantic_flags=semantic_flags,
        value=raw_contract,
    )
    if contract is None:
        return fallback_contract, "deterministic", fallback_variants
    variants = _parse_variant_items(
        value.get("variants"),
        contract=contract,
        style=style_intent,
    )
    if variants is None:
        return fallback_contract, "deterministic", fallback_variants
    return contract, "ai", variants


def build_visual_scene_variants(
    *,
    request: str,
    scene_contract: VisualSceneContract,
    style_intent: VisualStyleIntent,
    client: OpenAIClient | None = None,
) -> tuple[VisualSceneVariant, ...]:
    """Return exactly five safe presentation alternatives in one text-AI call."""

    fallback = _fallback_variants(contract=scene_contract, style=style_intent)
    if str(os.getenv("VISUAL_SCENE_VARIANTS_ENABLED", "1")).strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return fallback

    selected = client or OpenAIClient.from_settings()
    if selected is None:
        return fallback

    system = (
        "You are a visual scene director. Produce exactly five genuinely different "
        "presentation directions for the SAME immutable semantic contract. You may "
        "change composition, camera logic, staging, atmosphere and visual rhythm, "
        "but you MUST NOT remove, contradict or replace any subject, action, relation, "
        "cause, chronology, state change, required evidence or forbidden constraint "
        "from the contract. Do not invent facts, claims, brand lettering or new story "
        "events. Return JSON only with key variants. Each variant must contain title, "
        "description, direction, composition. composition values must be exactly one "
        "each of clear_story, cinematic, editorial, focused, sequential. title and "
        "description are short Russian user-facing text. direction is concise English "
        "provider-facing art direction. Internal labels such as BEFORE, AFTER or ACTION "
        "must never be requested as visible text."
    )
    payload = {
        "owner_request": " ".join(str(request or "").split())[:1500],
        "scene_contract": scene_contract.to_mapping(),
        "style": style_intent.to_mapping(),
    }
    raw = selected.chat(
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":"),
                ),
            },
        ],
        temperature=0.55,
        max_tokens=1800,
    )
    parsed = _parse_ai_variants(
        raw or "",
        contract=scene_contract,
        style=style_intent,
    )
    return parsed or fallback


def recommended_scene_variant(
    variants: tuple[VisualSceneVariant, ...],
) -> VisualSceneVariant:
    if len(variants) != _VARIANT_COUNT:
        raise ValueError("visual_scene_variants_invalid")
    return max(variants, key=lambda item: (item.score, -int(item.id[1:])))


def supplement_scene_variant(
    variant: VisualSceneVariant,
    supplement: str,
) -> VisualSceneVariant:
    addition = _clean(supplement, limit=_MAX_SUPPLEMENT_CHARS)
    direction = (
        variant.direction.rstrip(".")
        + ". Owner refinement: "
        + addition
        + ". Apply this only where compatible with the canonical semantic contract; "
        "the contract remains mandatory."
    )
    if len(direction) > 1400:
        raise ValueError("visual_scene_supplement_too_long")
    return replace(
        variant,
        direction=direction,
        description=(
            variant.description.rstrip(".")
            + ". Дополнение пользователя: "
            + addition
        )[:_MAX_DESCRIPTION_CHARS],
        user_supplement=addition,
    )


__all__ = [
    "VisualSceneVariant",
    "build_visual_scene_bundle",
    "build_visual_scene_variants",
    "recommended_scene_variant",
    "supplement_scene_variant",
]
