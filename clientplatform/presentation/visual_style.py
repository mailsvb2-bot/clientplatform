from __future__ import annotations

"""Telegram-agnostic labels and compact callback choices for visual style intent."""

from dataclasses import dataclass

from clientplatform.domain.visual_style_intent import (
    VisualStyleIntent,
    style_summary_ru,
    visual_style_preset,
)


@dataclass(frozen=True, slots=True)
class StyleChoice:
    label: str
    value: str
    code: str


@dataclass(frozen=True, slots=True)
class StyleDimension:
    code: str
    field: str
    title: str
    choices: tuple[StyleChoice, ...]


_DIMENSIONS = (
    StyleDimension(
        code="t",
        field="color_temperature",
        title="Температура цвета",
        choices=(
            StyleChoice("☀️ Тёплая", "warm", "w"),
            StyleChoice("⚪ Нейтральная", "neutral", "n"),
            StyleChoice("❄️ Холодная", "cool", "c"),
            StyleChoice("🤖 Авто", "auto", "a"),
        ),
    ),
    StyleDimension(
        code="m",
        field="emotional_tone",
        title="Настроение",
        choices=(
            StyleChoice("😊 Доброжелательная", "friendly", "f"),
            StyleChoice("🧘 Спокойная", "calm", "c"),
            StyleChoice("⚡ Дерзкая", "bold", "b"),
            StyleChoice("🎭 Драматичная", "dramatic", "d"),
            StyleChoice("🟥 Агрессивная", "aggressive", "g"),
            StyleChoice("💎 Статусная", "premium", "p"),
            StyleChoice("🤖 Авто", "auto", "a"),
        ),
    ),
    StyleDimension(
        code="e",
        field="energy",
        title="Динамика",
        choices=(
            StyleChoice("🧘 Спокойная", "low", "l"),
            StyleChoice("⚖️ Средняя", "medium", "m"),
            StyleChoice("🔥 Энергичная", "high", "h"),
            StyleChoice("🤖 Авто", "auto", "a"),
        ),
    ),
    StyleDimension(
        code="r",
        field="realism",
        title="Визуальный язык",
        choices=(
            StyleChoice("📷 Фотореализм", "photorealistic", "p"),
            StyleChoice("🌿 Реалистично", "realistic", "r"),
            StyleChoice("✨ Полустилизация", "semi_stylized", "s"),
            StyleChoice("🎨 Иллюстрация", "illustrative", "i"),
            StyleChoice("🤖 Авто", "auto", "a"),
        ),
    ),
    StyleDimension(
        code="l",
        field="lighting",
        title="Свет",
        choices=(
            StyleChoice("🌤 Светло", "bright", "b"),
            StyleChoice("⚖️ Сбалансировано", "balanced", "n"),
            StyleChoice("🌑 Темно", "dark", "d"),
            StyleChoice("🤖 Авто", "auto", "a"),
        ),
    ),
    StyleDimension(
        code="c",
        field="contrast",
        title="Контраст",
        choices=(
            StyleChoice("☁️ Мягкий", "soft", "s"),
            StyleChoice("⚖️ Сбалансированный", "balanced", "b"),
            StyleChoice("◐ Сильный", "strong", "h"),
            StyleChoice("🤖 Авто", "auto", "a"),
        ),
    ),
    StyleDimension(
        code="o",
        field="composition",
        title="Композиция",
        choices=(
            StyleChoice("👤 Крупный план", "close_up", "c"),
            StyleChoice("🖼 Средний план", "medium", "m"),
            StyleChoice("🌍 Общий план", "wide", "w"),
            StyleChoice("🎬 Сюжетная сцена", "story_scene", "s"),
            StyleChoice("↔️ До / После", "before_after", "b"),
            StyleChoice("✨ Превращение", "transformation", "t"),
            StyleChoice("🤖 Авто", "auto", "a"),
        ),
    ),
    StyleDimension(
        code="x",
        field="copy_space",
        title="Место под текст",
        choices=(
            StyleChoice("🚫 Не оставлять", "none", "n"),
            StyleChoice("▫️ Немного", "small", "s"),
            StyleChoice("📝 Средне", "medium", "m"),
            StyleChoice("⬜ Много", "large", "l"),
            StyleChoice("🤖 Авто", "auto", "a"),
        ),
    ),
)

_DIMENSION_BY_CODE = {dimension.code: dimension for dimension in _DIMENSIONS}
_DIMENSION_BY_FIELD = {dimension.field: dimension for dimension in _DIMENSIONS}

