from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.sales_ai_policy import SalesAIDataMode, normalize_sales_ai_data_mode
from clientplatform.domain.tenancy import normalize_uuid


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


@dataclass(frozen=True, slots=True)
class SalesAIConsent:
    business_id: str
    enabled: bool
    consent_target: str
    consent_epoch: int
    data_mode: SalesAIDataMode
    customer_notice_confirmed: bool
    updated_by_member_id: str
    created_at: str
    updated_at: str


class SalesAIConsentRepository:
    """Durable consent projection and cross-process egress barrier.

    `lock_valid_consent` performs a no-op UPDATE on the tenant's consent row. The
    caller intentionally keeps that transaction open across the external provider
    call. A concurrent disable/provider change must update the same row and thus
    cannot complete until the in-flight request exits; after revocation commits,
    no later request can acquire a valid permit for the old epoch/target.
    """

    def __init__(self, conn: Any):
        self._conn = conn

    def get(self, *, business_id: str) -> SalesAIConsent | None:
        business = normalize_uuid(business_id, field_name="business_id")
        row = self._conn.execute(
            """
            SELECT business_id, enabled, consent_target, consent_epoch, data_mode,
                   customer_notice_confirmed, updated_by_member_id, created_at, updated_at
            FROM clientplatform_sales_ai_consents
            WHERE business_id=? LIMIT 1
            """,
            (business,),
        ).fetchone()
        if row is None:
            return None
        return SalesAIConsent(
            business_id=str(_value(row, "business_id", 0)),
            enabled=bool(_value(row, "enabled", 1)),
            consent_target=str(_value(row, "consent_target", 2) or ""),
            consent_epoch=int(_value(row, "consent_epoch", 3)),
            data_mode=SalesAIDataMode(str(_value(row, "data_mode", 4))),
            customer_notice_confirmed=bool(_value(row, "customer_notice_confirmed", 5)),
            updated_by_member_id=str(_value(row, "updated_by_member_id", 6)),
            created_at=str(_value(row, "created_at", 7)),
            updated_at=str(_value(row, "updated_at", 8)),
        )

    def set(
        self,
        *,
        business_id: str,
        enabled: bool,
        consent_target: str,
        data_mode: SalesAIDataMode | str,
        customer_notice_confirmed: bool,
        updated_by_member_id: str,
        now: str | None = None,
    ) -> SalesAIConsent:
        if not isinstance(enabled, bool) or not isinstance(customer_notice_confirmed, bool):
            raise ValueError("sales AI consent flags must be booleans")
        business = normalize_uuid(business_id, field_name="business_id")
        member = normalize_uuid(updated_by_member_id, field_name="member_id")
        mode = normalize_sales_ai_data_mode(data_mode)
        target = str(consent_target or "").strip()
        if enabled and mode != SalesAIDataMode.NO_CLOUD and not target:
            raise ValueError("enabled cloud Sales AI requires a consent target")
        if enabled and mode != SalesAIDataMode.NO_CLOUD and not customer_notice_confirmed:
            raise ValueError("customer-facing AI data notice must be confirmed before cloud AI is enabled")
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO clientplatform_sales_ai_consents(
                business_id, enabled, consent_target, consent_epoch, data_mode,
                customer_notice_confirmed, updated_by_member_id, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(business_id) DO UPDATE SET
                enabled=excluded.enabled,
                consent_target=excluded.consent_target,
                consent_epoch=clientplatform_sales_ai_consents.consent_epoch+1,
                data_mode=excluded.data_mode,
                customer_notice_confirmed=excluded.customer_notice_confirmed,
                updated_by_member_id=excluded.updated_by_member_id,
                updated_at=excluded.updated_at
            """,
            (
                business,
                1 if enabled else 0,
                target if enabled else "",
                1,
                mode.value,
                1 if customer_notice_confirmed else 0,
                member,
                timestamp,
                timestamp,
            ),
        )
        return self.get(business_id=business)  # type: ignore[return-value]

    def lock_valid_consent(
        self,
        *,
        business_id: str,
        consent_target: str,
        expected_epoch: int | None = None,
    ) -> SalesAIConsent:
        business = normalize_uuid(business_id, field_name="business_id")
        target = str(consent_target or "").strip()
        # UPDATE rather than SELECT is deliberate: Postgres takes a row lock and
        # SQLite takes the writer lock. Keep the surrounding transaction open
        # until the external request finishes.
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_ai_consents
            SET consent_epoch=consent_epoch
            WHERE business_id=? AND enabled=1 AND consent_target=?
              AND data_mode!='no_cloud'
            """,
            (business, target),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise PermissionError("sales AI consent is not valid for this provider target")
        consent = self.get(business_id=business)
        if consent is None or not consent.enabled or consent.consent_target != target:
            raise PermissionError("sales AI consent changed before egress")
        if expected_epoch is not None and consent.consent_epoch != expected_epoch:
            raise PermissionError("sales AI consent epoch changed before egress")
        return consent


__all__ = ["SalesAIConsent", "SalesAIConsentRepository"]
