from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EventContentStage(StrEnum):
    WARMUP = "warmup"
    EVENT_DAY = "event_day_announcement"
    POST_EVENT = "post_event_followup"


class EventContentMode(StrEnum):
    TEXT = "text"
    TEXT_WITH_IMAGE = "text_with_image"
    TEXT_IN_IMAGE = "text_in_image"


_MODE_LABELS = {
    EventContentMode.TEXT: "Только текст",
    EventContentMode.TEXT_WITH_IMAGE: "Текст + картинка",
    EventContentMode.TEXT_IN_IMAGE: "Текст в тематической картинке",
}
_STAGE_LABELS = {
    EventContentStage.WARMUP: "прогрев",
    EventContentStage.EVENT_DAY: "анонс в день мероприятия",
    EventContentStage.POST_EVENT: "дожим после мероприятия",
}


@dataclass(frozen=True, slots=True)
class EventContentPreference:
    business_id: str
    event_id: str
    stage: EventContentStage
    mode: EventContentMode
    updated_by_member_id: str
    created_at: str
    updated_at: str

    @property
    def mode_label(self) -> str:
        return _MODE_LABELS[self.mode]

    @property
    def stage_label(self) -> str:
        return _STAGE_LABELS[self.stage]


@dataclass(frozen=True, slots=True)
class EventContentMessage:
    business_id: str
    event_id: str
    stage: EventContentStage
    slot_key: str
    position: int
    revision: int
    scheduled_at: str | None
    text: str
    source: str
    updated_by_member_id: str
    created_at: str
    updated_at: str


def event_content_mode_label(mode: EventContentMode | str) -> str:
    normalized = mode if isinstance(mode, EventContentMode) else EventContentMode(str(mode))
    return _MODE_LABELS[normalized]


def event_content_stage_label(stage: EventContentStage | str) -> str:
    normalized = stage if isinstance(stage, EventContentStage) else EventContentStage(str(stage))
    return _STAGE_LABELS[normalized]


def parse_event_content_mode(value: object) -> EventContentMode:
    raw = " ".join(str(value or "").strip().casefold().split())
    aliases = {
        "1": EventContentMode.TEXT,
        "только текст": EventContentMode.TEXT,
        "текст": EventContentMode.TEXT,
        "2": EventContentMode.TEXT_WITH_IMAGE,
        "текст + картинка": EventContentMode.TEXT_WITH_IMAGE,
        "текст+картинка": EventContentMode.TEXT_WITH_IMAGE,
        "текст и картинка": EventContentMode.TEXT_WITH_IMAGE,
        "3": EventContentMode.TEXT_IN_IMAGE,
        "текст в тематической картинке": EventContentMode.TEXT_IN_IMAGE,
        "текст в картинке": EventContentMode.TEXT_IN_IMAGE,
    }
    try:
        return aliases[raw]
    except KeyError as exc:
        raise ValueError("unsupported event content mode") from exc


def event_visual_request(
    *,
    stage: EventContentStage,
    mode: EventContentMode,
    event_title: str,
    message_text: str,
    session_label: str = "",
) -> str:
    if mode is EventContentMode.TEXT:
        raise ValueError("text-only mode does not require a visual")
    title = " ".join(str(event_title or "").split()).strip()[:180]
    body = " ".join(str(message_text or "").split()).strip()[:900]
    session = " ".join(str(session_label or "").split()).strip()[:180]
    if not title or not body:
        raise ValueError("event title and message text are required")
    stage_label = event_content_stage_label(stage)
    context = f" Эфир: {session}." if session else ""
    if mode is EventContentMode.TEXT_WITH_IMAGE:
        return (
            f"Тематическая иллюстрация для этапа «{stage_label}» вебинара «{title}». "
            f"Смысл сообщения: {body}.{context} "
            "Сделай выразительный тематический визуал без читаемого рекламного текста, "
            "букв, дат, URL и интерфейсных элементов; основной текст будет отправлен отдельно."
        )
    return (
        f"Тематическая карточка для этапа «{stage_label}» вебинара «{title}».{context} "
        "Размести внутри изображения короткий, хорошо читаемый основной текст на русском: "
        f"«{body}». Сохрани смысл и факты, не добавляй обещаний, цен, срочности, дат или URL, "
        "которых нет в исходном тексте. Текст должен оставаться читаемым на экране телефона."
    )


__all__ = [
    "EventContentMessage",
    "EventContentMode",
    "EventContentPreference",
    "EventContentStage",
    "event_content_mode_label",
    "event_content_stage_label",
    "event_visual_request",
    "parse_event_content_mode",
]
