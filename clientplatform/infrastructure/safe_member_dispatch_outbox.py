from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Any

from clientplatform.domain.connections import DispatchLeaseLost
from clientplatform.domain.customer_interactions import CustomerInteractionMessage
from clientplatform.domain.tenancy import normalize_user_id, normalize_uuid
from clientplatform.infrastructure.safe_unified_dispatch_outbox import (
    DispatchOutboxRepository as _SafeUnifiedDispatchOutboxRepository,
)
from clientplatform.infrastructure.unified_dispatch_outbox import (
    ClaimedProviderDispatch,
    ProviderDispatch,
    _provider_dispatch_from_row,
    _utc_now,
)


_RETURNING_PROVIDER_COLUMNS = """
id, business_id, platform, source_kind, source_id, connection_id,
external_subject, payload_kind, payload_ref, idempotency_key,
status, attempts, available_at, locked_at, lock_token,
provider_message_id, last_error, created_at, updated_at, sent_at, dead_at
""".strip()

_MEMBER_INTERACTION_PROVIDER_BOUNDARY_MARKER = (
    "member_interaction_provider_call_started_non_idempotent"
)
_MEMBER_INTERACTION_AMBIGUOUS_ERROR = (
    "member_interaction_delivery_outcome_ambiguous_manual_reconciliation_required"
)
_NATIVE_INTERACTION_SOURCE_KINDS = frozenset(
    {"customer_interaction", "member_interaction"}
)


