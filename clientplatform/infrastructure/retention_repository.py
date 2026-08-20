from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.retention import RetentionCandidate, RetentionEvidence, classify_retention_evidence
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _parse_timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class RetentionRepository:
    """Deterministic U-010 retention candidates from canonical customer/outcome facts."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def list_candidates(
        self,
        *,
        actor: TenantContext,
        now: datetime,
        limit: int = 100,
    ) -> list[RetentionCandidate]:
        current = self._tenancy.resolve_context(user_id=actor.user_id, business_id=actor.business_id)
        current.assert_can_view_customer_records()
        normalized_limit = int(limit)
        if normalized_limit < 1 or normalized_limit > 500:
            raise ValueError("limit must be between 1 and 500")
        rows = self._conn.execute(
            """
            SELECT c.id,c.display_name,c.last_contact_at,
                   SUM(CASE WHEN o.outcome_type='order_paid' THEN 1 ELSE 0 END) AS paid_orders,
                   MAX(CASE WHEN o.outcome_type='order_paid' THEN o.occurred_at END) AS last_paid_at,
                   MAX(CASE WHEN o.outcome_type IN (
                       'booking_created','booking_confirmed','booking_completed',
                       'order_paid','customer_reactivated'
                   ) THEN o.occurred_at END) AS last_outcome_at
            FROM customers c
            LEFT JOIN business_outcome_events o
              ON o.business_id=c.business_id AND o.customer_id=c.id
            WHERE c.business_id=? AND c.status='active'
            GROUP BY c.id,c.display_name,c.last_contact_at
            HAVING SUM(CASE WHEN o.outcome_type='order_paid' THEN 1 ELSE 0 END) > 0
            """,
            (current.business_id,),
        ).fetchall()
        candidates: list[RetentionCandidate] = []
        for row in rows:
            last_paid_raw = _value(row, "last_paid_at", 4)
            if last_paid_raw is None:
                continue
            last_paid = _parse_timestamp(last_paid_raw)
            activity_values = [last_paid]
            for position, key in ((2, "last_contact_at"), (5, "last_outcome_at")):
                raw = _value(row, key, position)
                if raw is not None:
                    activity_values.append(_parse_timestamp(raw))
            evidence = RetentionEvidence(
                customer_id=str(_value(row, "id", 0)),
                display_name=_value(row, "display_name", 1),
                paid_orders=int(_value(row, "paid_orders", 3) or 0),
                last_paid_at=last_paid,
                last_activity_at=max(activity_values),
            )
            candidate = classify_retention_evidence(evidence, now=now)
            if candidate is not None:
                candidates.append(candidate)
        candidates.sort(key=lambda item: (item.last_activity_at, item.customer_id))
        return candidates[:normalized_limit]


__all__ = ["RetentionRepository"]
