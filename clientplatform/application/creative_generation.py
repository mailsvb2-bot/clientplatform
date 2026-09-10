from __future__ import annotations

from clientplatform.domain.creative_generation import CreativeGenerationReceipt
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.creative_generation_receipt_repository import (
    CreativeGenerationReceiptRepository,
)
from services.db import get_db, get_db_ro


def get_active_creative_generation(
    *, actor: TenantContext
) -> CreativeGenerationReceipt | None:
    with get_db_ro() as conn:
        return CreativeGenerationReceiptRepository(conn).get_active(actor=actor)


def get_creative_generation(
    *, actor: TenantContext, receipt_id: str
) -> CreativeGenerationReceipt:
    with get_db_ro() as conn:
        return CreativeGenerationReceiptRepository(conn).get(
            actor=actor,
            receipt_id=receipt_id,
        )


def prepare_creative_generation(
    *,
    actor: TenantContext,
    request_text: str,
    brand_context: str,
    country_code: str,
    provider_payload_json: str,
) -> CreativeGenerationReceipt:
    with get_db() as conn:
        return CreativeGenerationReceiptRepository(conn).prepare(
            actor=actor,
            request_text=request_text,
            brand_context=brand_context,
            country_code=country_code,
            provider_payload_json=provider_payload_json,
        )


def begin_creative_generation_submission(
    *, actor: TenantContext, receipt_id: str
) -> CreativeGenerationReceipt:
    with get_db() as conn:
        return CreativeGenerationReceiptRepository(conn).begin_submission(
            actor=actor,
            receipt_id=receipt_id,
        )


def remember_creative_generation_job(
    *,
    actor: TenantContext,
    receipt_id: str,
    source_job_id: str,
    provider_status: str,
) -> CreativeGenerationReceipt:
    with get_db() as conn:
        return CreativeGenerationReceiptRepository(conn).remember_job(
            actor=actor,
            receipt_id=receipt_id,
            source_job_id=source_job_id,
            provider_status=provider_status,
        )



def claim_creative_generation_delivery(
    *, actor: TenantContext, receipt_id: str
) -> bool:
    with get_db() as conn:
        return CreativeGenerationReceiptRepository(conn).claim_delivery(
            actor=actor,
            receipt_id=receipt_id,
        )


def authorize_creative_generation_redelivery(
    *, actor: TenantContext, receipt_id: str
) -> bool:
    with get_db() as conn:
        return CreativeGenerationReceiptRepository(conn).authorize_redelivery(
            actor=actor,
            receipt_id=receipt_id,
        )


def abandon_creative_generation(
    *, actor: TenantContext, receipt_id: str
) -> bool:
    with get_db() as conn:
        return CreativeGenerationReceiptRepository(conn).abandon(
            actor=actor,
            receipt_id=receipt_id,
        )

def mark_creative_generation_delivered(
    *, actor: TenantContext, receipt_id: str
) -> CreativeGenerationReceipt:
    with get_db() as conn:
        return CreativeGenerationReceiptRepository(conn).mark_delivered(
            actor=actor,
            receipt_id=receipt_id,
        )


__all__ = [
    "abandon_creative_generation",
    "authorize_creative_generation_redelivery",
    "begin_creative_generation_submission",
    "claim_creative_generation_delivery",
    "get_active_creative_generation",
    "get_creative_generation",
    "mark_creative_generation_delivered",
    "prepare_creative_generation",
    "remember_creative_generation_job",
]
