from __future__ import annotations

"""Canonical deterministic typography presets for ClientPlatform visual layouts."""

VISUAL_TYPOGRAPHY_PRESETS = (
    "auto",
    "modern",
    "strict",
    "friendly",
    "premium",
    "editorial",
    "elegant",
    "bold_ad",
)

VISUAL_TYPOGRAPHY_LABELS_RU = {
    "auto": "Автоматически",
    "modern": "Современный · Lato",
    "strict": "Стогий · Liberation Sans",
    "friendly": "Дружелюбный · DejaVu Sans",
    "premium": "Премиальный · Noto Serif + Sans",
    "editorial": "Редакционный · Noto Serif",
    "elegant": "Элегантный · Liberation Serif",
    "bold_ad": "Жирный рекламный · Lato Heavy",
}


def normalize_visual_typography_preset(value: object) -> str:
    token = str(value or "auto").strip().lower()
    if token not in VISUAL_TYPOGRAPHY_PRESETS:
        raise ValueError("visual_typography_preset_invalid")
    return token


__all__ = [
    "VISUAL_TYPOGRAPHY_LABELS_RU",
    "VISUAL_TYPOGRAPHY_PRESETS",
    "normalize_visual_typography_preset",
]
