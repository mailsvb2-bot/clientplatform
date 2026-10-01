from __future__ import annotations

"""Structured visual-style intent for ordinary owner-authored image/video requests."""

from dataclasses import dataclass, replace
import json
import re
from typing import Mapping


STYLE_SCHEMA_VERSION = 2
SUPPORTED_STYLE_SCHEMA_VERSIONS = frozenset({1, 2})

_QUICK_STYLE_ORDER = (
    "warm_friendly",
    "soft_calm",
    "bright_energy",
    "premium",
    "cinematic",
    "illustrative",
    "natural_photo",
)
_QUICK_STYLE_SET = frozenset(_QUICK_STYLE_ORDER)
_QUICK_STYLE_LABELS_RU = {
    "warm_friendly": "тёпло и дружелюбно",
    "soft_calm": "мягко и спокойно",
    "bright_energy": "ярко и энергично",
    "premium": "премиально",
    "cinematic": "кинематографично",
    "illustrative": "художественно",
    "natural_photo": "натуральное фото",
}
_QUICK_STYLE_DIRECTIVES = {
    "warm_friendly": "Blend in a warm, welcoming and approachable visual character.",
    "soft_calm": "Blend in a soft, calm and reassuring visual character.",
    "bright_energy": "Blend in a bright, energetic and lively visual character.",
    "premium": "Blend in a refined premium feel with restrained, polished visual cues.",
    "cinematic": "Blend in a cinematic, story-driven visual treatment with deliberate framing.",
    "illustrative": "Blend in an artistic, crafted visual treatment rather than a generic stock look.",
    "natural_photo": "Blend in a natural photographic feel with believable, unforced details.",
}

_ALLOWED = {
    "color_temperature": {"auto", "warm", "neutral", "cool"},
    "emotional_tone": {
        "auto",
        "friendly",
        "calm",
        "bold",
        "dramatic",
        "aggressive",
        "playful",
        "premium",
    },
    "energy": {"auto", "low", "medium", "high"},
    "realism": {
        "auto",
        "photorealistic",
        "realistic",
        "semi_stylized",
        "illustrative",
    },
    "lighting": {"auto", "bright", "balanced", "dark"},
    "contrast": {"auto", "soft", "balanced", "strong"},
    "detail": {"auto", "minimal", "balanced", "detailed"},
    "composition": {
        "auto",
        "close_up",
        "medium",
        "wide",
        "story_scene",
        "before_after",
        "transformation",
    },
    "motion": {"auto", "static", "gentle", "dynamic"},
    "commercial_tone": {
        "auto",
        "natural",
        "friendly",
        "professional",
        "premium",
        "promotional",
    },
    "copy_space": {"auto", "none", "small", "medium", "large"},
}


