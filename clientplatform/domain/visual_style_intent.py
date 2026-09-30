from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import re
from typing import Mapping


STYLE_SCHEMA_VERSION = 1

_ALLOWED: dict[str, frozenset[str]] = {
    "color_temperature": frozenset({"auto", "warm", "neutral", "cool"}),
    "emotional_tone": frozenset(
        {"auto", "friendly", "calm", "playful", "bold", "dramatic", "aggressive", "premium"}
    ),
    "energy": frozenset({"auto", "low", "medium", "high"}),
    "realism": frozenset(
        {"auto", "photorealistic", "realistic", "semi_stylized", "illustrative"}
    ),
    "lighting": frozenset({"auto", "bright", "balanced", "dark"}),
    "contrast": frozenset({"auto", "soft", "balanced", "strong"}),
    "detail": frozenset({"auto", "minimal", "balanced", "detailed"}),
    "composition": frozenset(
        {"auto", "close_up", "medium", "wide", "story_scene", "before_after", "transformation"}
    ),
    "motion": frozenset({"auto", "static", "gentle", "dynamic"}),
    "commercial_tone": frozenset(
        {"auto", "natural", "friendly", "professional", "premium", "promotional"}
    ),
    "copy_space": frozenset({"auto", "none", "small", "medium", "large"}),
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

    def normalized(self) -> "VisualStyleIntent":
        values: dict[str, str] = {}
        for field, allowed in _ALLOWED.items():
            token = str(getattr(self, field) or "auto").strip().lower()
            if token not in allowed:
                raise ValueError(f"visual_style_{field}_invalid")
            values[field] = token
        return VisualStyleIntent(**values)

    def to_mapping(self) -> dict[str, str]:
        return asdict(self.normalized())

    def to_json(self) -> str:
        return json.dumps(
            {"version": STYLE_SCHEMA_VERSION, "style": self.to_mapping()},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def is_auto(self) -> bool:
        return all(value == "auto" for value in self.to_mapping().values())

    def with_choice(self, field: str, value: str) -> "VisualStyleIntent":
        if field not in _ALLOWED:
            raise ValueError("visual_style_field_invalid")
        token = str(value or "").strip().lower()
        if token not in _ALLOWED[field]:
            raise ValueError("visual_style_value_invalid")
        return replace(self, **{field: token}).normalized()


def visual_style_from_mapping(value: Mapping[str, object] | None) -> VisualStyleIntent:
    if not value:
        return VisualStyleIntent()
    unknown = set(value) - set(_ALLOWED)
    if unknown:
        raise ValueError("visual_style_unknown_fields")
    kwargs = {field: str(value.get(field) or "auto") for field in _ALLOWED}
    return VisualStyleIntent(**kwargs).normalized()


def visual_style_from_json(raw: str) -> VisualStyleIntent:
    try:
        payload = json.loads(str(raw or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("visual_style_json_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "style"}:
        raise ValueError("visual_style_json_invalid")
    if payload.get("version") != STYLE_SCHEMA_VERSION:
        raise ValueError("visual_style_version_invalid")
    style = payload.get("style")
    if not isinstance(style, dict):
        raise ValueError("visual_style_json_invalid")
    return visual_style_from_mapping(style)


_PRESETS: dict[str, VisualStyleIntent] = {
    "warm_friendly": VisualStyleIntent(
        color_temperature="warm",
        emotional_tone="friendly",
        energy="medium",
        realism="realistic",
        lighting="bright",
        contrast="soft",
        commercial_tone="friendly",
    ),
    "soft_calm": VisualStyleIntent(
        color_temperature="warm",
        emotional_tone="calm",
        energy="low",
        realism="realistic",
        lighting="bright",
        contrast="soft",
        motion="gentle",
        commercial_tone="natural",
    ),
    "clean_professional": VisualStyleIntent(
        color_temperature="neutral",
        emotional_tone="calm",
        energy="medium",
        realism="realistic",
        lighting="balanced",
        contrast="balanced",
        detail="minimal",
        commercial_tone="professional",
    ),
    "premium": VisualStyleIntent(
        color_temperature="neutral",
        emotional_tone="premium",
        energy="low",
        realism="photorealistic",
        lighting="dark",
        contrast="strong",
        detail="detailed",
        commercial_tone="premium",
    ),
    "bright_energy": VisualStyleIntent(
        color_temperature="warm",
        emotional_tone="bold",
        energy="high",
        realism="realistic",
        lighting="bright",
        contrast="strong",
        motion="dynamic",
        commercial_tone="promotional",
    ),
    "cinematic": VisualStyleIntent(
        color_temperature="cool",
        emotional_tone="dramatic",
        energy="medium",
        realism="photorealistic",
        lighting="dark",
        contrast="strong",
        detail="detailed",
        commercial_tone="natural",
    ),
    "fairytale": VisualStyleIntent(
        color_temperature="warm",
        emotional_tone="playful",
        energy="medium",
        realism="illustrative",
        lighting="bright",
        contrast="soft",
        detail="detailed",
        commercial_tone="natural",
    ),
    "natural_photo": VisualStyleIntent(
        color_temperature="neutral",
        emotional_tone="friendly",
        energy="medium",
        realism="photorealistic",
        lighting="balanced",
        contrast="balanced",
        detail="balanced",
        commercial_tone="natural",
    ),
}


def visual_style_preset(name: str) -> VisualStyleIntent:
    token = str(name or "").strip().lower()
    try:
        return _PRESETS[token].normalized()
    except KeyError as exc:
        raise ValueError("visual_style_preset_invalid") from exc


def visual_style_presets() -> tuple[str, ...]:
    return tuple(_PRESETS)


_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("color_temperature", "warm", re.compile(r"\b(т[её]пл\w*|warm|golden)\b", re.I)),
    ("color_temperature", "cool", re.compile(r"\b(холодн\w*|cool|bluish)\b", re.I)),
    ("emotional_tone", "friendly", re.compile(r"\b(добр\w*|дружелюб\w*|friendly|kind)\b", re.I)),
    ("emotional_tone", "calm", re.compile(r"\b(спокойн\w*|мягк\w*|calm|gentle|soft)\b", re.I)),
    ("emotional_tone", "dramatic", re.compile(r"\b(драматич\w*|dramatic|moody)\b", re.I)),
    ("emotional_tone", "aggressive", re.compile(r"\b(агрессив\w*|aggressive)\b", re.I)),
    ("energy", "high", re.compile(r"\b(энергич\w*|динамич\w*|energetic|dynamic)\b", re.I)),
    ("energy", "low", re.compile(r"\b(умиротвор\w*|спокойн\w*|relaxed|serene)\b", re.I)),
    ("realism", "photorealistic", re.compile(r"\b(фотореалист\w*|photoreal\w*)\b", re.I)),
    ("realism", "realistic", re.compile(r"\b(реалистич\w*|фотограф\w*|realistic|photo)\b", re.I)),
    ("realism", "illustrative", re.compile(r"\b(иллюстрац\w*|рисунк\w*|сказочн\w*|illustrat\w*|fairy.?tale)\b", re.I)),
    ("lighting", "bright", re.compile(r"\b(светл\w*|ярк\w*\s+свет|bright|airy)\b", re.I)),
    ("lighting", "dark", re.compile(r"\b(т[её]мн\w*|dark|low.?key)\b", re.I)),
    ("contrast", "soft", re.compile(r"\b(мягк\w*\s+(?:свет|контраст)|soft\s+(?:light|contrast))\b", re.I)),
    ("contrast", "strong", re.compile(r"\b(контрастн\w*|high.?contrast)\b", re.I)),
    ("detail", "minimal", re.compile(r"\b(минималист\w*|minimal\w*)\b", re.I)),
    ("detail", "detailed", re.compile(r"\b(детальн\w*|detailed|intricate)\b", re.I)),
    ("composition", "close_up", re.compile(r"\b(крупн\w*\s+план|close.?up)\b", re.I)),
    ("composition", "wide", re.compile(r"\b(общ\w*\s+план|wide\s+shot)\b", re.I)),
    ("composition", "story_scene", re.compile(r"\b(сюжетн\w*\s+сцен|story\s+scene)\b", re.I)),
    ("composition", "before_after", re.compile(r"\b(до\s+и\s+после|before\s+and\s+after)\b", re.I)),
    ("motion", "static", re.compile(r"\b(статич\w*|static)\b", re.I)),
    ("motion", "gentle", re.compile(r"\b(плавн\w*|gentle\s+motion|slow\s+motion)\b", re.I)),
    ("motion", "dynamic", re.compile(r"\b(динамич\w*|dynamic\s+motion|fast.?paced)\b", re.I)),
    ("commercial_tone", "premium", re.compile(r"\b(премиальн\w*|статусн\w*|premium|luxury)\b", re.I)),
    ("commercial_tone", "professional", re.compile(r"\b(профессиональн\w*|делов\w*|professional|businesslike)\b", re.I)),
    ("copy_space", "none", re.compile(r"\b(без\s+текста|no\s+text|without\s+text)\b", re.I)),
    ("copy_space", "medium", re.compile(r"\b(место\s+(?:для|под)\s+текст|copy\s+space)\b", re.I)),
)


def infer_visual_style(request: str) -> VisualStyleIntent:
    text = " ".join(str(request or "").replace("\x00", " ").split()).strip()
    values: dict[str, str] = {field: "auto" for field in _ALLOWED}
    for field, choice, pattern in _RULES:
        if values[field] == "auto" and pattern.search(text):
            values[field] = choice
    return VisualStyleIntent(**values).normalized()


def merge_visual_styles(
    *,
    explicit: VisualStyleIntent | None = None,
    inferred: VisualStyleIntent | None = None,
    saved: VisualStyleIntent | None = None,
) -> VisualStyleIntent:
    explicit_value = (explicit or VisualStyleIntent()).normalized()
    inferred_value = (inferred or VisualStyleIntent()).normalized()
    saved_value = (saved or VisualStyleIntent()).normalized()
    merged: dict[str, str] = {}
    for field in _ALLOWED:
        merged[field] = next(
            (
                value
                for value in (
                    getattr(explicit_value, field),
                    getattr(inferred_value, field),
                    getattr(saved_value, field),
                )
                if value != "auto"
            ),
            "auto",
        )
    return VisualStyleIntent(**merged).normalized()


def style_missing_fields(
    *,
    request: str,
    explicit: VisualStyleIntent | None = None,
    saved: VisualStyleIntent | None = None,
) -> tuple[str, ...]:
    resolved = merge_visual_styles(
        explicit=explicit,
        inferred=infer_visual_style(request),
        saved=saved,
    )
    priority = (
        "color_temperature",
        "emotional_tone",
        "energy",
        "realism",
        "lighting",
        "contrast",
        "composition",
        "copy_space",
    )
    return tuple(field for field in priority if getattr(resolved, field) == "auto")


_STYLE_LABELS: dict[str, dict[str, str]] = {
    "color_temperature": {"warm": "warm color palette", "neutral": "neutral color palette", "cool": "cool color palette"},
    "emotional_tone": {
        "friendly": "friendly and welcoming mood",
        "calm": "calm and gentle mood",
        "playful": "playful mood",
        "bold": "bold expressive mood",
        "dramatic": "dramatic mood",
        "aggressive": "aggressive intense mood",
        "premium": "premium restrained mood",
    },
    "energy": {"low": "low visual energy", "medium": "balanced visual energy", "high": "high energetic visual rhythm"},
    "realism": {
        "photorealistic": "photorealistic treatment",
        "realistic": "realistic treatment",
        "semi_stylized": "semi-stylized treatment",
        "illustrative": "illustrative artistic treatment",
    },
    "lighting": {"bright": "bright airy lighting", "balanced": "balanced natural lighting", "dark": "dark low-key lighting"},
    "contrast": {"soft": "soft contrast", "balanced": "balanced contrast", "strong": "strong contrast"},
    "detail": {"minimal": "minimal visual detail", "balanced": "balanced detail", "detailed": "rich detailed scene"},
    "composition": {
        "close_up": "close-up composition",
        "medium": "medium-shot composition",
        "wide": "wide-shot composition",
        "story_scene": "story-driven scene composition",
        "before_after": "clear before-and-after composition",
        "transformation": "continuous transformation composition",
    },
    "motion": {"static": "static composition", "gentle": "gentle motion", "dynamic": "dynamic motion"},
    "commercial_tone": {
        "natural": "natural non-salesy presentation",
        "friendly": "friendly commercial presentation",
        "professional": "clean professional presentation",
        "premium": "premium commercial presentation",
        "promotional": "high-energy promotional presentation",
    },
    "copy_space": {
        "none": "no intentional copy space",
        "small": "small intentional copy space",
        "medium": "moderate intentional copy space",
        "large": "large intentional copy space",
    },
}


def visual_style_prompt_lines(style: VisualStyleIntent) -> tuple[str, ...]:
    value = style.normalized()
    lines: list[str] = []
    for field, choice in value.to_mapping().items():
        if choice == "auto":
            continue
        label = _STYLE_LABELS.get(field, {}).get(choice)
        if label:
            lines.append(label)
    return tuple(lines)


__all__ = [
    "STYLE_SCHEMA_VERSION",
    "VisualStyleIntent",
    "infer_visual_style",
    "merge_visual_styles",
    "style_missing_fields",
    "visual_style_from_json",
    "visual_style_from_mapping",
    "visual_style_preset",
    "visual_style_presets",
    "visual_style_prompt_lines",
]
