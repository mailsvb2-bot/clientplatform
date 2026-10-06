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


_SCENE_BUNDLE_VERSION = 1
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


def _fallback_variant_copy(
    *,
    contract: VisualSceneContract,
    owner_request: str,
) -> tuple[tuple[str, str], ...]:
    """Build user-facing alternatives around the actual requested meaning.

    Deterministic fallback must never masquerade generic style presets as scene
    alternatives.  It may not invent facts, so it anchors every explanation in
    the owner's own bounded request and the grounded subject/topology.
    """

    concept = " ".join(str(owner_request or "").split()).strip()[:240]
    subject = str(contract.primary_subject or "").strip() or "главный объект"
    quoted = f"«{concept}»" if concept else "исходный смысл запроса"

    if contract.topology in {"transformation", "sequence", "replacement"}:
        return (
            (
                "Причина и результат в одном кадре",
                f"{subject.capitalize()} остаётся одним и тем же героем: исходное состояние, "
                f"причинное действие и заметный результат {quoted} читаются в одной сцене.",
            ),
            (
                "История через ключевой момент",
                f"Главный акцент — на действии, которое запускает изменение {quoted}; "
                "окружение и поза героя показывают, откуда началось и к чему пришло.",
            ),
            (
                "Три связанных этапа",
                f"Один и тот же {subject}: начало → причинное действие → итог. "
                f"Все три момента визуально связаны и раскрывают именно {quoted}.",
            ),
            (
                "Крупный фокус на изменении",
                f"Камера держится ближе к {subject}: действие и физически заметная перемена "
                f"становятся главным доказательством смысла {quoted}.",
            ),
            (
                "Последовательность без подписей",
                f"Смысл {quoted} читается слева направо без стрелок и поясняющего текста: "
                "тот же герой, причина изменения и различимый финальный результат.",
            ),
        )

    return (
        (
            "Смысл одним кадром",
            f"Один ясный кадр, где {subject} и главное действие напрямую раскрывают {quoted}.",
        ),
        (
            "Действие в живой сцене",
            f"{subject.capitalize()} показан в естественном окружении; контекст помогает "
            f"сразу понять действие и смысл {quoted}.",
        ),
        (
            "Чистая смысловая композиция",
            f"Второстепенные детали убраны, а визуальная иерархия подчёркивает именно {quoted}.",
        ),
        (
            "Крупный фокус на главном",
            f"Более близкий кадр: {subject}, его действие и нужное взаимодействие занимают "
            f"основное внимание и раскрывают {quoted}.",
        ),
        (
            "Контекстная история",
            f"Окружение и взаимодействия дают больше контекста, но сохраняют неизменным "
            f"главный смысл {quoted}.",
        ),
    )


def _fallback_variants(
    *,
    contract: VisualSceneContract,
    style: VisualStyleIntent,
    owner_request: str = "",
) -> tuple[VisualSceneVariant, ...]:
    compositions = _COMPOSITION_ORDER
    copy = _fallback_variant_copy(
        contract=contract,
        owner_request=owner_request,
    )
    variants: list[VisualSceneVariant] = []
    for index, (((_generic_title, _generic_description, base_direction), composition), (title, description)) in enumerate(
        zip(
            zip(_fallback_directions(contract), compositions, strict=True),
            copy,
            strict=True,
        ),
        start=1,
    ):
        concept = " ".join(str(owner_request or "").split()).strip()[:700]
        direction = _safe_direction_for_composition(contract, composition)
        if concept:
            direction = (
                direction.rstrip(".")
                + ". Preserve this exact owner concept throughout the composition: "
                + concept
                + "."
            )
        variants.append(
            VisualSceneVariant(
                id=f"v{index}",
                title=title,
                description=description[:_MAX_DESCRIPTION_CHARS],
                direction=direction[:1400],
                composition=composition,
                score=_score_variant(
                    contract=contract,
                    composition=composition,
                    direction=base_direction,
                    style=style,
                    index=index,
                ),
            )
        )
    return tuple(variants)


