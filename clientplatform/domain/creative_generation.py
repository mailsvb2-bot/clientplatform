from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CreativeGenerationReceiptStatus(StrEnum):
    PREPARED = "prepared"
    SUBMITTING = "submitting"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DELIVERED = "delivered"


_ACTIVE_STATUSES = frozenset(
    {
        CreativeGenerationReceiptStatus.PREPARED,
        CreativeGenerationReceiptStatus.SUBMITTING,
        CreativeGenerationReceiptStatus.QUEUED,
        CreativeGenerationReceiptStatus.RUNNING,
        CreativeGenerationReceiptStatus.SUCCEEDED,
    }
)


@dataclass(frozen=True, slots=True)
class CreativeGenerationReceipt:
    id: str
    business_id: str
    created_by_member_id: str
    request_text: str
    brand_context: str
    country_code: str
    provider_payload_json: str
    idempotency_key: str
    source_job_id: str
    status: CreativeGenerationReceiptStatus
    created_at: str
    updated_at: str
    delivery_claimed_at: str = ""

    @property
    def active(self) -> bool:
        return self.status in _ACTIVE_STATUSES


__all__ = ["CreativeGenerationReceipt", "CreativeGenerationReceiptStatus"]
