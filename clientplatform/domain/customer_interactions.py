from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


class CustomerInteractionError(ValueError):
    """A customer interaction payload violates the channel-neutral contract."""


_MAX_TEXT_CHARS = 3500
_MAX_LABEL_CHARS = 40
_MAX_COMMAND_CHARS = 180
_MAX_BUTTONS = 10
_MAX_BUTTONS_PER_ROW = 5
_COMMAND_RE = re.compile(r"[A-Za-z0-9:_-]{1,180}")


def _clean_text(value: object) -> str:
    text = str(value or "").replace("\x00", " " ).strip()
    if not text:
        raise CustomerInteractionError("customer interaction text must not be empty")
    if len(text) > _MAX_TEXT_CHARS:
        raise CustomerInteractionError("customer interaction text is too long")
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise CustomerInteractionError("customer interaction text contains control characters")
    return text


def _clean_label(value: object) -> str:
    label = " ".join(str(value or "").replace("\x00", " " ).split()).strip()
    if not label or len(label) > _MAX_LABEL_CHARS:
        raise CustomerInteractionError("customer interaction button label is invalid")
    return label


def _clean_command(value: object) -> str:
    command = str(value or "").strip()
    if not _COMMAND_RE.fullmatch(command) or len(command) > _MAX_COMMAND_CHARS:
        raise CustomerInteractionError("customer interaction command is invalid")
    return command


@dataclass(frozen=True, slots=True)
class CustomerInteractionButton:
    label: str
    command: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", _clean_label(self.label))
        object.__setattr__(self, "command", _clean_command(self.command))


@dataclass(frozen=True, slots=True)
class CustomerInteractionMessage:
    text: str
    rows: tuple[tuple[CustomerInteractionButton, ...], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _clean_text(self.text))
        normalized_rows: list[tuple[CustomerInteractionButton, ...]] = []
        total = 0
        for row in self.rows:
            normalized = tuple(
                button if isinstance(button, CustomerInteractionButton)
                else CustomerInteractionButton(**dict(button))
                for button in row
            )
            if not normalized or len(normalized) > _MAX_BUTTONS_PER_ROW:
                raise CustomerInteractionError("customer interaction button row is invalid")
            total += len(normalized)
            normalized_rows.append(normalized)
        if total > _MAX_BUTTONS:
            raise CustomerInteractionError("customer interaction has too many buttons")
        object.__setattr__(self, "rows", tuple(normalized_rows))

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "text": self.text,
                "rows": [
                    [{"label": button.label, "command": button.command} for button in row]
                    for row in self.rows
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "CustomerInteractionMessage":
        try:
            payload: Any = json.loads(str(value or ""))
        except json.JSONDecodeError as exc:
            raise CustomerInteractionError("customer interaction payload is not JSON") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise CustomerInteractionError("customer interaction payload version is unsupported")
        raw_rows = payload.get("rows") or []
        if not isinstance(raw_rows, list):
            raise CustomerInteractionError("customer interaction rows are invalid")
        rows: list[tuple[CustomerInteractionButton, ...]] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, list):
                raise CustomerInteractionError("customer interaction row is invalid")
            buttons: list[CustomerInteractionButton] = []
            for raw_button in raw_row:
                if not isinstance(raw_button, dict):
                    raise CustomerInteractionError("customer interaction button is invalid")
                buttons.append(
                    CustomerInteractionButton(
                        label=raw_button.get("label"),
                        command=raw_button.get("command"),
                    )
                )
            rows.append(tuple(buttons))
        return cls(text=payload.get("text"), rows=tuple(rows))


__all__ = [
    "CustomerInteractionButton",
    "CustomerInteractionError",
    "CustomerInteractionMessage",
]
