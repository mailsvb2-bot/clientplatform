from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.creative_generation import (
    CreativeGenerationReceipt,
    CreativeGenerationReceiptStatus,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _receipt(row: Any) -> CreativeGenerationReceipt:
    return CreativeGenerationReceipt(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        created_by_member_id=str(_value(row, "created_by_member_id", 2)),
        request_text=str(_value(row, "request_text", 3)),
        brand_context=str(_value(row, "brand_context", 4)),
        country_code=str(_value(row, "country_code", 5)),
        provider_payload_json=str(_value(row, "provider_payload_json", 6)),
        idempotency_key=str(_value(row, "idempotency_key", 7)),
        source_job_id=str(_value(row, "source_job_id", 8)),
        delivery_claimed_at=str(_value(row, "delivery_claimed_at", 9)),
        status=CreativeGenerationReceiptStatus(str(_value(row, "status", 10))),
        created_at=str(_value(row, "created_at", 11)),
        updated_at=str(_value(row, "updated_at", 12)),
    )


_SELECT = """
    SELECT id, business_id, created_by_member_id, request_text, brand_context,
           country_code, provider_payload_json, idempotency_key, source_job_id,
           delivery_claimed_at, status, created_at, updated_at
    FROM creative_generation_receipts
"""


class CreativeGenerationReceiptRepository:
    """Durable receipt around one exact provider-owned visual generation request."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _actor(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        return current

    def get_active(self, *, actor: TenantContext) -> CreativeGenerationReceipt | None:
        current = self._actor(actor)
        row = self._conn.execute(
            _SELECT
            + " WHERE business_id=? AND created_by_member_id=? "
            + "AND status IN ('prepared','submitting','queued','running','succeeded') "
            + "ORDER BY updated_at DESC LIMIT 1",
            (current.business_id, current.membership_id),
        ).fetchone()
        return None if row is None else _receipt(row)

    def get(self, *, actor: TenantContext, receipt_id: str) -> CreativeGenerationReceipt:
        current = self._actor(actor)
        normalized = normalize_uuid(receipt_id, field_name="receipt_id")
        row = self._conn.execute(
            _SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise LookupError("creative generation receipt was not found")
        receipt = _receipt(row)
        if receipt.created_by_member_id != current.membership_id:
            raise LookupError("creative generation receipt was not found")
        return receipt

    def prepare(
        self,
        *,
        actor: TenantContext,
        request_text: str,
        brand_context: str,
        country_code: str,
        provider_payload_json: str,
        now: str | None = None,
    ) -> CreativeGenerationReceipt:
        current = self._actor(actor)
        request = " ".join(str(request_text or "").replace("\x00", " ").split()).strip()
        brand = " ".join(str(brand_context or "").replace("\x00", " ").split()).strip()
        country = str(country_code or "").strip().upper()
        provider_payload = str(provider_payload_json or "")
        if not request or len(request) > 1500:
            raise ValueError("creative generation request is invalid")
        if len(brand) > 2500:
            raise ValueError("creative generation brand context is invalid")
        if country and (len(country) != 2 or not country.isalpha()):
            raise ValueError("creative generation country code is invalid")
        if not provider_payload or len(provider_payload) > 10000 or "\x00" in provider_payload:
            raise ValueError("creative generation provider payload is invalid")
        existing = self.get_active(actor=current)
        timestamp = str(now or _iso_now())
        if existing is not None:
            if existing.status != CreativeGenerationReceiptStatus.PREPARED:
                return existing
            if (
                existing.request_text == request
                and existing.brand_context == brand
                and existing.country_code == country
                and existing.provider_payload_json == provider_payload
            ):
                return existing
            # A confirmation callback must authorize one immutable receipt only.
            # Replacing a not-yet-submitted request therefore invalidates its old UUID.
            self._conn.execute(
                "DELETE FROM creative_generation_receipts "
                "WHERE id=? AND business_id=? AND status='prepared'",
                (existing.id, current.business_id),
            )
        receipt_id = str(uuid.uuid4())
        digest = hashlib.sha256(
            f"{current.business_id}|{receipt_id}".encode("utf-8")
        ).hexdigest()
        idempotency_key = f"clientplatform:owner-image:{digest}"
        self._conn.execute(
            """
            INSERT INTO creative_generation_receipts(
                id,business_id,created_by_member_id,request_text,brand_context,
                country_code,provider_payload_json,idempotency_key,source_job_id,
                status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'prepared',?,?)
            """,
            (
                receipt_id,
                current.business_id,
                current.membership_id,
                request,
                brand,
                country,
                provider_payload,
                idempotency_key,
                "",
                timestamp,
                timestamp,
            ),
        )
        return self.get(actor=current, receipt_id=receipt_id)

    def begin_submission(
        self, *, actor: TenantContext, receipt_id: str
    ) -> CreativeGenerationReceipt:
        current = self._actor(actor)
        receipt = self.get(actor=current, receipt_id=receipt_id)
        if receipt.status == CreativeGenerationReceiptStatus.PREPARED:
            self._conn.execute(
                """
                UPDATE creative_generation_receipts
                SET status='submitting', updated_at=?
                WHERE id=? AND business_id=? AND status='prepared'
                """,
                (_iso_now(), receipt.id, current.business_id),
            )
            receipt = self.get(actor=current, receipt_id=receipt.id)
        return receipt

    def remember_job(
        self,
        *,
        actor: TenantContext,
        receipt_id: str,
        source_job_id: str,
        provider_status: str,
        now: str | None = None,
    ) -> CreativeGenerationReceipt:
        current = self._actor(actor)
        receipt = self.get(actor=current, receipt_id=receipt_id)
        job_id = str(source_job_id or "").strip()
        if not job_id or len(job_id) > 128:
            raise ValueError("creative generation source job is invalid")
        try:
            status = CreativeGenerationReceiptStatus(str(provider_status or "").strip())
        except ValueError as exc:
            raise ValueError("creative generation provider status is invalid") from exc
        if status not in {
            CreativeGenerationReceiptStatus.QUEUED,
            CreativeGenerationReceiptStatus.RUNNING,
            CreativeGenerationReceiptStatus.SUCCEEDED,
            CreativeGenerationReceiptStatus.FAILED,
        }:
            raise ValueError("creative generation provider status is invalid")
        if receipt.source_job_id and receipt.source_job_id != job_id:
            raise ValueError("creative generation source job changed")
        timestamp = str(now or _iso_now())
        if status == CreativeGenerationReceiptStatus.FAILED:
            # No recovery is possible or needed after provider failure; erase the
            # prompt/Brand DNA immediately instead of retaining terminal receipts.
            self._conn.execute(
                "DELETE FROM creative_generation_receipts WHERE id=? AND business_id=?",
                (receipt.id, current.business_id),
            )
            return replace(
                receipt,
                source_job_id=job_id,
                status=status,
                updated_at=timestamp,
            )
        self._conn.execute(
            """
            UPDATE creative_generation_receipts
            SET source_job_id=?, status=?, updated_at=?
            WHERE id=? AND business_id=?
            """,
            (job_id, status.value, timestamp, receipt.id, current.business_id),
        )
        return self.get(actor=current, receipt_id=receipt.id)

    def claim_delivery(
        self, *, actor: TenantContext, receipt_id: str, now: str | None = None
    ) -> bool:
        current = self._actor(actor)
        receipt = self.get(actor=current, receipt_id=receipt_id)
        if (
            receipt.status != CreativeGenerationReceiptStatus.SUCCEEDED
            or receipt.delivery_claimed_at
        ):
            return False
        timestamp = str(now or _iso_now())
        cursor = self._conn.execute(
            """
            UPDATE creative_generation_receipts
            SET delivery_claimed_at=?, updated_at=?
            WHERE id=? AND business_id=? AND status='succeeded'
              AND delivery_claimed_at=''
            """,
            (timestamp, timestamp, receipt.id, current.business_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1

    def authorize_redelivery(
        self, *, actor: TenantContext, receipt_id: str, now: str | None = None
    ) -> bool:
        current = self._actor(actor)
        receipt = self.get(actor=current, receipt_id=receipt_id)
        if (
            receipt.status != CreativeGenerationReceiptStatus.SUCCEEDED
            or not receipt.delivery_claimed_at
        ):
            return False
        timestamp = str(now or _iso_now())
        cursor = self._conn.execute(
            """
            UPDATE creative_generation_receipts
            SET delivery_claimed_at='', updated_at=?
            WHERE id=? AND business_id=? AND status='succeeded'
              AND delivery_claimed_at<>''
            """,
            (timestamp, receipt.id, current.business_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1

    def abandon(
        self, *, actor: TenantContext, receipt_id: str
    ) -> bool:
        current = self._actor(actor)
        receipt = self.get(actor=current, receipt_id=receipt_id)
        if receipt.status != CreativeGenerationReceiptStatus.SUCCEEDED:
            return False
        if receipt.delivery_claimed_at:
            sql = (
                "DELETE FROM creative_generation_receipts WHERE id=? AND business_id=? "
                "AND status='succeeded' AND delivery_claimed_at<>''"
            )
        else:
            sql = (
                "DELETE FROM creative_generation_receipts WHERE id=? AND business_id=? "
                "AND status='succeeded' AND delivery_claimed_at=''"
            )
        cursor = self._conn.execute(sql, (receipt.id, current.business_id))
        return int(getattr(cursor, "rowcount", 0) or 0) == 1

    def mark_delivered(
        self, *, actor: TenantContext, receipt_id: str, now: str | None = None
    ) -> CreativeGenerationReceipt:
        current = self._actor(actor)
        receipt = self.get(actor=current, receipt_id=receipt_id)
        if (
            receipt.status != CreativeGenerationReceiptStatus.SUCCEEDED
            or not receipt.delivery_claimed_at
        ):
            raise ValueError("creative generation delivery was not claimed")
        timestamp = str(now or _iso_now())
        self._conn.execute(
            "DELETE FROM creative_generation_receipts WHERE id=? AND business_id=?",
            (receipt.id, current.business_id),
        )
        return replace(
            receipt,
            status=CreativeGenerationReceiptStatus.DELIVERED,
            updated_at=timestamp,
        )


__all__ = ["CreativeGenerationReceiptRepository"]
