from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from clientplatform.domain.connections import ConnectionPlatform, normalize_connection_platform
from clientplatform.domain.tenancy import normalize_uuid


class SalesAIJobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY = "retry"
    DONE = "done"
    DEAD = "dead"


class SalesAIJobLeaseLost(RuntimeError):
    """A worker attempted to finish or retry work after losing its conditional lease."""


_DEDUPE_RE = re.compile(r"[^\x00-\x1f\x7f]{1,240}")
_ORDER_RE = re.compile(r"[0-9]{32}")


def normalize_sales_ai_source_order(value: int | str) -> str:
    raw = str(value).strip()
    if not raw.isdigit() or len(raw) > 32:
        raise ValueError("source order must be a decimal identifier of at most 32 digits")
    return raw.lstrip("0").zfill(32)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _timestamp_candidate(
    payload: Mapping[str, Any],
    platform: ConnectionPlatform,
) -> int | None:
    candidates: list[Any] = []
    if platform == ConnectionPlatform.VK:
        obj = _mapping(payload.get("object"))
        message = _mapping(obj.get("message") or obj)
        candidates.extend((message.get("date"), obj.get("date"), payload.get("timestamp")))
    else:
        message = _mapping(payload.get("message"))
        candidates.extend(
            (
                message.get("timestamp"),
                message.get("created_at"),
                payload.get("timestamp"),
                payload.get("created_at"),
            )
        )
    for candidate in candidates:
        if candidate is None or isinstance(candidate, bool):
            continue
        try:
            if isinstance(candidate, str) and not candidate.strip().isdigit():
                parsed = datetime.fromisoformat(candidate.strip().replace("Z", "+00:00"))
                value = int(parsed.timestamp() * 1000)
            else:
                numeric = int(str(candidate).strip())
                if numeric <= 0:
                    continue
                if numeric < 10_000_000_000:
                    value = numeric * 1000
                elif numeric > 9_999_999_999_999:
                    value = numeric // 1000
                else:
                    value = numeric
            if 1 <= value <= 9_999_999_999_999:
                return value
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _positive_sequence(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        candidate = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if 0 < candidate < 10**19:
        return candidate
    return None


def _provider_sequence_candidate(
    payload: Mapping[str, Any],
    platform: ConnectionPlatform,
) -> int | None:
    if platform == ConnectionPlatform.VK:
        obj = _mapping(payload.get("object"))
        message = _mapping(obj.get("message") or obj)
        candidates = (
            message.get("conversation_message_id"),
            message.get("id"),
            obj.get("conversation_message_id"),
            obj.get("id"),
            payload.get("ts"),
        )
    elif platform == ConnectionPlatform.MAX:
        message = _mapping(payload.get("message"))
        body = _mapping(message.get("body"))
        candidates = (
            payload.get("update_id"),
            body.get("mid"),
            message.get("message_id"),
            message.get("id"),
        )
    else:
        raise ValueError("messenger source ordering supports only VK or MAX")
    for candidate in candidates:
        sequence = _positive_sequence(candidate)
        if sequence is not None:
            return sequence
    return None


def messenger_source_order(
    payload: Mapping[str, Any],
    platform: ConnectionPlatform | str,
) -> str:
    """Build a 32-digit provider-monotonic Sales AI order key for VK/MAX.

    Normal provider events must expose a numeric provider-side message/update
    sequence. A hash fallback is deliberately forbidden: hashes are stable but
    not monotonic and can invert two messages sharing a coarse timestamp.
    """

    normalized_platform = normalize_connection_platform(platform)
    if normalized_platform not in {ConnectionPlatform.VK, ConnectionPlatform.MAX}:
        raise ValueError("messenger source ordering supports only VK or MAX")
    sequence = _provider_sequence_candidate(payload, normalized_platform)
    if sequence is None:
        raise ValueError("provider event has no monotonic message/update sequence")
    millis = _timestamp_candidate(payload, normalized_platform)
    if millis is None:
        millis = min(int(time.time() * 1000), 9_999_999_999_999)
    return f"{millis:013d}{sequence:019d}"


@dataclass(frozen=True, slots=True)
class SalesAIJob:
    id: str
    business_id: str
    lead_id: str
    source_event_dedupe_key: str
    source_order_key: str
    status: SalesAIJobStatus
    attempts: int
    available_at: str
    created_at: str
    updated_at: str
    locked_at: str | None = None
    lock_token: str | None = None
    last_error_code: str | None = None
    completed_at: str | None = None
    dead_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="sales_ai_job_id"))
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        object.__setattr__(self, "lead_id", normalize_uuid(self.lead_id, field_name="sales_lead_id"))
        key = str(self.source_event_dedupe_key or "").strip()
        if not _DEDUPE_RE.fullmatch(key):
            raise ValueError("source_event_dedupe_key must be 1..240 printable characters")
        object.__setattr__(self, "source_event_dedupe_key", key)
        order = str(self.source_order_key or "").strip()
        if not _ORDER_RE.fullmatch(order):
            raise ValueError("source_order_key must be a 32-digit decimal sort key")
        object.__setattr__(self, "source_order_key", order)
        status = self.status if isinstance(self.status, SalesAIJobStatus) else SalesAIJobStatus(str(self.status))
        object.__setattr__(self, "status", status)
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int) or self.attempts < 0:
            raise ValueError("attempts must be a non-negative integer")
        if status == SalesAIJobStatus.PROCESSING and not self.lock_token:
            raise ValueError("processing sales AI job requires lock_token")


__all__ = [
    "SalesAIJob",
    "SalesAIJobLeaseLost",
    "SalesAIJobStatus",
    "messenger_source_order",
    "normalize_sales_ai_source_order",
]
