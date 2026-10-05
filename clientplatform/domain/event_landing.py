from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


_MAX_POINTS = 6
_MAX_FAQ = 6


class EventLandingTheme(StrEnum):
    CALM = "calm"
    BOLD = "bold"
    MINIMAL = "minimal"


def _text(
    value: object,
    *,
    field_name: str,
    maximum: int,
    required: bool = False,
) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
    if required and not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return normalized


def _items(
    values: object,
    *,
    field_name: str,
    maximum: int = 280,
) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        source = [values]
    elif isinstance(values, (list, tuple)):
        source = list(values)
    else:
        raise ValueError(f"{field_name} must be a list of strings")
    result: list[str] = []
    seen: set[str] = set()
    for raw in source:
        item = _text(raw, field_name=field_name, maximum=maximum)
        if not item:
            continue
        marker = item.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
        if len(result) > _MAX_POINTS:
            raise ValueError(f"{field_name} must contain at most {_MAX_POINTS} items")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class EventLandingFaq:
    question: str
    answer: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "question",
            _text(self.question, field_name="faq question", maximum=220, required=True),
        )
        object.__setattr__(
            self,
            "answer",
            _text(self.answer, field_name="faq answer", maximum=700, required=True),
        )

    def to_payload(self) -> dict[str, str]:
        return {"question": self.question, "answer": self.answer}

    @classmethod
    def from_payload(cls, payload: object) -> "EventLandingFaq":
        if not isinstance(payload, dict):
            raise ValueError("event landing faq item must be an object")
        if set(payload) != {"question", "answer"}:
            raise ValueError("event landing faq item has unsupported fields")
        return cls(
            question=str(payload.get("question") or ""),
            answer=str(payload.get("answer") or ""),
        )


@dataclass(frozen=True, slots=True)
class EventLandingContent:
    eyebrow: str
    hero_title: str
    hero_subtitle: str
    audience_title: str
    audience_points: tuple[str, ...]
    outcomes_title: str
    outcome_points: tuple[str, ...]
    agenda_title: str
    agenda_points: tuple[str, ...]
    speaker_title: str
    speaker_text: str
    faq_title: str
    faq: tuple[EventLandingFaq, ...]
    cta_title: str
    cta_text: str
    theme: EventLandingTheme = EventLandingTheme.CALM

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "eyebrow",
            _text(self.eyebrow, field_name="eyebrow", maximum=120),
        )
        object.__setattr__(
            self,
            "hero_title",
            _text(self.hero_title, field_name="hero title", maximum=180, required=True),
        )
        object.__setattr__(
            self,
            "hero_subtitle",
            _text(self.hero_subtitle, field_name="hero subtitle", maximum=800),
        )
        object.__setattr__(
            self,
            "audience_title",
            _text(
                self.audience_title,
                field_name="audience title",
                maximum=120,
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "audience_points",
            _items(self.audience_points, field_name="audience points"),
        )
        object.__setattr__(
            self,
            "outcomes_title",
            _text(
                self.outcomes_title,
                field_name="outcomes title",
                maximum=120,
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "outcome_points",
            _items(self.outcome_points, field_name="outcome points"),
        )
        object.__setattr__(
            self,
            "agenda_title",
            _text(
                self.agenda_title,
                field_name="agenda title",
                maximum=120,
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "agenda_points",
            _items(self.agenda_points, field_name="agenda points"),
        )
        object.__setattr__(
            self,
            "speaker_title",
            _text(
                self.speaker_title,
                field_name="speaker title",
                maximum=120,
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "speaker_text",
            _text(self.speaker_text, field_name="speaker text", maximum=1200),
        )
        object.__setattr__(
            self,
            "faq_title",
            _text(self.faq_title, field_name="faq title", maximum=120, required=True),
        )
        faq_items = tuple(
            item if isinstance(item, EventLandingFaq) else EventLandingFaq.from_payload(item)
            for item in self.faq
        )
        if len(faq_items) > _MAX_FAQ:
            raise ValueError(f"event landing faq must contain at most {_MAX_FAQ} items")
        object.__setattr__(self, "faq", faq_items)
        object.__setattr__(
            self,
            "cta_title",
            _text(self.cta_title, field_name="cta title", maximum=180, required=True),
        )
        object.__setattr__(
            self,
            "cta_text",
            _text(self.cta_text, field_name="cta text", maximum=600),
        )
        object.__setattr__(
            self,
            "theme",
            self.theme
            if isinstance(self.theme, EventLandingTheme)
            else EventLandingTheme(str(self.theme)),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "eyebrow": self.eyebrow,
            "hero_title": self.hero_title,
            "hero_subtitle": self.hero_subtitle,
            "audience_title": self.audience_title,
            "audience_points": list(self.audience_points),
            "outcomes_title": self.outcomes_title,
            "outcome_points": list(self.outcome_points),
            "agenda_title": self.agenda_title,
            "agenda_points": list(self.agenda_points),
            "speaker_title": self.speaker_title,
            "speaker_text": self.speaker_text,
            "faq_title": self.faq_title,
            "faq": [item.to_payload() for item in self.faq],
            "cta_title": self.cta_title,
            "cta_text": self.cta_text,
            "theme": self.theme.value,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "EventLandingContent":
        if not isinstance(payload, dict):
            raise ValueError("event landing content must be an object")
        expected = set(cls.__dataclass_fields__)
        unknown = set(payload) - expected
        if unknown:
            raise ValueError("event landing content has unsupported fields")
        required = expected - {"theme"}
        missing = required - set(payload)
        if missing:
            raise ValueError("event landing content is missing required fields")
        return cls(
            eyebrow=payload.get("eyebrow"),
            hero_title=payload.get("hero_title"),
            hero_subtitle=payload.get("hero_subtitle"),
            audience_title=payload.get("audience_title"),
            audience_points=payload.get("audience_points") or (),
            outcomes_title=payload.get("outcomes_title"),
            outcome_points=payload.get("outcome_points") or (),
            agenda_title=payload.get("agenda_title"),
            agenda_points=payload.get("agenda_points") or (),
            speaker_title=payload.get("speaker_title"),
            speaker_text=payload.get("speaker_text"),
            faq_title=payload.get("faq_title"),
            faq=tuple(
                EventLandingFaq.from_payload(item)
                for item in (payload.get("faq") or ())
            ),
            cta_title=payload.get("cta_title"),
            cta_text=payload.get("cta_text"),
            theme=payload.get("theme") or EventLandingTheme.CALM.value,
        )


def event_landing_content_to_json(content: EventLandingContent) -> str:
    if not isinstance(content, EventLandingContent):
        raise ValueError("content must be EventLandingContent")
    return json.dumps(
        content.to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def event_landing_content_from_json(raw: object) -> EventLandingContent:
    try:
        payload: Any = json.loads(str(raw or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("stored event landing content is invalid") from exc
    return EventLandingContent.from_payload(payload)


__all__ = [
    "EventLandingContent",
    "EventLandingFaq",
    "EventLandingTheme",
    "event_landing_content_from_json",
    "event_landing_content_to_json",
]
