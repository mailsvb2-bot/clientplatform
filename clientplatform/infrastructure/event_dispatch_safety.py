from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3
from typing import Any

from clientplatform.domain.automation_policy import (
    AutomationCandidateAction,
    AutomationPolicyError,
    evaluate_automation_policy,
)
from clientplatform.domain.connections import DispatchLeaseLost
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.infrastructure.automation_policy_repository import AutomationPolicyRepository
from clientplatform.infrastructure.unified_dispatch_outbox import ClaimedProviderDispatch

_COMMERCIAL_KEY_FRAGMENT = ":message:post:v4:stage:"
_LEGACY_OFFER_KEY_SUFFIX = ":message:after:v2"
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


def is_legacy_event_offer_dispatch(item: object) -> bool:
    return bool(
        isinstance(item, ClaimedProviderDispatch)
        and item.dispatch.source_kind == "event_message"
        and item.dispatch.idempotency_key.endswith(_LEGACY_OFFER_KEY_SUFFIX)
    )


def suppress_legacy_event_offer_dispatch(
    conn: Any,
    item: ClaimedProviderDispatch,
    *,
    now: str | None = None,
) -> bool:
    if not is_legacy_event_offer_dispatch(item):
        return False
    timestamp = str(now or _utc_now().isoformat())
    conn.execute(
        """
        UPDATE provider_dispatch_outbox
        SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
            last_error='legacy_event_offer_suppressed'
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
    return True


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _owner_actor(conn: Any, *, business_id: str) -> TenantContext | None:
    row = conn.execute(
        """
        SELECT bm.id,bm.user_id
        FROM business_members bm
        JOIN businesses b ON b.id=bm.business_id AND b.status='active'
        WHERE bm.business_id=? AND bm.role='owner' AND bm.status='active'
        ORDER BY bm.created_at,bm.id
        LIMIT 1
        """,
        (business_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return TenantContext(
            business_id=business_id,
            user_id=int(_value(row, "user_id", 1)),
            membership_id=str(_value(row, "id", 0)),
            role=PlatformRole.OWNER,
        )
    except ValueError:
        return None


def event_commercial_policy_authorized(
    conn: Any,
    *,
    business_id: str,
    registration_id: str,
    platform: str,
    payload_ref: str,
    scheduled_at: datetime | str,
    now: datetime | str | None = None,
) -> bool:
    """Evaluate the canonical owner-approved AutomationPolicy for one exact send."""

    owner = _owner_actor(conn, business_id=business_id)
    if owner is None:
        return False
    current = now if now is not None else scheduled_at
    try:
        automation_candidate = AutomationCandidateAction(
            business_id=business_id,
            action="sales.followup",
            external_write=True,
            channel=str(platform),
            audience="prospect_opted_in",
            scheduled_at=scheduled_at,
            content_topics=("service_offer",),
            subject_ref=f"event-registration:{registration_id}",
            payload_digest=hashlib.sha256(str(payload_ref).encode("utf-8")).hexdigest(),
        )
        repository = AutomationPolicyRepository(conn)
        policy = repository.effective(actor=owner, now=current)
        if policy is None:
            return False
        check = evaluate_automation_policy(
            policy=policy,
            candidate=automation_candidate,
            now=current,
        )
        if check.allowed:
            return True
        if not check.requires_approval:
            return False
        rows = conn.execute(
            """
            SELECT id
            FROM clientplatform_automation_action_approvals
            WHERE business_id=? AND candidate_hash=? AND status='approved'
            ORDER BY decided_at DESC,id DESC
            LIMIT 10
            """,
            (business_id, automation_candidate.candidate_hash),
        ).fetchall()
        for row in rows:
            approval_id = str(_value(row, "id", 0))
            try:
                repository.get_action_authorization(
                    actor=owner,
                    approval_id=approval_id,
                    expected_candidate_hash=automation_candidate.candidate_hash,
                    expected_subject_ref=str(automation_candidate.subject_ref),
                    expected_payload_digest=str(automation_candidate.payload_digest),
                    now=current,
                )
                return True
            except (AutomationPolicyError, ValueError):
                continue
    except (AutomationPolicyError, ValueError, sqlite3.OperationalError):
        return False
    return False


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
        policy_now = str(now or _utc_now().isoformat())
        if event_commercial_policy_authorized(
            conn,
            business_id=item.dispatch.business_id,
            registration_id=item.dispatch.source_id,
            platform=str(item.dispatch.platform),
            payload_ref=item.dispatch.payload_ref,
            scheduled_at=item.dispatch.available_at,
            now=policy_now,
        ):
            return True
        conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
                last_error='event_commercial_policy_not_authorized'
            WHERE id=? AND business_id=? AND source_kind='event_message'
              AND status='sending' AND lock_token=?
            """,
            (
                policy_now,
                item.dispatch.id,
                item.dispatch.business_id,
                item.dispatch.lock_token,
            ),
        )
        return False
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
    "event_commercial_policy_authorized",
    "is_commercial_event_dispatch",
    "is_legacy_event_offer_dispatch",
    "mark_event_commercial_non_replay_boundary",
    "quarantine_stale_event_commercial_boundaries",
    "suppress_legacy_event_offer_dispatch",
]
