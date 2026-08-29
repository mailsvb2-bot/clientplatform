from __future__ import annotations

import json
import re
from dataclasses import dataclass
from email.utils import parseaddr


_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def normalize_email_address(value: str) -> str:
    """Normalize an address without accepting display-name/header injection."""

    raw = str(value or "").strip()
    _display, address = parseaddr(raw)
    normalized = address.casefold().strip()
    if not normalized or len(normalized) > 320 or not _EMAIL_RE.fullmatch(normalized):
        raise ValueError("email address is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError("email address contains control characters")
    return normalized


@dataclass(frozen=True, slots=True)
class EmailPayload:
    subject: str
    body: str

    def __post_init__(self) -> None:
        subject = " ".join(str(self.subject or "").replace("\x00", " ").split())
        body = str(self.body or "").replace("\x00", "").strip()
        if not subject or len(subject) > 240:
            raise ValueError("email subject must be 1..240 characters")
        if "\r" in subject or "\n" in subject:
            raise ValueError("email subject contains a header break")
        if not body or len(body) > 100_000:
            raise ValueError("email body must be 1..100000 characters")
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "body", body)

    def to_json(self) -> str:
        return json.dumps(
            {"subject": self.subject, "body": self.body},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "EmailPayload":
        try:
            payload = json.loads(str(raw or ""))
        except json.JSONDecodeError as exc:
            raise ValueError("email dispatch payload is invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("email dispatch payload is invalid")
        return cls(
            subject=str(payload.get("subject") or ""),
            body=str(payload.get("body") or ""),
        )


__all__ = ["EmailPayload", "normalize_email_address"]