_UNSAFE_DIRECTION_PHRASES = (
    "replace ",
    "remove ",
    "ignore ",
    "instead of",
    "different animal",
    "different subject",
    "different object",
    "swap ",
    "substitute ",
)


def _safe_ai_direction_text(direction: str) -> str | None:
    folded = " ".join(direction.casefold().split())
    if any(phrase in folded for phrase in _UNSAFE_DIRECTION_PHRASES):
        return None
    return direction


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
        required = {"title", "description", "direction", "composition"}
        if not isinstance(item, dict) or not required.issubset(item):
            return None
        try:
            title = _clean(item["title"], limit=80)
            description = _clean(
                item["description"], limit=_MAX_DESCRIPTION_CHARS,
            )
            ai_direction = _clean(item["direction"], limit=_MAX_DIRECTION_CHARS)
        except ValueError:
            return None
        composition = str(item["composition"] or "").strip().lower()
        if (
            composition not in _ALLOWED_COMPOSITIONS
            or composition in seen_compositions
        ):
            return None
        seen_compositions.add(composition)
        semantic_guard = _safe_direction_for_composition(contract, composition)
        safe_ai_direction = _safe_ai_direction_text(ai_direction)
        direction = (
            (
                safe_ai_direction.rstrip(".")
                + ". "
                + semantic_guard
            )
            if safe_ai_direction
            else semantic_guard
        )[:1400]
        variants.append(
            VisualSceneVariant(
                id=f"v{index}",
                title=title,
                description=description,
                direction=direction,
                composition=composition,
                score=_score_variant(
                    contract=contract,
                    composition=composition,
                    direction=direction,
                    style=style,
                    index=index,
                ),
                source="ai",
            )
        )
    return tuple(variants)


