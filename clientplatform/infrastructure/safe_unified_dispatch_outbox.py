from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from clientplatform.domain.partners import (
    PartnerChannel,
    PartnerInvariantViolation,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.partner_repository import PartnerRepository
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

_RETURNING_PROVIDER_COLUMNS = """
id, business_id, platform, source_kind, source_id, connection_id,
external_subject, payload_kind, payload_ref, idempotency_key,
status, attempts, available_at, locked_at, lock_token,
provider_message_id, last_error, created_at, updated_at, sent_at, dead_at
""".strip()


class DispatchOutboxRepository(_UnifiedDispatchOutboxRepository):
    """Production hardening for the staged lesson/partner dispatch rollout.

    The parent owns the shared settlement/retry semantics. This wrapper keeps
    the public repository safe under multiple PostgreSQL workers: enqueue is
    conflict-idempotent, partner work receives bounded batch capacity, and all
    joined provider columns are explicitly qualified.
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
        idempotency_key = (
            f"partner:{candidate.id}:first-contact:connection:{normalized_connection}"
        )
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
        return _provider_dispatch_from_row(row)

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
        token = uuid.uuid4().hex
        if isinstance(self._conn, PostgresCompatConnection):
            rows = self._conn.execute(
                """
                WITH due AS (
                    SELECT d.id
                    FROM provider_dispatch_outbox d
                    JOIN connections c
                      ON c.id=d.connection_id AND c.business_id=d.business_id
                     AND c.platform=d.platform AND c.status='active'
                    WHERE (
                        (d.status IN ('pending','retry') AND d.available_at<=?)
                        OR (d.status='sending' AND d.locked_at IS NOT NULL
                            AND d.locked_at<=?)
                    )
                    ORDER BY d.available_at,d.id
                    LIMIT ?
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE provider_dispatch_outbox d
                SET status='sending',locked_at=?,lock_token=?,updated_at=?
                FROM due
                WHERE d.id=due.id
                RETURNING d.id
                """,
                (now_iso, stale_before, int(limit), now_iso, token, now_iso),
            ).fetchall()
            if not rows:
                return []
        else:
            rows = self._conn.execute(
                """
                SELECT d.id
                FROM provider_dispatch_outbox d
                JOIN connections c
                  ON c.id=d.connection_id AND c.business_id=d.business_id
                 AND c.platform=d.platform AND c.status='active'
                WHERE (
                    (d.status IN ('pending','retry') AND d.available_at<=?)
                    OR (d.status='sending' AND d.locked_at IS NOT NULL
                        AND d.locked_at<=?)
                )
                ORDER BY d.available_at,d.id
                LIMIT ?
                """,
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
            WHERE d.lock_token=? AND d.status='sending'
            ORDER BY d.available_at,d.id
            """,  # nosec B608 - static column list
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