_PRESETS = (
    ("🤗 Тёпло и дружелюбно", "warm_friendly", "wf"),
    ("🧘 Мягко и спокойно", "soft_calm", "sc"),
    ("🔥 Ярко и энергично", "bright_energy", "be"),
    ("💎 Премиально", "premium", "pr"),
    ("🎬 Кинематографично", "cinematic", "ci"),
    ("🎨 Художественно", "illustrative", "il"),
    ("📷 Натуральное фото", "natural_photo", "np"),
)
_PRESET_BY_CODE = {code: name for _label, name, code in _PRESETS}


def style_dimension(code: str) -> StyleDimension:
    try:
        return _DIMENSION_BY_CODE[str(code or "").strip()]
    except KeyError as exc:
        raise ValueError("visual style dimension is invalid") from exc


def style_choice(dimension_code: str, value_code: str) -> tuple[str, str]:
    dimension = style_dimension(dimension_code)
    token = str(value_code or "").strip()
    for choice in dimension.choices:
        if choice.code == token:
            return dimension.field, choice.value
    raise ValueError("visual style choice is invalid")


def style_preset_name(code: str) -> str:
    try:
        return _PRESET_BY_CODE[str(code or "").strip()]
    except KeyError as exc:
        raise ValueError("visual style preset is invalid") from exc


def _checked_label(label: str, *, selected: bool) -> str:
    return f"✅ {label}" if selected else label


def style_dashboard_rows(
    token: str,
    intent: VisualStyleIntent | None = None,
) -> list[list[tuple[str, str]]]:
    current = (intent or VisualStyleIntent()).normalized()
    current_mapping = current.to_mapping()

    def preset(index: int) -> tuple[str, str]:
        label, name, code = _PRESETS[index]
        selected = current_mapping == visual_style_preset(name).to_mapping()
        return (
            _checked_label(label, selected=selected),
            f"cpc:st:p:{code}:{token}",
        )

    rows = [
        [preset(0), preset(1)],
        [preset(2), preset(3)],
        [preset(4), preset(5)],
        [preset(6)],
    ]
    for dimension in _DIMENSIONS:
        selected_value = getattr(current, dimension.field)
        configured = selected_value != "auto"
        rows.append(
            [(
                _checked_label(f"⚙️ {dimension.title}", selected=configured),
                f"cpc:st:d:{dimension.code}:{token}",
            )]
        )
    rows.extend(
        [
            [("🤖 Авто по запросу", f"cpc:st:reset:{token}")],
            [("💾 Запомнить этот стиль", f"cpc:st:save:{token}")],
            [("🗑 Не использовать сохранённый стиль", f"cpc:st:clear:{token}")],
            [("✅ Готово — к созданию", f"cpc:st:go:{token}")],
        ]
    )
    return rows


def style_dimension_rows(
    token: str,
    code: str,
    intent: VisualStyleIntent | None = None,
) -> list[list[tuple[str, str]]]:
    dimension = style_dimension(code)
    current = (intent or VisualStyleIntent()).normalized()
    selected_value = getattr(current, dimension.field)
    rows: list[list[tuple[str, str]]] = []
    choices = list(dimension.choices)
    for index in range(0, len(choices), 2):
        row: list[tuple[str, str]] = []
        for choice in choices[index:index + 2]:
            row.append(
                (
                    _checked_label(
                        choice.label,
                        selected=choice.value == selected_value,
                    ),
                    f"cpc:st:s:{dimension.code}:{choice.code}:{token}",
                )
            )
        rows.append(row)
    rows.append([("✅ Готово — к созданию", f"cpc:st:go:{token}")])
    rows.append([("⬅️ Все настройки", f"cpc:st:open:{token}")])
    return rows

def style_dashboard_text(
    intent: VisualStyleIntent,
    *,
    request_inferred_fields: tuple[str, ...] = (),
    saved_applied: bool = False,
) -> str:
    inferred_names = [
        _DIMENSION_BY_FIELD[field].title
        for field in request_inferred_fields
        if field in _DIMENSION_BY_FIELD
    ]
    notes: list[str] = []
    if inferred_names:
        notes.append(
            "Из Вашего текста уже понял: " + ", ".join(inferred_names) + "."
        )
    if saved_applied:
        notes.append("Ваш обычный стиль подставлен как стартовый и его можно изменить.")
    suffix = "\n\n" + "\n".join(notes) if notes else ""
    return (
        "🎨 Как Вы представляете будущий визуал?\n\n"
        "Можно выбрать готовый характер или уточнить отдельные параметры. "
        "Это не меняет смысл Вашей идеи — только то, как она будет выглядеть.\n\n"
        + style_summary_ru(intent)
        + suffix
    )


__all__ = [
    "StyleChoice",
    "StyleDimension",
    "style_choice",
    "style_dashboard_rows",
    "style_dashboard_text",
    "style_dimension",
    "style_dimension_rows",
    "style_preset_name",
]