class DispatchOutboxRepository(_SafeUnifiedDispatchOutboxRepository):
    """Add staff-facing VK/MAX interaction work to the canonical provider outbox."""

    def materialize_member_interaction(
        self,
        *,
        business_id: str,
        connection_id: str,
        member_user_id: int,
        platform: str,
        external_subject: str,
        interaction: CustomerInteractionMessage,
        interaction_key: str,
        now: str | None = None,
    ) -> ProviderDispatch:
        business = normalize_uuid(business_id, field_name="business_id")
        connection = normalize_uuid(connection_id, field_name="connection_id")
        member = normalize_user_id(member_user_id)
        channel = str(platform or "").strip().lower()
        if channel not in {"vk", "max"}:
            raise ValueError("member interaction supports only VK or MAX")

        subject = str(external_subject or "").strip()
        if not subject or len(subject) > 512:
            raise ValueError(
                "member interaction external_subject must be 1..512 characters"
            )
        if any(ord(char) < 32 or ord(char) == 127 for char in subject):
            raise ValueError(
                "member interaction external_subject contains control characters"
            )

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
            SELECT 1
            FROM business_members bm
            JOIN businesses b
              ON b.id=bm.business_id AND b.status='active'
            JOIN connections cn
              ON cn.id=? AND cn.business_id=bm.business_id
             AND cn.platform=? AND cn.status='active'
            JOIN account_channel_identities aci
              ON aci.account_id=bm.user_id
             AND aci.platform=cn.platform
             AND aci.external_user_id=?
            JOIN accounts a
              ON a.account_id=bm.user_id AND a.status='active'
            WHERE bm.business_id=? AND bm.user_id=? AND bm.status='active'
            LIMIT 1
            """,
            (connection, channel, subject, business, member),
        ).fetchone()
        if recipient is None:
            raise ValueError(
                "member interaction requires an active tenant member, "
                "account identity and connection"
            )

        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        source_id = f"member:{member}:{digest[:32]}"
        idempotency_key = f"member-interaction:{digest}"
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
                ?,?,?,'member_interaction',?,NULL,NULL,NULL,NULL,?,
                'external_subject',NULL,?,'mixed',?,?,'pending',0,?,
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
                subject,
                payload,
                idempotency_key,
                timestamp,
                timestamp,
                timestamp,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("member_interaction_atomic_upsert_unavailable")

        persisted = _provider_dispatch_from_row(row)
        if (
            persisted.source_kind != "member_interaction"
            or persisted.source_id != source_id
            or persisted.connection_id != connection
            or persisted.platform.value != channel
            or persisted.external_subject != subject
            or persisted.payload_ref != payload
        ):
            raise ValueError("member interaction idempotency belongs to different work")
        return persisted

    def native_interaction_claim_can_cross_provider_boundary(
        self,
        item: ClaimedProviderDispatch,
        *,
        now: str | None = None,
    ) -> bool:
        """Revalidate customer/staff recipient authority immediately before provider I/O."""

        source_kind = item.dispatch.source_kind
        if source_kind not in _NATIVE_INTERACTION_SOURCE_KINDS:
            return True
        if source_kind == "customer_interaction":
            row = self._conn.execute(
                """
                SELECT 1
                FROM provider_dispatch_outbox d
                JOIN connections cn
                  ON cn.id=d.connection_id AND cn.business_id=d.business_id
                 AND cn.platform=d.platform AND cn.status='active'
                JOIN customer_identities ci
                  ON ci.id=d.customer_identity_id AND ci.business_id=d.business_id
                 AND ci.platform=d.platform AND ci.status='active'
                 AND ci.external_subject=d.external_subject
                JOIN customers c
                  ON c.id=ci.customer_id AND c.business_id=ci.business_id
                 AND c.status='active'
                WHERE d.id=? AND d.business_id=?
                  AND d.source_kind='customer_interaction'
                  AND d.status='sending' AND d.lock_token=?
                LIMIT 1
                """,
                (
                    item.dispatch.id,
                    item.dispatch.business_id,
                    item.dispatch.lock_token,
                ),
            ).fetchone()
            reason = "customer_interaction_recipient_revoked"
        else:
            row = self._conn.execute(
                """
                SELECT 1
                FROM provider_dispatch_outbox d
                JOIN businesses b
                  ON b.id=d.business_id AND b.status='active'
                JOIN connections cn
                  ON cn.id=d.connection_id AND cn.business_id=d.business_id
                 AND cn.platform=d.platform AND cn.status='active'
                JOIN account_channel_identities aci
                  ON aci.platform=d.platform
                 AND aci.external_user_id=d.external_subject
                JOIN accounts a
                  ON a.account_id=aci.account_id AND a.status='active'
                JOIN business_members bm
                  ON bm.business_id=d.business_id AND bm.user_id=aci.account_id
                 AND bm.status='active'
                WHERE d.id=? AND d.business_id=?
                  AND d.source_kind='member_interaction'
                  AND d.status='sending' AND d.lock_token=?
                LIMIT 1
                """,
                (
                    item.dispatch.id,
                    item.dispatch.business_id,
                    item.dispatch.lock_token,
                ),
            ).fetchone()
            reason = "member_interaction_recipient_revoked"
        if row is not None:
            return True

        timestamp = str(now or _utc_now().isoformat())
        cursor = self._conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
                last_error=?
            WHERE id=? AND business_id=? AND source_kind=?
              AND status='sending' AND lock_token=?
            """,
            (
                timestamp,
                reason,
                item.dispatch.id,
                item.dispatch.business_id,
                source_kind,
                item.dispatch.lock_token,
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) != 0 and False

    def mark_provider_non_replay_boundary(
        self,
        item: ClaimedProviderDispatch,
        *,
        now: str | None = None,
    ) -> bool:
        if item.dispatch.source_kind != "member_interaction":
            return super().mark_provider_non_replay_boundary(item, now=now)
        if item.dispatch.platform.value != "max":
            return False

        timestamp = str(now or _utc_now().isoformat())
        cursor = self._conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET last_error=?,updated_at=?
            WHERE id=? AND business_id=? AND source_kind='member_interaction'
              AND platform='max' AND status='sending' AND lock_token=?
            """,
            (
                _MEMBER_INTERACTION_PROVIDER_BOUNDARY_MARKER,
                timestamp,
                item.dispatch.id,
                item.dispatch.business_id,
                item.dispatch.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise DispatchLeaseLost(
                "member interaction lease was lost before provider boundary"
            )
        return True

    def _quarantine_stale_member_interaction_boundaries(
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
            WHERE source_kind='member_interaction' AND platform='max'
              AND status='sending' AND locked_at IS NOT NULL AND locked_at<=?
              AND last_error=?
            """,
            (
                now,
                now,
                _MEMBER_INTERACTION_AMBIGUOUS_ERROR,
                stale_before,
                _MEMBER_INTERACTION_PROVIDER_BOUNDARY_MARKER,
            ),
        )
        return max(0, int(getattr(cursor, "rowcount", 0) or 0))

    def claim_due(
        self,
        *,
        limit: int = 10,
        lock_ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> list[Any]:
        claim_now = (now or _utc_now()).replace(microsecond=0)
        if not self._provider_table_available():
            return super().claim_due(
                limit=limit,
                lock_ttl_seconds=lock_ttl_seconds,
                now=claim_now,
            )
        stale_before = (
            claim_now - timedelta(seconds=max(1, int(lock_ttl_seconds)))
        ).isoformat()
        self._quarantine_stale_member_interaction_boundaries(
            stale_before=stale_before,
            now=claim_now.isoformat(),
        )
        return super().claim_due(
            limit=limit,
            lock_ttl_seconds=lock_ttl_seconds,
            now=claim_now,
        )


__all__ = ["DispatchOutboxRepository"]