def _json_object_from_model(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().lower() in {"```", "```json"}:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _parse_ai_variants(
    raw: str,
    *,
    contract: VisualSceneContract,
    style: VisualStyleIntent,
) -> tuple[VisualSceneVariant, ...] | None:
    value = _json_object_from_model(raw)
    if value is None or "variants" not in value:
        return None
    return _parse_variant_items(
        value.get("variants"),
        contract=contract,
        style=style,
    )


def freeze_visual_scene_bundle(
    *,
    scene_contract: VisualSceneContract,
    planner_source: str,
    variants: tuple[VisualSceneVariant, ...],
) -> str:
    source = str(planner_source or "").strip().lower()
    if source not in {"ai", "deterministic"} or len(variants) != _VARIANT_COUNT:
        raise ValueError("visual_scene_bundle_invalid")
    payload = {
        "version": _SCENE_BUNDLE_VERSION,
        "scene_contract": scene_contract.to_mapping(),
        "planner_source": source,
        "variants": [item.to_mapping() for item in variants],
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def load_visual_scene_bundle(
    raw: str,
) -> tuple[VisualSceneContract, str, tuple[VisualSceneVariant, ...]]:
    try:
        payload: Any = json.loads(str(raw or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("visual_scene_bundle_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "scene_contract",
        "planner_source",
        "variants",
    }:
        raise ValueError("visual_scene_bundle_invalid")
    if payload.get("version") != _SCENE_BUNDLE_VERSION:
        raise ValueError("visual_scene_bundle_invalid")
    contract = VisualSceneContract.from_mapping(payload.get("scene_contract"))
    source = str(payload.get("planner_source") or "").strip().lower()
    raw_variants = payload.get("variants")
    if source not in {"ai", "deterministic"} or not isinstance(raw_variants, list):
        raise ValueError("visual_scene_bundle_invalid")
    variants = tuple(VisualSceneVariant.from_mapping(item) for item in raw_variants)
    if len(variants) != _VARIANT_COUNT:
        raise ValueError("visual_scene_bundle_invalid")
    return contract, source, variants


def visual_scene_ai_planning_available() -> bool:
    enabled = (
        str(os.getenv("VISUAL_SCENE_PLANNER_ENABLED", "1")).strip().lower()
        in {"1", "true", "yes", "on"}
        and str(os.getenv("VISUAL_SCENE_VARIANTS_ENABLED", "1")).strip().lower()
        in {"1", "true", "yes", "on"}
    )
    return enabled and OpenAIClient.from_settings() is not None


def deterministic_visual_scene_bundle(
    *,
    request: str,
    semantic_flags: tuple[str, ...],
    style_intent: VisualStyleIntent,
) -> tuple[VisualSceneContract, str, tuple[VisualSceneVariant, ...]]:
    owner_request = " ".join(
        str(request or "").replace("\x00", " ").split()
    ).strip()
    contract = fallback_scene_contract(
        request=owner_request,
        semantic_flags=semantic_flags,
    )
    variants = _fallback_variants(
        contract=contract,
        style=style_intent,
        owner_request=owner_request,
    )
    return contract, "deterministic", variants


def build_visual_scene_bundle(
    *,
    request: str,
    semantic_flags: tuple[str, ...],
    style_intent: VisualStyleIntent,
    client: OpenAIClient | None = None,
) -> tuple[VisualSceneContract, str, tuple[VisualSceneVariant, ...]]:
    """Build one grounded scene bundle with at most one text-AI call.

    The deterministic contract is always available as the semantic safety net.
    AI may improve the grounded contract when it returns exact owner spans and may
    propose staging variants, but a malformed contract no longer discards otherwise
    valid presentation work.
    """

    owner_request = " ".join(str(request or "").replace("\x00", " ").split()).strip()
    fallback_contract, _fallback_source, fallback_variants = deterministic_visual_scene_bundle(
        request=owner_request,
        semantic_flags=semantic_flags,
        style_intent=style_intent,
    )
    if client is None and not visual_scene_ai_planning_available():
        return fallback_contract, "deterministic", fallback_variants

    selected = client or OpenAIClient.from_settings()
    if selected is None:
        return fallback_contract, "deterministic", fallback_variants

    system = (
        "You are ClientPlatform's visual scene director. Return JSON with a variants "
        "array of exactly five genuinely different presentation directions for the "
        "same owner meaning. You may also return scene_contract. If scene_contract is "
        "returned, its textual semantic values must be exact contiguous spans copied "
        "from the owner request; never invent synonyms or facts. Each variant has "
        "title, description, direction, composition. composition must use each value "
        "exactly once: clear_story, cinematic, editorial, focused, sequential. "
        "direction may change only camera, staging, lighting, atmosphere, visual "
        "rhythm and composition; never replace, remove, ignore or contradict the "
        "requested subject/action/result. Return JSON only."
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
                        "scene_contract": fallback_contract.to_mapping(),
                        "style": style_intent.to_mapping(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        temperature=0.55,
        max_tokens=2200,
    )

    value = _json_object_from_model(raw or "")
    if value is None:
        return fallback_contract, "deterministic", fallback_variants

    contract = fallback_contract
    raw_contract = value.get("scene_contract")
    if isinstance(raw_contract, dict):
        grounded = grounded_scene_contract_from_mapping(
            owner_request=owner_request,
            semantic_flags=semantic_flags,
            value=raw_contract,
        )
        if grounded is not None:
            contract = grounded

    variants = _parse_variant_items(
        value.get("variants"),
        contract=contract,
        style=style_intent,
    )
    if variants is None:
        return contract, "deterministic", _fallback_variants(
            contract=contract,
            style=style_intent,
            owner_request=owner_request,
        )
    return contract, "ai", variants

def build_visual_scene_variants(
    *,
    request: str,
    scene_contract: VisualSceneContract,
    style_intent: VisualStyleIntent,
    client: OpenAIClient | None = None,
) -> tuple[VisualSceneVariant, ...]:
    """Return exactly five safe presentation alternatives in one text-AI call."""

    fallback = _fallback_variants(
        contract=scene_contract,
        style=style_intent,
        owner_request=request,
    )
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
    "deterministic_visual_scene_bundle",
    "freeze_visual_scene_bundle",
    "load_visual_scene_bundle",
    "recommended_scene_variant",
    "supplement_scene_variant",
    "visual_scene_ai_planning_available",
]
