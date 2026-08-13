from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

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
    "normalize_sales_ai_source_order",
]
