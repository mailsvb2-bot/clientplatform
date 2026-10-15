"""Bounded positive-only Russian scene prompts for visual providers."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

MAX_SCENE_PROMPT = 350
_OWNER = re.compile(r'Owner request, preserve its meaning exactly: "([^"]+)"')


@dataclass(frozen=True)
class VisualDirection:
    subject: str
    action: str = ""
    setting: str = ""
    light: str = ""
    angle: str = ""
    style: str = ""
    palette: str = ""
    source: str = "fallback"


def owner_request_from_prompt(prompt: str) -> str:
    match = _OWNER.search(str(prompt or ""))
    return (match.group(1) if match else "").strip()


def direction_from_brief_metadata(metadata: dict[str, Any] | None) -> VisualDirection | None:
    value = (metadata or {}).get("visual_direction")
    if not isinstance(value, dict):
        return None
    fields = ("subject", "action", "setting", "light", "angle", "style", "palette")
    if not set(value).issubset(fields):
        return None
    result = {key: " ".join(str(value.get(key) or "").split())[:120] for key in fields}
    if not result["subject"]:
        return None
    return VisualDirection(**result, source="director")


def fallback_visual_direction(prompt: str, *, style_hint: str = "") -> VisualDirection | None:
    subject = owner_request_from_prompt(prompt) or str(prompt or "").splitlines()[0].strip()
    if not subject:
        return None
    return VisualDirection(subject=subject[:220], style=str(style_hint or "")[:100])


def build_direction_prompt(direction: VisualDirection) -> str:
    parts = [direction.subject.strip()]
    for label, value in (
        ("Действие", direction.action), ("Окружение", direction.setting),
        ("Свет", direction.light), ("Ракурс", direction.angle),
        ("Стиль", direction.style), ("Палитра", direction.palette),
    ):
        if value:
            parts.append(f"{label}: {value.strip()}")
    while len(". ".join(parts)) > MAX_SCENE_PROMPT and len(parts) > 1:
        parts.pop()
    return ". ".join(parts)[:MAX_SCENE_PROMPT]


def explicit_text_allowed(prompt: str) -> bool:
    return bool(re.search(r"(?i)текст на изображени|надпись на картин|render text|typography text", prompt or ""))


def negative_prompt_for(*, explicit_text_allowed: bool = False) -> str:
    parts = ["watermark", "logo", "blur", "distorted anatomy", "low quality"]
    if not explicit_text_allowed:
        parts.extend(("text", "letters"))
    return ", ".join(parts)
