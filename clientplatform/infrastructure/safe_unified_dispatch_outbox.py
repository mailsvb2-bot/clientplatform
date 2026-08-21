from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from clientplatform.domain.connections import DispatchLeaseLost
from clientplatform.domain.customer_interactions import CustomerInteractionMessage
from clientplatform.domain.partners import (
    PartnerChannel,
    PartnerInvariantViolation,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.partner_repository import PartnerRepository
from clientplatform.infrastructure.sales_followup_repository import SalesFollowupRepository
from clientplatform.infrastructure.safe_dispatch_outbox import (
    DispatchOutboxRepository as _LessonDispatchOutboxRepository,
)
from clientplatform.infrastructure.unified_dispatch_outbox import (
    ClaimedProviderDispatch,
    DispatchOutboxRepository as _UnifiedDispatchOutboxRepository,
    ProviderDispatch,
    _provider_dispatch_from_row,
    _utc_now,
    _value,
)
from services.db.core import PostgresCompatConnection


_QUALIFIED_PROVIDER_COLUMNS = """
d.id, d.business_id, d.platform, d.source_kind, d.source_id, d.connection_id,
d.external_subject, d.payload_kind, d.payload_ref, d.idempotency_key,
d.status, d.attempts, d.available_at, d.locked_at, d.lock_token,
d.provider_message_id, d.last_error, d.created_at, d.updated_at, d.sent_at,
d.dead_at
""".strip()

_FOLLOWUP_NON_REPLAY_PLATFORMS = frozenset({"telegram", "max"})
_FOLLOWUP_PROVIDER_BOUNDARY_MARKER = "sales_followup_provider_call_started_non_idempotent"
_FOLLOWUP_AMBIGUOUS_ERROR = "sales_followup_delivery_outcome_ambiguous_manual_reconciliation_required"
_CUSTOMER_INTERACTION_PROVIDER_BOUNDARY_MARKER = (
    "customer_interaction_provider_call_started_non_idempotent"
)
_CUSTOMER_INTERACTION_AMBIGUOUS_ERROR = (
    "customer_interaction_delivery_outcome_ambiguous_manual_reconciliation_required"
)


_RETURNING_PROVIDER_COLUMNS = """
id, business_id, platform, source_kind, source_id, connection_id,
external_subject, payload_kind, payload_ref, idempotency_key,
status, attempts, available_at, locked_at, lock_token,
provider_message_id, last_error, created_at, updated_at, sent_at, dead_at
""".strip()

_PARTNER_AUTHORIZATION_SQL = """
d.source_kind!='partner_outreach'
OR (
    p.id IS NOT NULL
    AND p.competitor=0
    AND p.channel='telegram'
    AND p.contact_basis IN ('existing_relationship','opted_in')
    AND p.status NOT IN ('declined','do_not_contact','invalid')
    AND p.contact_value=d.external_subject
)
""".strip()


def _partner_recipient_idempotency_key(external_subject: str) -> str:
    """Return a bounded non-PII first-contact key for one Telegram recipient."""

    digest = hashlib.sha256(
        f"telegram\x00{external_subject}".encode("utf-8")
    ).hexdigest()[:32]
    return f"partner:telegram:{digest}:first-contact"


class DispatchOutboxRepository(_UnifiedDispatchOutboxRepository):
    """Production hardening for the staged lesson/partner dispatch rollout.

    The parent owns shared settlement/retry semantics. This wrapper keeps the
    public repository safe under multiple PostgreSQL workers and under operator
    revocation: first-contact enqueue is business-wide recipient-idempotent,
    partner work receives bounded batch capacity, and every claim revalidates
    first-contact authority.
    """

    def materialize_partner_outreach(
        self,
        *,
        actor: TenantContext,
        candidate_id: str,
        connection_id: str,
        now: str | None = None,
    ) -> ProviderDispatch:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        candidate = PartnerRepository(self._conn).get_candidate(
            actor=current,
            candidate_id=candidate_id,
        )
        if candidate.channel != PartnerChannel.TELEGRAM:
            raise PartnerInvariantViolation(
                "automatic partner outreach currently requires Telegram"
            )
        if not candidate.first_contact_permitted:
            raise PartnerInvariantViolation(
                "partner has not granted automatic first-contact authority"
            )
        external_subject = str(candidate.contact_value or "").strip()
        if not external_subject.lstrip("-").isdigit() or external_subject in {
            "",
            "0",
            "-0",
        }:
            raise PartnerInvariantViolation(
                "automatic Telegram outreach requires a verified numeric chat id"
            )
        digits = external_subject.lstrip("-")
        if len(digits) > 20 or digits.startswith("0"):
            raise PartnerInvariantViolation(
                "automatic Telegram outreach requires a verified numeric chat id"
            )

        normalized_connection = normalize_uuid(
            connection_id,
            field_name="connection_id",
        )
        connection = self._conn.execute(
            """
            SELECT platform, connection_type
            FROM connections
            WHERE id=? AND business_id=? AND status='active'
            LIMIT 1
            """,
            (normalized_connection, current.business_id),
        ).fetchone()
        if connection is None:
            raise PartnerInvariantViolation("active partner-send connection not found")
        if str(_value(connection, "platform", 0)) != "telegram" or str(
            _value(connection, "connection_type", 1)
        ) not in {"telegram_shared_bot", "telegram_managed_bot"}:
            raise PartnerInvariantViolation(
                "partner outreach requires an active Telegram bot connection"
            )

        pack = PartnerRepository(self._conn).get_content_pack(
            actor=current,
            candidate_id=candidate.id,
        )
        message = str(pack.outreach_message or "").strip()
        if not message or len(message) > 4096:
            raise PartnerInvariantViolation("partner outreach text is not sendable")

        timestamp = str(now or _utc_now().isoformat())
        idempotency_key = _partner_recipient_idempotency_key(external_subject)
        dispatch_id = str(uuid.uuid4())
        row = self._conn.execute(
            f"""
            INSERT INTO provider_dispatch_outbox(
                id,business_id,platform,source_kind,source_id,
                logical_delivery_id,partner_campaign_id,partner_candidate_id,
                connection_id,recipient_kind,customer_identity_id,
                external_subject,payload_kind,payload_ref,idempotency_key,
                status,attempts,available_at,locked_at,lock_token,
                provider_message_id,last_error,created_at,updated_at,sent_at,dead_at
            ) VALUES(
                ?,?,'telegram','partner_outreach',?,NULL,?,?,?,
                'external_subject',NULL,?,'text',?,?,'pending',0,?,
                NULL,NULL,NULL,NULL,?,?,NULL,NULL
            )
            ON CONFLICT(business_id,idempotency_key) DO UPDATE
            SET idempotency_key=excluded.idempotency_key
            RETURNING {_RETURNING_PROVIDER_COLUMNS}
            """,  # nosec B608 - static returning column list
            (
                dispatch_id,
                current.business_id,
                candidate.id,
                candidate.campaign_id,
                candidate.id,
                normalized_connection,
                external_subject,
                message,
                idempotency_key,
                timestamp,
                timestamp,
                timestamp,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("partner_dispatch_atomic_upsert_unavailable")
        persisted = _provider_dispatch_from_row(row)
        if persisted.source_id != candidate.id:
            raise PartnerInvariantViolation(
                "this Telegram contact already has a first-contact attempt"
            )
        return persisted

    def materialize_sales_followup(
        self,
        *,
        actor: TenantContext,
        followup_id: str,
        now: str | None = None,
    ) -> ProviderDispatch:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_customer_records()
        followups = SalesFollowupRepository(self._conn)
        followup = followups.get(actor=current, followup_id=followup_id)
        if followup.status.value not in {"scheduled", "queued"}:
            raise ValueError("sales follow-up is not queueable")
        identity = self._conn.execute(
            """
            SELECT external_subject
            FROM customer_identities
            WHERE id=? AND business_id=? AND platform=? AND status='active'
            LIMIT 1
            """,
            (followup.customer_identity_id, current.business_id, followup.platform),
        ).fetchone()
        if identity is None:
            raise ValueError("sales follow-up customer identity is no longer active")
        connection = self._conn.execute(
            """
            SELECT 1 FROM connections
            WHERE id=? AND business_id=? AND platform=? AND status='active'
            LIMIT 1
            """,
            (followup.connection_id, current.business_id, followup.platform),
        ).fetchone()
        if connection is None:
            raise ValueError("sales follow-up connection is no longer active")
        external_subject = str(_value(identity, "external_subject", 0))
        timestamp = str(now or _utc_now().isoformat())
        dispatch_id = str(uuid.uuid4())
        idempotency_key = f"sales-followup:{followup.id}"
        row = self._conn.execute(
            f"""
            INSERT INTO provider_dispatch_outbox(
                id,business_id,platform,source_kind,source_id,logical_delivery_id,
                partner_campaign_id,partner_candidate_id,sales_followup_id,
                connection_id,recipient_kind,customer_identity_id,external_subject,
                payload_kind,payload_ref,idempotency_key,status,attempts,available_at,
                locked_at,lock_token,provider_message_id,last_error,created_at,updated_at,
                sent_at,dead_at
            ) VALUES(
                ?,?,?, 'sales_followup', ?,NULL,NULL,NULL,?,?,
                'customer_identity',?,?,'text',?,?,'pending',0,?,
                NULL,NULL,NULL,NULL,?,?,NULL,NULL
            )
            ON CONFLICT(business_id,idempotency_key) DO UPDATE
            SET idempotency_key=excluded.idempotency_key
            RETURNING {_RETURNING_PROVIDER_COLUMNS}
            """,  # nosec B608 - static returning column list
            (
                dispatch_id,
                current.business_id,
                followup.platform,
                followup.id,
                followup.id,
                followup.connection_id,
                followup.customer_identity_id,
                external_subject,
                followup.message_text,
                idempotency_key,
                followup.scheduled_at,
                timestamp,
                timestamp,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("sales_followup_dispatch_atomic_upsert_unavailable")
        persisted = _provider_dispatch_from_row(row)
        if persisted.source_id != followup.id:
            raise ValueError("sales follow-up idempotency belongs to different work")
        self._conn.execute(
            """
            UPDATE clientplatform_sales_followups
            SET status='queued',provider_dispatch_id=?,queued_at=COALESCE(queued_at,?),updated_at=?
            WHERE id=? AND business_id=? AND status IN ('scheduled','queued')
            """,
            (persisted.id, timestamp, timestamp, followup.id, current.business_id),
        )
        return persisted

    def materialize_customer_interaction(
        self,
        *,
        business_id: str,
        connection_id: str,
        customer_identity_id: str,
        customer_id: str,
        platform: str,
        interaction: CustomerInteractionMessage,
        interaction_key: str,
        now: str | None = None,
    ) -> ProviderDispatch:
        """Queue one deterministic VK/MAX customer UI response in the canonical outbox."""

        business = normalize_uuid(business_id, field_name="business_id")
        connection = normalize_uuid(connection_id, field_name="connection_id")
        identity = normalize_uuid(
            customer_identity_id, field_name="customer_identity_id"
        )
        customer = normalize_uuid(customer_id, field_name="customer_id")
        channel = str(platform or "").strip().lower()
        if channel not in {"vk", "max"}:
            raise ValueError("customer interaction supports only VK or MAX")
        message = (
            interaction
            if isinstance(interaction, CustomerInteractionMessage)
            else CustomerInteractionMessage(**dict(interaction))
        )
        raw_key = str(interaction_key or "").strip()
        if not raw_key or len(raw_key) > 500:
            raise ValueError("interaction_key must be 1..500 characters")
        if any(ord(char) < 32 or ord(char) == 127 for char in raw_key):
            raise ValueError("interaction_key contains control characters")

        recipient = self._conn.execute(
            """
            SELECT ci.external_subject
            FROM customer_identities ci
            JOIN customers c
              ON c.id=ci.customer_id AND c.business_id=ci.business_id
             AND c.status='active'
            JOIN connections cn
              ON cn.id=? AND cn.business_id=ci.business_id
             AND cn.platform=ci.platform AND cn.status='active'
            WHERE ci.id=? AND ci.business_id=? AND ci.customer_id=?
              AND ci.platform=? AND ci.status='active'
            LIMIT 1
            """,
            (connection, identity, business, customer, channel),
        ).fetchone()
        if recipient is None:
            raise ValueError(
                "customer interaction requires an active tenant-scoped identity and connection"
            )
        external_subject = str(_value(recipient, "external_subject", 0)).strip()
        if not external_subject:
            raise ValueError("customer interaction recipient is empty")

        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        source_id = f"interaction:{digest[:40]}"
        idempotency_key = f"customer-interaction:{digest}"
        payload = message.to_json()
        timestamp = str(now or _utc_now().isoformat())
        dispatch_id = str(uuid.uuid4())
        row = self._conn.execute(
            f"""
            INSERT INTO provider_dispatch_outbox(
                id,business_id,platform,source_kind,source_id,logical_delivery_id,
                partner_campaign_id,partner_candidate_id,sales_followup_id,
                connection_id,recipient_kind,customer_identity_id,external_subject,
                payload_kind,payload_ref,idempotency_key,status,attempts,available_at,
                locked_at,lock_token,provider_message_id,last_error,created_at,updated_at,
                sent_at,dead_at
            ) VALUES(
                ?,?,?,'customer_interaction',?,NULL,NULL,NULL,NULL,?,
                'customer_identity',?,?,'mixed',?,?,'pending',0,?,
                NULL,NULL,NULL,NULL,?,?,NULL,NULL
            )
            ON CONFLICT(business_id,idempotency_key) DO UPDATE
            SET idempotency_key=excluded.idempotency_key
            RETURNING {_RETURNING_PROVIDER_COLUMNS}
            """,  # nosec B608 - static returning column list
            (
                dispatch_id,
                business,
                channel,
                source_id,
                connection,
                identity,
                external_subject,
                payload,
                idempotency_key,
                timestamp,
                timestamp,
                timestamp,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("customer_interaction_atomic_upsert_unavailable")
        persisted = _provider_dispatch_from_row(row)
        if (
            persisted.source_kind != "customer_interaction"
            or persisted.source_id != source_id
            or persisted.connection_id != connection
            or persisted.platform.value != channel
            or persisted.external_subject != external_subject
            or persisted.payload_ref != payload
        ):
            raise ValueError(
                "customer interaction idempotency belongs to different work"
            )
        return persisted

    def cancel_not_started_partner_outreach(
        self,
        *,
        actor: TenantContext,
        candidate_id: str,
        now: str | None = None,
    ) -> int:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        candidate = PartnerRepository(self._conn).get_candidate(
            actor=current,
            candidate_id=candidate_id,
        )
        timestamp = str(now or _utc_now().isoformat())
        cursor = self._conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
                last_error='partner_contact_revoked'
            WHERE business_id=? AND source_kind='partner_outreach'
              AND source_id=? AND status IN ('pending','retry')
            """,
            (timestamp, current.business_id, candidate.id),
        )
        return max(0, int(getattr(cursor, "rowcount", 0) or 0))

    def cancel_invalid_pending_partner_outreach(self, *, now: str | None = None) -> int:
        """Cancel queued partner work whose live authorization no longer matches."""

        timestamp = str(now or _utc_now().isoformat())
        cursor = self._conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
                last_error='partner_authorization_invalid'
            WHERE source_kind='partner_outreach' AND status IN ('pending','retry')
              AND NOT EXISTS (
                  SELECT 1
                  FROM partner_candidates p
                  JOIN connections c
                    ON c.id=provider_dispatch_outbox.connection_id
                   AND c.business_id=provider_dispatch_outbox.business_id
                   AND c.platform=provider_dispatch_outbox.platform
                  WHERE p.id=provider_dispatch_outbox.partner_candidate_id
                    AND p.business_id=provider_dispatch_outbox.business_id
                    AND c.status='active'
                    AND p.competitor=0 AND p.channel='telegram'
                    AND p.contact_basis IN ('existing_relationship','opted_in')
                    AND p.status NOT IN ('declined','do_not_contact','invalid')
                    AND p.contact_value=provider_dispatch_outbox.external_subject
              )
            """,
            (timestamp,),
        )
        return max(0, int(getattr(cursor, "rowcount", 0) or 0))

    def partner_dispatch_still_authorized(
        self,
        item: ClaimedProviderDispatch,
    ) -> bool:
        if item.dispatch.source_kind != "partner_outreach":
            return True
        row = self._conn.execute(
            """
            SELECT 1
            FROM provider_dispatch_outbox d
            JOIN partner_candidates p
              ON p.id=d.partner_candidate_id AND p.business_id=d.business_id
            JOIN connections c
              ON c.id=d.connection_id AND c.business_id=d.business_id
             AND c.platform=d.platform
            WHERE d.id=? AND d.business_id=? AND d.status='sending'
              AND d.lock_token=? AND c.status='active'
              AND p.competitor=0 AND p.channel='telegram'
              AND p.contact_basis IN ('existing_relationship','opted_in')
              AND p.status NOT IN ('declined','do_not_contact','invalid')
              AND p.contact_value=d.external_subject
            LIMIT 1
            """,
            (
                item.dispatch.id,
                item.dispatch.business_id,
                item.dispatch.lock_token,
            ),
        ).fetchone()
        return row is not None

    def cancel_revoked_leased_partner_outreach(
        self,
        item: ClaimedProviderDispatch,
        *,
        now: str | None = None,
    ) -> bool:
        if item.dispatch.source_kind != "partner_outreach":
            return False
        timestamp = str(now or _utc_now().isoformat())
        cursor = self._conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
                last_error='partner_contact_revoked'
            WHERE id=? AND business_id=? AND source_kind='partner_outreach'
              AND status='sending' AND lock_token=?
              AND NOT EXISTS (
                  SELECT 1
                  FROM partner_candidates p
                  JOIN connections c
                    ON c.id=provider_dispatch_outbox.connection_id
                   AND c.business_id=provider_dispatch_outbox.business_id
                   AND c.platform=provider_dispatch_outbox.platform
                  WHERE p.id=provider_dispatch_outbox.partner_candidate_id
                    AND p.business_id=provider_dispatch_outbox.business_id
                    AND c.status='active'
                    AND p.competitor=0 AND p.channel='telegram'
                    AND p.contact_basis IN ('existing_relationship','opted_in')
                    AND p.status NOT IN ('declined','do_not_contact','invalid')
                    AND p.contact_value=provider_dispatch_outbox.external_subject
              )
            """,
            (
                timestamp,
                item.dispatch.id,
                item.dispatch.business_id,
                item.dispatch.lock_token,
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1

    def sales_followup_claim_can_cross_provider_boundary(
        self,
        item: ClaimedProviderDispatch,
        *,
        now: str | None = None,
    ) -> bool:
        if item.dispatch.source_kind != "sales_followup":
            return True
        timestamp = str(now or _utc_now().isoformat())
        decision = SalesFollowupRepository(self._conn).decision_for_send(
            business_id=item.dispatch.business_id,
            followup_id=item.dispatch.source_id,
            now=timestamp,
        )
        if decision.allowed:
            return True
        if decision.defer_until:
            cursor = self._conn.execute(
                """
                UPDATE provider_dispatch_outbox
                SET status='retry',available_at=?,updated_at=?,locked_at=NULL,lock_token=NULL,
                    last_error='sales_followup_quiet_hours'
                WHERE id=? AND business_id=? AND source_kind='sales_followup'
                  AND status='sending' AND lock_token=?
                """,
                (
                    decision.defer_until,
                    timestamp,
                    item.dispatch.id,
                    item.dispatch.business_id,
                    item.dispatch.lock_token,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) == 1:
                self._conn.execute(
                    """
                    UPDATE clientplatform_sales_followups
                    SET scheduled_at=?,updated_at=?
                    WHERE id=? AND business_id=? AND status='queued'
                    """,
                    (decision.defer_until, timestamp, item.dispatch.source_id, item.dispatch.business_id),
                )
            return False
        reason = "contact_forbidden" if decision.stop_reason is None else decision.stop_reason.value
        cursor = self._conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,last_error=?
            WHERE id=? AND business_id=? AND source_kind='sales_followup'
              AND status='sending' AND lock_token=?
            """,
            (
                timestamp,
                f"sales_followup_{reason}",
                item.dispatch.id,
                item.dispatch.business_id,
                item.dispatch.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            return False
        self._conn.execute(
            """
            UPDATE clientplatform_sales_followups
            SET status='stopped',stopped_at=?,stop_reason=?,updated_at=?
            WHERE id=? AND business_id=? AND status='queued'
            """,
            (timestamp, reason, timestamp, item.dispatch.source_id, item.dispatch.business_id),
        )
        lead = self._conn.execute(
            "SELECT lead_id FROM clientplatform_sales_followups WHERE id=? AND business_id=? LIMIT 1",
            (item.dispatch.source_id, item.dispatch.business_id),
        ).fetchone()
        if lead is not None:
            lead_id = str(_value(lead, "lead_id", 0))
            self._conn.execute(
                """
                INSERT INTO clientplatform_sales_events(
                    id,business_id,lead_id,event_type,dedupe_key,payload_json,occurred_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(business_id,lead_id,dedupe_key) DO NOTHING
                """,
                (
                    str(uuid.uuid4()),
                    item.dispatch.business_id,
                    lead_id,
                    "followup_stopped",
                    f"followup-stop:{item.dispatch.source_id}:{reason}",
                    json.dumps({"followup_id": item.dispatch.source_id, "reason": reason}, sort_keys=True),
                    timestamp,
                ),
            )
        return False

    def mark_sales_followup_non_replay_boundary(
        self,
        item: ClaimedProviderDispatch,
        *,
        now: str | None = None,
    ) -> bool:
        if item.dispatch.source_kind != "sales_followup":
            return False
        if item.dispatch.platform.value not in _FOLLOWUP_NON_REPLAY_PLATFORMS:
            return False
        timestamp = str(now or _utc_now().isoformat())
        cursor = self._conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET last_error=?,updated_at=?
            WHERE id=? AND business_id=? AND source_kind='sales_followup'
              AND status='sending' AND lock_token=?
            """,
            (
                _FOLLOWUP_PROVIDER_BOUNDARY_MARKER,
                timestamp,
                item.dispatch.id,
                item.dispatch.business_id,
                item.dispatch.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise DispatchLeaseLost(
                "sales follow-up lease was lost before provider boundary"
            )
        return True

    def mark_provider_non_replay_boundary(
        self,
        item: ClaimedProviderDispatch,
        *,
        now: str | None = None,
    ) -> bool:
        if item.dispatch.source_kind == "sales_followup":
            return self.mark_sales_followup_non_replay_boundary(item, now=now)
        if (
            item.dispatch.source_kind != "customer_interaction"
            or item.dispatch.platform.value != "max"
        ):
            return False
        timestamp = str(now or _utc_now().isoformat())
        cursor = self._conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET last_error=?,updated_at=?
            WHERE id=? AND business_id=? AND source_kind='customer_interaction'
              AND platform='max' AND status='sending' AND lock_token=?
            """,
            (
                _CUSTOMER_INTERACTION_PROVIDER_BOUNDARY_MARKER,
                timestamp,
                item.dispatch.id,
                item.dispatch.business_id,
                item.dispatch.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise DispatchLeaseLost(
                "customer interaction lease was lost before provider boundary"
            )
        return True

    def mark_sent(
        self,
        item: Any,
        *,
        provider_message_id: str,
        now: datetime | None = None,
    ) -> Any:
        result = super().mark_sent(
            item,
            provider_message_id=provider_message_id,
            now=now,
        )
        if not isinstance(item, ClaimedProviderDispatch) or item.dispatch.source_kind != "sales_followup":
            return result
        stamp = (now or _utc_now()).replace(microsecond=0).isoformat()
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_followups
            SET status='sent',sent_at=?,updated_at=?
            WHERE id=? AND business_id=? AND status IN ('queued','stopped','cancelled')
            """,
            (stamp, stamp, item.dispatch.source_id, item.dispatch.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) == 1:
            row = self._conn.execute(
                "SELECT lead_id FROM clientplatform_sales_followups WHERE id=? AND business_id=? LIMIT 1",
                (item.dispatch.source_id, item.dispatch.business_id),
            ).fetchone()
            if row is not None:
                lead_id = str(_value(row, "lead_id", 0))
                self._conn.execute(
                    """
                    INSERT INTO clientplatform_sales_events(
                        id,business_id,lead_id,event_type,dedupe_key,payload_json,occurred_at
                    ) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(business_id,lead_id,dedupe_key) DO NOTHING
                    """,
                    (
                        str(uuid.uuid4()),
                        item.dispatch.business_id,
                        lead_id,
                        "followup_sent",
                        f"followup-sent:{item.dispatch.source_id}",
                        json.dumps({"followup_id": item.dispatch.source_id, "platform": item.dispatch.platform.value}, sort_keys=True),
                        stamp,
                    ),
                )
        return result

    def reschedule(
        self,
        item: Any,
        *,
        error: str,
        max_attempts: int = 8,
        now: datetime | None = None,
    ) -> Any:
        result = super().reschedule(
            item,
            error=error,
            max_attempts=max_attempts,
            now=now,
        )
        if not isinstance(item, ClaimedProviderDispatch) or item.dispatch.source_kind != "sales_followup":
            return result
        if str(getattr(result.status, "value", result.status)) != "dead":
            return result
        stamp = (now or _utc_now()).replace(microsecond=0).isoformat()
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_followups
            SET status='dead',stopped_at=?,stop_reason='delivery_failed',updated_at=?
            WHERE id=? AND business_id=? AND status IN ('queued','stopped','cancelled')
            """,
            (stamp, stamp, item.dispatch.source_id, item.dispatch.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) == 1:
            row = self._conn.execute(
                "SELECT lead_id FROM clientplatform_sales_followups WHERE id=? AND business_id=? LIMIT 1",
                (item.dispatch.source_id, item.dispatch.business_id),
            ).fetchone()
            if row is not None:
                lead_id = str(_value(row, "lead_id", 0))
                self._conn.execute(
                    """
                    INSERT INTO clientplatform_sales_events(
                        id,business_id,lead_id,event_type,dedupe_key,payload_json,occurred_at
                    ) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(business_id,lead_id,dedupe_key) DO NOTHING
                    """,
                    (
                        str(uuid.uuid4()),
                        item.dispatch.business_id,
                        lead_id,
                        "followup_delivery_failed",
                        f"followup-dead:{item.dispatch.source_id}",
                        json.dumps({"followup_id": item.dispatch.source_id}, sort_keys=True),
                        stamp,
                    ),
                )
        return result

    def claim_due(
        self,
        *,
        limit: int = 10,
        lock_ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> list[Any]:
        batch_limit = max(1, min(int(limit), 100))
        if not self._provider_table_available():
            return list(
                _LessonDispatchOutboxRepository.claim_due(
                    self,
                    limit=batch_limit,
                    lock_ttl_seconds=lock_ttl_seconds,
                    now=now,
                )
            )

        partner_reserve = max(1, batch_limit // 3)
        partner = self._claim_provider_due(
            limit=partner_reserve,
            lock_ttl_seconds=lock_ttl_seconds,
            now=now,
        )
        remaining = batch_limit - len(partner)
        lesson = list(
            _LessonDispatchOutboxRepository.claim_due(
                self,
                limit=remaining,
                lock_ttl_seconds=lock_ttl_seconds,
                now=now,
            )
        ) if remaining > 0 else []
        remaining = batch_limit - len(partner) - len(lesson)
        if remaining > 0:
            partner.extend(
                self._claim_provider_due(
                    limit=remaining,
                    lock_ttl_seconds=lock_ttl_seconds,
                    now=now,
                )
            )
        return [*lesson, *partner]

    def _sales_followup_table_available(self) -> bool:
        try:
            self._conn.execute(
                "SELECT 1 FROM clientplatform_sales_followups WHERE 1=0"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return False
            raise
        return True

    def _quarantine_stale_sales_followup_boundaries(
        self,
        *,
        stale_before: str,
        now: str,
    ) -> int:
        if not self._sales_followup_table_available():
            return 0
        rows = self._conn.execute(
            """
            SELECT d.id,d.business_id,d.source_id,f.lead_id
            FROM provider_dispatch_outbox d
            JOIN clientplatform_sales_followups f
              ON f.id=d.sales_followup_id AND f.business_id=d.business_id
            WHERE d.source_kind='sales_followup'
              AND d.platform IN ('telegram','max')
              AND d.status='sending' AND d.locked_at IS NOT NULL
              AND d.locked_at<=? AND d.last_error=?
            ORDER BY d.locked_at,d.id
            """,
            (stale_before, _FOLLOWUP_PROVIDER_BOUNDARY_MARKER),
        ).fetchall()
        quarantined = 0
        for row in rows:
            dispatch_id = str(_value(row, "id", 0))
            business_id = str(_value(row, "business_id", 1))
            followup_id = str(_value(row, "source_id", 2))
            lead_id = str(_value(row, "lead_id", 3))
            cursor = self._conn.execute(
                """
                UPDATE provider_dispatch_outbox
                SET status='dead',dead_at=?,updated_at=?,locked_at=NULL,
                    lock_token=NULL,last_error=?
                WHERE id=? AND business_id=? AND status='sending'
                  AND locked_at IS NOT NULL AND locked_at<=? AND last_error=?
                """,
                (
                    now,
                    now,
                    _FOLLOWUP_AMBIGUOUS_ERROR,
                    dispatch_id,
                    business_id,
                    stale_before,
                    _FOLLOWUP_PROVIDER_BOUNDARY_MARKER,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                continue
            quarantined += 1
            self._conn.execute(
                """
                UPDATE clientplatform_sales_followups
                SET status='dead',stopped_at=?,stop_reason='delivery_failed',updated_at=?
                WHERE id=? AND business_id=? AND status IN ('queued','stopped','cancelled')
                """,
                (now, now, followup_id, business_id),
            )
            self._conn.execute(
                """
                INSERT INTO clientplatform_sales_events(
                    id,business_id,lead_id,event_type,dedupe_key,payload_json,occurred_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(business_id,lead_id,dedupe_key) DO NOTHING
                """,
                (
                    str(uuid.uuid4()),
                    business_id,
                    lead_id,
                    "followup_delivery_ambiguous",
                    f"followup-ambiguous:{followup_id}",
                    json.dumps({"followup_id": followup_id}, sort_keys=True),
                    now,
                ),
            )
        return quarantined

    def _quarantine_stale_customer_interaction_boundaries(
        self,
        *,
        stale_before: str,
        now: str,
    ) -> int:
        cursor = self._conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET status='dead',dead_at=?,updated_at=?,locked_at=NULL,lock_token=NULL,
                last_error=?
            WHERE source_kind='customer_interaction' AND platform='max'
              AND status='sending' AND locked_at IS NOT NULL AND locked_at<=?
              AND last_error=?
            """,
            (
                now,
                now,
                _CUSTOMER_INTERACTION_AMBIGUOUS_ERROR,
                stale_before,
                _CUSTOMER_INTERACTION_PROVIDER_BOUNDARY_MARKER,
            ),
        )
        return max(0, int(getattr(cursor, "rowcount", 0) or 0))

    def _claim_provider_due(
        self,
        *,
        limit: int,
        lock_ttl_seconds: int,
        now: datetime | None,
    ) -> list[ClaimedProviderDispatch]:
        claim_now = (now or _utc_now()).replace(microsecond=0)
        now_iso = claim_now.isoformat()
        stale_before = (
            claim_now - timedelta(seconds=max(1, int(lock_ttl_seconds)))
        ).isoformat()
        self._quarantine_stale_sales_followup_boundaries(
            stale_before=stale_before,
            now=now_iso,
        )
        self._quarantine_stale_customer_interaction_boundaries(
            stale_before=stale_before,
            now=now_iso,
        )
        self.cancel_invalid_pending_partner_outreach(now=now_iso)
        token = uuid.uuid4().hex
        if isinstance(self._conn, PostgresCompatConnection):
            rows = self._conn.execute(
                f"""
                WITH due AS (
                    SELECT d.id
                    FROM provider_dispatch_outbox d
                    JOIN connections c
                      ON c.id=d.connection_id AND c.business_id=d.business_id
                     AND c.platform=d.platform AND c.status='active'
                    LEFT JOIN partner_candidates p
                      ON p.id=d.partner_candidate_id AND p.business_id=d.business_id
                    WHERE ({_PARTNER_AUTHORIZATION_SQL}) AND (
                        (d.status IN ('pending','retry') AND d.available_at<=?)
                        OR (d.status='sending' AND d.locked_at IS NOT NULL
                            AND d.locked_at<=?)
                    )
                    ORDER BY d.available_at,d.id
                    LIMIT ?
                    FOR UPDATE OF d SKIP LOCKED
                )
                UPDATE provider_dispatch_outbox d
                SET status='sending',locked_at=?,lock_token=?,updated_at=?
                FROM due
                WHERE d.id=due.id
                RETURNING d.id
                """,  # nosec B608 - static authorization expression
                (now_iso, stale_before, int(limit), now_iso, token, now_iso),
            ).fetchall()
            if not rows:
                return []
        else:
            rows = self._conn.execute(
                f"""
                SELECT d.id
                FROM provider_dispatch_outbox d
                JOIN connections c
                  ON c.id=d.connection_id AND c.business_id=d.business_id
                 AND c.platform=d.platform AND c.status='active'
                LEFT JOIN partner_candidates p
                  ON p.id=d.partner_candidate_id AND p.business_id=d.business_id
                WHERE ({_PARTNER_AUTHORIZATION_SQL}) AND (
                    (d.status IN ('pending','retry') AND d.available_at<=?)
                    OR (d.status='sending' AND d.locked_at IS NOT NULL
                        AND d.locked_at<=?)
                )
                ORDER BY d.available_at,d.id
                LIMIT ?
                """,  # nosec B608 - static authorization expression
                (now_iso, stale_before, int(limit)),
            ).fetchall()
            ids = [str(_value(row, "id", 0)) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(
                "UPDATE provider_dispatch_outbox "
                "SET status='sending',locked_at=?,lock_token=?,updated_at=? "
                f"WHERE id IN ({placeholders}) AND "  # nosec B608 - placeholders only
                "((status IN ('pending','retry') AND available_at<=?) OR "
                "(status='sending' AND locked_at IS NOT NULL AND locked_at<=?))",
                [now_iso, token, now_iso, *ids, now_iso, stale_before],
            )

        rows = self._conn.execute(
            f"""
            SELECT {_QUALIFIED_PROVIDER_COLUMNS}, c.credential_reference
            FROM provider_dispatch_outbox d
            JOIN connections c
              ON c.id=d.connection_id AND c.business_id=d.business_id
             AND c.platform=d.platform AND c.status='active'
            LEFT JOIN partner_candidates p
              ON p.id=d.partner_candidate_id AND p.business_id=d.business_id
            WHERE d.lock_token=? AND d.status='sending'
              AND ({_PARTNER_AUTHORIZATION_SQL})
            ORDER BY d.available_at,d.id
            """,  # nosec B608 - static column list/authorization expression
            (token,),
        ).fetchall()
        claimed: list[ClaimedProviderDispatch] = []
        for row in rows:
            dispatch = _provider_dispatch_from_row(row)
            claimed.append(
                ClaimedProviderDispatch(
                    dispatch=dispatch,
                    external_subject=dispatch.external_subject,
                    credential_reference=str(
                        _value(row, "credential_reference", 21)
                    ),
                )
            )
        return claimed


__all__ = ["DispatchOutboxRepository"]