@dataclass(frozen=True, slots=True)
class VisualStyleIntent:
    color_temperature: str = "auto"
    emotional_tone: str = "auto"
    energy: str = "auto"
    realism: str = "auto"
    lighting: str = "auto"
    contrast: str = "auto"
    detail: str = "auto"
    composition: str = "auto"
    motion: str = "auto"
    commercial_tone: str = "auto"
    copy_space: str = "auto"
    quick_styles: str = "auto"

    def quick_style_names(self) -> tuple[str, ...]:
        raw = str(self.quick_styles or "auto").strip().lower()
        if not raw or raw == "auto":
            return ()
        requested = {item.strip() for item in raw.split(",") if item.strip()}
        unknown = requested - _QUICK_STYLE_SET
        if unknown:
            raise ValueError("visual quick style is invalid")
        return tuple(name for name in _QUICK_STYLE_ORDER if name in requested)

    def has_quick_style(self, name: str) -> bool:
        token = str(name or "").strip().lower()
        if token not in _QUICK_STYLE_SET:
            raise ValueError("visual quick style is invalid")
        return token in self.quick_style_names()

    def with_quick_style(
        self,
        name: str,
        *,
        enabled: bool | None = None,
    ) -> "VisualStyleIntent":
        token = str(name or "").strip().lower()
        if token not in _QUICK_STYLE_SET:
            raise ValueError("visual quick style is invalid")
        selected = set(self.quick_style_names())
        should_enable = token not in selected if enabled is None else bool(enabled)
        if should_enable:
            selected.add(token)
        else:
            selected.discard(token)
        raw = ",".join(
            item for item in _QUICK_STYLE_ORDER if item in selected
        ) or "auto"
        return replace(self, quick_styles=raw).normalized()

    def normalized(self) -> "VisualStyleIntent":
        values: dict[str, str] = {}
        for field, allowed in _ALLOWED.items():
            value = str(getattr(self, field, "auto") or "auto").strip().lower()
            if value not in allowed:
                raise ValueError(f"visual style {field} is invalid")
            values[field] = value
        quick_styles = ",".join(self.quick_style_names()) or "auto"
        return VisualStyleIntent(**values, quick_styles=quick_styles)

    def to_mapping(self) -> dict[str, str]:
        value = self.normalized()
        result = {field: getattr(value, field) for field in _ALLOWED}
        result["quick_styles"] = value.quick_styles
        return result

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": STYLE_SCHEMA_VERSION,
                "style": self.to_mapping(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> "VisualStyleIntent":
        if value is None:
            return cls()
        unknown = set(value) - set(_ALLOWED) - {"quick_styles"}
        if unknown:
            raise ValueError("visual style contains unknown fields")
        return cls(
            **{
                field: str(value.get(field) or "auto")
                for field in _ALLOWED
            },
            quick_styles=str(value.get("quick_styles") or "auto"),
        ).normalized()

    @classmethod
    def from_json(cls, raw: str) -> "VisualStyleIntent":
        try:
            payload = json.loads(str(raw or ""))
        except json.JSONDecodeError as exc:
            raise ValueError("visual style json is invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("visual style json is invalid")
        version = payload.get("version")
        if version not in SUPPORTED_STYLE_SCHEMA_VERSIONS:
            raise ValueError("visual style version is unsupported")
        style = payload.get("style")
        if not isinstance(style, dict):
            raise ValueError("visual style payload is invalid")
        if version == 1 and "quick_styles" in style:
            raise ValueError("visual style version one cannot contain quick styles")
        return cls.from_mapping(style)

    def with_value(self, field: str, value: str) -> "VisualStyleIntent":
        key = str(field or "").strip()
        if key not in _ALLOWED:
            raise ValueError("visual style field is invalid")
        token = str(value or "").strip().lower()
        if token not in _ALLOWED[key]:
            raise ValueError("visual style value is invalid")
        return replace(self, **{key: token}).normalized()


@dataclass(frozen=True, slots=True)
class VisualStyleInference:
    intent: VisualStyleIntent
    explicit_fields: tuple[str, ...]


_PRESETS: dict[str, VisualStyleIntent] = {
    "warm_friendly": VisualStyleIntent(
        color_temperature="warm",
        emotional_tone="friendly",
        energy="medium",
        realism="realistic",
        lighting="bright",
        contrast="soft",
        detail="balanced",
        commercial_tone="friendly",
    ),
    "soft_calm": VisualStyleIntent(
        color_temperature="warm",
        emotional_tone="calm",
        energy="low",
        realism="realistic",
        lighting="bright",
        contrast="soft",
        detail="balanced",
        motion="gentle",
        commercial_tone="natural",
    ),
    "bright_energy": VisualStyleIntent(
        color_temperature="neutral",
        emotional_tone="bold",
        energy="high",
        realism="realistic",
        lighting="bright",
        contrast="strong",
        detail="balanced",
        motion="dynamic",
        commercial_tone="promotional",
    ),
    "premium": VisualStyleIntent(
        color_temperature="neutral",
        emotional_tone="premium",
        energy="low",
        realism="photorealistic",
        lighting="balanced",
        contrast="strong",
        detail="detailed",
        commercial_tone="premium",
    ),
    "cinematic": VisualStyleIntent(
        color_temperature="cool",
        emotional_tone="dramatic",
        energy="medium",
        realism="realistic",
        lighting="dark",
        contrast="strong",
        detail="detailed",
        motion="gentle",
        commercial_tone="natural",
    ),
    "illustrative": VisualStyleIntent(
        color_temperature="warm",
        emotional_tone="playful",
        energy="medium",
        realism="illustrative",
        lighting="bright",
        contrast="balanced",
        detail="detailed",
        commercial_tone="friendly",
    ),
    "natural_photo": VisualStyleIntent(
        color_temperature="neutral",
        emotional_tone="friendly",
        energy="low",
        realism="photorealistic",
        lighting="balanced",
        contrast="soft",
        detail="balanced",
        commercial_tone="natural",
    ),
}


def visual_style_preset(name: str) -> VisualStyleIntent:
    try:
        return _PRESETS[str(name or "").strip().lower()]
    except KeyError as exc:
        raise ValueError("visual style preset is invalid") from exc


def merge_visual_style(
    base: VisualStyleIntent | None,
    override: VisualStyleIntent | None,
    *,
    override_fields: tuple[str, ...] | None = None,
) -> VisualStyleIntent:
    current = (base or VisualStyleIntent()).normalized()
    incoming = (override or VisualStyleIntent()).normalized()
    selected = set(_ALLOWED if override_fields is None else override_fields)
    values = current.to_mapping()
    for field in selected:
        if field not in _ALLOWED:
            continue
        value = getattr(incoming, field)
        if value != "auto":
            values[field] = value
    if override_fields is None and incoming.quick_styles != "auto":
        values["quick_styles"] = incoming.quick_styles
    return VisualStyleIntent.from_mapping(values)


def _match_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def infer_visual_style_intent(request: str) -> VisualStyleInference:
    text = " ".join(str(request or "").casefold().split())
    values = VisualStyleIntent().to_mapping()
    explicit: list[str] = []

    rules: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("color_temperature", "warm", (r"\bт[её]пл\w*", r"\bwarm\w*")),
        ("color_temperature", "cool", (r"\bхолодн\w*", r"\bcool\b", r"\bcold\b")),
        ("color_temperature", "neutral", (r"\bнейтральн\w*", r"\bneutral\b")),
        (
            "emotional_tone",
            "friendly",
            (
                r"\bдоброжелательн\w*",
                r"\bдружелюбн\w*(?:\s+\w+){0,3}\s+(?:сцен|картин|визуал|атмосфер)\w*",
                r"\bfriendly\s+(?:scene|image|visual|mood)\b",
            ),
        ),
        (
            "emotional_tone",
            "calm",
            (
                r"\bспокойн\w*(?:\s+\w+){0,3}\s+(?:сцен|картин|фотограф|визуал|атмосфер)\w*",
                r"\bумиротвор[её]нн\w*",
                r"\bcalm\s+(?:scene|image|visual|mood)\b",
            ),
        ),
        ("emotional_tone", "dramatic", (r"\bдрамат\w*", r"\bdramatic\w*", r"\bcinematic\w*")),
        ("emotional_tone", "aggressive", (r"\bагрессив\w*", r"\baggressive\w*")),
        ("emotional_tone", "playful", (r"\bигрив\w*", r"\bвес[её]л\w*", r"\bplayful\w*")),
        ("emotional_tone", "premium", (r"\bпремиальн\w*", r"\bстатусн\w*", r"\bpremium\w*")),
        ("energy", "low", (r"\bмедленн\w*", r"\bрасслаб\w*", r"\blow[- ]energy\b")),
        ("energy", "high", (r"\bэнергич\w*", r"\bдинамич\w*", r"\bhigh[- ]energy\b")),
        ("realism", "photorealistic", (r"\bфотореал\w*", r"\bphotoreal\w*")),
        ("realism", "realistic", (r"\bреалистич\w*", r"\bфотограф\w*", r"\brealistic\w*")),
        ("realism", "illustrative", (r"\bиллюстрац\w*", r"\bрисован\w*", r"\billustrat\w*")),
        ("lighting", "bright", (r"\bсветл\w*", r"\bярк(?:ий|ая|ое|о)\b", r"\bbright\w*")),
        ("lighting", "dark", (r"\bт[её]мн\w*", r"\bmрачн\w*", r"\bdark\w*")),
        (
            "contrast",
            "soft",
            (
                r"\bмягк\w*(?:\s+\w+){0,3}\s+(?:свет|контраст|тон|палитр|цвет|сцен|картин|визуал)\w*",
                r"\bsoft\s+(?:light|lighting|contrast|tones?|palette|image|visual)\b",
                r"\blow[- ]contrast\b",
            ),
        ),
        ("contrast", "strong", (r"\bконтрастн\w*", r"\bstrong[- ]contrast\b")),
        ("detail", "minimal", (r"\bминималист\w*", r"\bminimal\w*")),
        ("detail", "detailed", (r"\bдетальн\w*", r"\bподробн\w*", r"\bdetailed\w*")),
        ("composition", "close_up", (r"\bкрупн(?:ый|ым)\s+план\w*", r"\bclose[- ]up\b")),
        ("composition", "wide", (r"\bобщ(?:ий|им)\s+план\w*", r"\bwide\s+shot\b")),
        ("composition", "story_scene", (r"\bсюжетн\w*", r"\bstory\s+scene\b")),
        ("composition", "before_after", (r"\bдо\s*(?:и|/)\s*после\b", r"\bbefore\s+and\s+after\b")),
        ("motion", "dynamic", (r"\bдинамич\w*", r"\bдвижен\w*", r"\bdynamic\w*")),
        ("motion", "gentle", (r"\bплавн\w*", r"\bмягк(?:ое|ое)\s+движен\w*", r"\bgentle\s+motion\b")),
        ("copy_space", "none", (r"\bбез\s+текста\b", r"\bno\s+text\b")),
        ("copy_space", "medium", (r"\bместо\s+(?:под|для)\s+текст\w*", r"\bcopy\s+space\b")),
    )
    for field, value, patterns in rules:
        if field in explicit:
            continue
        if _match_any(text, patterns):
            values[field] = value
            explicit.append(field)

    return VisualStyleInference(
        intent=VisualStyleIntent.from_mapping(values),
        explicit_fields=tuple(explicit),
    )


def resolve_visual_style_intent(
    *,
    request: str,
    saved: VisualStyleIntent | None = None,
    selected: VisualStyleIntent | None = None,
) -> VisualStyleIntent:
    base = (saved or VisualStyleIntent()).normalized()
    inferred = infer_visual_style_intent(request)
    current = merge_visual_style(
        base,
        inferred.intent,
        override_fields=inferred.explicit_fields,
    )
    return merge_visual_style(current, selected)


def visual_style_prompt_directives(
    intent: VisualStyleIntent,
    *,
    kind: str,
) -> tuple[str, ...]:
    value = intent.normalized()
    labels = {
        "color_temperature": {
            "warm": "Use a warm color temperature.",
            "neutral": "Use a neutral color temperature.",
            "cool": "Use a cool color temperature.",
        },
        "emotional_tone": {
            "friendly": "The emotional tone should feel friendly and approachable.",
            "calm": "The emotional tone should feel calm and reassuring.",
            "bold": "The emotional tone should feel bold and assertive.",
            "dramatic": "The emotional tone should feel dramatic and cinematic.",
            "aggressive": "The emotional tone should feel intentionally aggressive and forceful.",
            "playful": "The emotional tone should feel playful and lively.",
            "premium": "The emotional tone should feel premium, restrained and status-oriented.",
        },
        "energy": {
            "low": "Keep visual energy low and unhurried.",
            "medium": "Use balanced medium visual energy.",
            "high": "Use high visual energy and strong momentum.",
        },
        "realism": {
            "photorealistic": "Use a photorealistic visual language.",
            "realistic": "Use a credible realistic visual language.",
            "semi_stylized": "Use a semi-stylized but believable visual language.",
            "illustrative": "Use a clearly illustrative artistic visual language.",
        },
        "lighting": {
            "bright": "Use bright, open lighting.",
            "balanced": "Use balanced natural lighting.",
            "dark": "Use intentionally dark, moody lighting.",
        },
        "contrast": {
            "soft": "Use soft contrast and gentle tonal transitions.",
            "balanced": "Use balanced contrast.",
            "strong": "Use strong visual contrast.",
        },
        "detail": {
            "minimal": "Keep the scene visually minimal and uncluttered.",
            "balanced": "Use a balanced amount of visual detail.",
            "detailed": "Use rich but coherent visual detail.",
        },
        "composition": {
            "close_up": "Use a close-up composition.",
            "medium": "Use a medium-shot composition.",
            "wide": "Use a wide composition with readable environment.",
            "story_scene": "Use a narrative story-scene composition.",
            "before_after": "Use an explicit before/after composition.",
            "transformation": "Use a continuous transformation-focused composition.",
        },
        "motion": {
            "static": "Keep motion static and composed.",
            "gentle": "Use gentle, smooth motion." if kind == "video" else "Suggest gentle, flowing movement.",
            "dynamic": "Use dynamic motion and energetic framing." if kind == "video" else "Suggest strong dynamic movement.",
        },
        "commercial_tone": {
            "natural": "Keep the commercial presentation natural and non-salesy.",
            "friendly": "Keep the commercial presentation friendly and accessible.",
            "professional": "Keep the commercial presentation professional and trustworthy.",
            "premium": "Keep the commercial presentation premium and restrained.",
            "promotional": "Use a more promotional advertising presentation without manipulative urgency.",
        },
        "copy_space": {
            "none": "Do not reserve empty copy space.",
            "small": "Reserve only a small intentional area for later typography.",
            "medium": "Reserve a balanced intentional area for later typography.",
            "large": "Reserve a large intentional area for later typography while keeping the image visually complete.",
        },
    }
    lines: list[str] = []
    quick_styles = value.quick_style_names()
    if quick_styles:
        lines.append(
            "Combine every selected quick style accent coherently; blend overlapping "
            "qualities instead of dropping one."
        )
        lines.extend(_QUICK_STYLE_DIRECTIVES[name] for name in quick_styles)
    for field in _ALLOWED:
        token = getattr(value, field)
        if token == "auto":
            continue
        directive = labels.get(field, {}).get(token)
        if directive:
            lines.append(directive)
    return tuple(lines)


def style_summary_ru(intent: VisualStyleIntent) -> str:
    value = intent.normalized()
    labels = {
        "color_temperature": {"auto": "авто", "warm": "тёплая", "neutral": "нейтральная", "cool": "холодная"},
        "emotional_tone": {
            "auto": "авто", "friendly": "доброжелательная", "calm": "спокойная",
            "bold": "дерзкая", "dramatic": "драматичная", "aggressive": "агрессивная",
            "playful": "игривая", "premium": "статусная",
        },
        "energy": {"auto": "авто", "low": "спокойная", "medium": "средняя", "high": "энергичная"},
        "realism": {
            "auto": "авто", "photorealistic": "фотореализм", "realistic": "реалистично",
            "semi_stylized": "полустилизация", "illustrative": "иллюстрация",
        },
        "lighting": {"auto": "авто", "bright": "светло", "balanced": "сбалансировано", "dark": "темно"},
        "contrast": {"auto": "авто", "soft": "мягкий", "balanced": "сбалансированный", "strong": "сильный"},
        "detail": {"auto": "авто", "minimal": "минимум", "balanced": "сбалансировано", "detailed": "детально"},
        "composition": {
            "auto": "авто", "close_up": "крупный план", "medium": "средний план",
            "wide": "общий план", "story_scene": "сюжетная сцена",
            "before_after": "до / после", "transformation": "плавное превращение",
        },
        "motion": {"auto": "авто", "static": "статично", "gentle": "плавно", "dynamic": "динамично"},
        "commercial_tone": {
            "auto": "авто", "natural": "натурально", "friendly": "дружелюбно",
            "professional": "профессионально", "premium": "премиально", "promotional": "рекламно",
        },
        "copy_space": {"auto": "авто", "none": "без места", "small": "немного", "medium": "средне", "large": "много"},
    }
    shown = (
        ("Гамма", "color_temperature"),
        ("Настроение", "emotional_tone"),
        ("Динамика", "energy"),
        ("Вид", "realism"),
        ("Свет", "lighting"),
        ("Контраст", "contrast"),
        ("Композиция", "composition"),
        ("Место под текст", "copy_space"),
    )
    lines = [
        f"{title}: {labels[field][getattr(value, field)]}"
        for title, field in shown
    ]
    quick_styles = value.quick_style_names()
    if quick_styles:
        lines.insert(
            0,
            "Акценты: "
            + ", ".join(_QUICK_STYLE_LABELS_RU[name] for name in quick_styles),
        )
    return "\n".join(lines)


__all__ = [
    "STYLE_SCHEMA_VERSION",
    "SUPPORTED_STYLE_SCHEMA_VERSIONS",
    "VisualStyleInference",
    "VisualStyleIntent",
    "infer_visual_style_intent",
    "merge_visual_style",
    "resolve_visual_style_intent",
    "style_summary_ru",
    "visual_style_preset",
    "visual_style_prompt_directives",
]
