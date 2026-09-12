from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any

from clientplatform.domain.connections import DispatchLeaseLost
from clientplatform.infrastructure.unified_dispatch_outbox import ClaimedProviderDispatch

_COMMERCIAL_KEY_FRAGMENT = ":message:post:v4:stage:"
_PROVIDER_BOUNDARY_MARKER = "event_commercial_provider_call_started_non_idempotent"
_AMBIGUOUS_ERROR = (
    "event_commercial_delivery_outcome_ambiguous_manual_reconciliation_required"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def is_commercial_event_dispatch(item: object) -> bool:
    return bool(
        isinstance(item, ClaimedProviderDispatch)
        and item.dispatch.source_kind == "event_message"
        and _COMMERCIAL_KEY_FRAGMENT in item.dispatch.idempotency_key
    )


def mark_event_commercial_non_replay_boundary(
    conn: Any,
    item: ClaimedProviderDispatch,
    *,
    now: str | None = None,
) -> bool:
    if not is_commercial_event_dispatch(item):
        return False
    timestamp = str(now or _utc_now().isoformat())
    cursor = conn.execute(
        """
        UPDATE provider_dispatch_outbox
        SET last_error=?,updated_at=?
        WHERE id=? AND business_id=? AND source_kind='event_message'
          AND status='sending' AND lock_token=?
        """,
        (
            _PROVIDER_BOUNDARY_MARKER,
            timestamp,
            item.dispatch.id,
            item.dispatch.business_id,
            item.dispatch.lock_token,
        ),
    )
    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
        raise DispatchLeaseLost("commercial event lease was lost before provider boundary")
    return True


def event_commercial_claim_can_cross_provider_boundary(
    conn: Any,
    item: ClaimedProviderDispatch,
    *,
    now: str | None = None,
) -> bool:
    """Revalidate event, recipient, payment and commercial consent live."""

    if not is_commercial_event_dispatch(item):
        return True
    row = conn.execute(
        """
        SELECT 1
        FROM provider_dispatch_outbox d
        JOIN clientplatform_event_registrations r
          ON r.id=d.source_id AND r.business_id=d.business_id
         AND r.status='registered'
        JOIN clientplatform_events e
          ON e.id=r.event_id AND e.business_id=r.business_id
         AND e.status IN ('published','completed')
        JOIN businesses b
          ON b.id=d.business_id AND b.status='active'
        JOIN connections c
          ON c.id=d.connection_id AND c.business_id=d.business_id
         AND c.platform=d.platform AND c.status='active'
        LEFT JOIN customer_identities ci
          ON ci.id=d.customer_identity_id AND ci.business_id=d.business_id
         AND ci.platform=d.platform AND ci.status='active'
        WHERE d.id=? AND d.business_id=? AND d.source_kind='event_message'
          AND d.status='sending' AND d.lock_token=?
          AND NOT EXISTS (
              SELECT 1 FROM clientplatform_event_conversion_links p
              WHERE p.business_id=r.business_id
                AND p.event_id=r.event_id
                AND p.registration_id=r.id
          )
          AND EXISTS (
              SELECT 1
              FROM clientplatform_event_commercial_channel_state cs
              WHERE cs.business_id=r.business_id
                AND cs.event_id=r.event_id
                AND cs.registration_id=r.id
                AND cs.platform=d.platform
                AND cs.status='active'
          )
          AND (
              (d.platform='email'
                  AND c.connection_type='email_smtp'
                  AND d.customer_identity_id IS NULL
                  AND r.email=d.external_subject)
              OR
              (d.platform IN ('vk','max','telegram')
                  AND r.customer_id IS NOT NULL
                  AND ci.customer_id=r.customer_id
                  AND ci.external_subject=d.external_subject)
          )
        LIMIT 1
        """,
        (
            item.dispatch.id,
            item.dispatch.business_id,
            item.dispatch.lock_token,
        ),
    ).fetchone()
    if row is not None:
        return True
    timestamp = str(now or _utc_now().isoformat())
    conn.execute(
        """
        UPDATE provider_dispatch_outbox
        SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
            last_error='event_message_authority_revoked_or_paid'
        WHERE id=? AND business_id=? AND source_kind='event_message'
          AND status='sending' AND lock_token=?
        """,
        (
            timestamp,
            item.dispatch.id,
            item.dispatch.business_id,
            item.dispatch.lock_token,
        ),
    )
    return False


def quarantine_stale_event_commercial_boundaries(
    conn: Any,
    *,
    lock_ttl_seconds: int,
    now: datetime | None = None,
) -> int:
    execute = getattr(conn, "execute", None)
    if not callable(execute):
        return 0
    claim_now = (now or _utc_now()).replace(microsecond=0)
    now_iso = claim_now.isoformat()
    stale_before = (
        claim_now - timedelta(seconds=max(1, int(lock_ttl_seconds)))
    ).isoformat()
    try:
        candidate = execute(
            """
            SELECT 1
            FROM provider_dispatch_outbox
            WHERE source_kind='event_message'
              AND idempotency_key LIKE 'event:%:message:post:v4:stage:%'
              AND status='sending' AND locked_at IS NOT NULL AND locked_at<=?
              AND last_error=?
            LIMIT 1
            """,
            (stale_before, _PROVIDER_BOUNDARY_MARKER),
        ).fetchone()
        if candidate is None:
            return 0
        cursor = execute(
            """
            UPDATE provider_dispatch_outbox
            SET status='dead',dead_at=?,updated_at=?,locked_at=NULL,lock_token=NULL,
                last_error=?
            WHERE source_kind='event_message'
              AND idempotency_key LIKE 'event:%:message:post:v4:stage:%'
              AND status='sending' AND locked_at IS NOT NULL AND locked_at<=?
              AND last_error=?
            """,
            (
                now_iso,
                now_iso,
                _AMBIGUOUS_ERROR,
                stale_before,
                _PROVIDER_BOUNDARY_MARKER,
            ),
        )
    except sqlite3.OperationalError as exc:
        if "no such table: provider_dispatch_outbox" in str(exc).lower():
            return 0
        raise
    return max(0, int(getattr(cursor, "rowcount", 0) or 0))


__all__ = [
    "event_commercial_claim_can_cross_provider_boundary",
    "is_commercial_event_dispatch",
    "mark_event_commercial_non_replay_boundary",
    "quarantine_stale_event_commercial_boundaries",
]
