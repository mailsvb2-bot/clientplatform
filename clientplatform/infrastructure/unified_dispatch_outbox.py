from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from clientplatform.domain.connections import (
    ConnectionPlatform,
    DispatchLeaseLost,
    DispatchStatus,
)
from clientplatform.domain.partners import (
    PartnerCandidateStatus,
    PartnerChannel,
    PartnerInvariantViolation,
    PartnerNotFound,
)
from clientplatform.domain.programs import ContentKind
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.partner_repository import PartnerRepository
from clientplatform.infrastructure.safe_dispatch_outbox import (
    DispatchOutboxRepository as _LessonDispatchOutboxRepository,
)
from services.db.core import PostgresCompatConnection


_TELEGRAM_CHAT_ID_RE = re.compile(r"-?[1-9][0-9]{0,19}")
_PARTNER_SOURCE_KIND = "partner_outreach"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


@dataclass(frozen=True, slots=True)
class ProviderDispatch:
    id: str
    business_id: str
    platform: ConnectionPlatform
    source_kind: str
    source_id: str
    connection_id: str
    external_subject: str
    payload_kind: ContentKind
    payload_ref: str
    idempotency_key: str
    status: DispatchStatus
    attempts: int
    available_at: str
    created_at: str
    updated_at: str
    locked_at: str | None = None
    lock_token: str | None = None
    provider_message_id: str | None = None
    last_error: str | None = None
    sent_at: str | None = None
    dead_at: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimedProviderDispatch:
    dispatch: ProviderDispatch
    external_subject: str
    credential_reference: str


def _provider_dispatch_from_row(row: Any) -> ProviderDispatch:
    return ProviderDispatch(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        platform=ConnectionPlatform(str(_value(row, "platform", 2))),
        source_kind=str(_value(row, "source_kind", 3)),
        source_id=str(_value(row, "source_id", 4)),
        connection_id=str(_value(row, "connection_id", 5)),
        external_subject=str(_value(row, "external_subject", 6)),
        payload_kind=ContentKind(str(_value(row, "payload_kind", 7))),
        payload_ref=str(_value(row, "payload_ref", 8)),
        idempotency_key=str(_value(row, "idempotency_key", 9)),
        status=DispatchStatus(str(_value(row, "status", 10))),
        attempts=int(_value(row, "attempts", 11) or 0),
        available_at=str(_value(row, "available_at", 12)),
        locked_at=None if _value(row, "locked_at", 13) is None else str(_value(row, "locked_at", 13)),
        lock_token=None if _value(row, "lock_token", 14) is None else str(_value(row, "lock_token", 14)),
        provider_message_id=None if _value(row, "provider_message_id", 15) is None else str(_value(row, "provider_message_id", 15)),
        last_error=None if _value(row, "last_error", 16) is None else str(_value(row, "last_error", 16)),
        created_at=str(_value(row, "created_at", 17)),
        updated_at=str(_value(row, "updated_at", 18)),
        sent_at=None if _value(row, "sent_at", 19) is None else str(_value(row, "sent_at", 19)),
        dead_at=None if _value(row, "dead_at", 20) is None else str(_value(row, "dead_at", 20)),
    )


_PROVIDER_COLUMNS = """
id, business_id, platform, source_kind, source_id, connection_id,
external_subject, payload_kind, payload_ref, idempotency_key, status, attempts,
available_at, locked_at, lock_token, provider_message_id, last_error,
created_at, updated_at, sent_at, dead_at
""".strip()


class DispatchOutboxRepository(_LessonDispatchOutboxRepository):
    """One transport repository over the staged lesson/partner storage rollout.

    Existing lesson rows remain in their historical table for duplicate-safe
    rollback. Partner work is stored in the generic provider table. Claiming,
    leases, retries, credential resolution and adapters are shared by the same
    worker, so this is not a second sender or a second transport stack.
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
        if not _TELEGRAM_CHAT_ID_RE.fullmatch(external_subject):
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
        existing = self._find_provider_by_idempotency(
            business_id=current.business_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return existing

        dispatch_id = str(uuid.uuid4())
        try:
            self._conn.execute(
                """
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
                """,
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
            )
        except sqlite3.IntegrityError:
            concurrent = self._find_provider_by_idempotency(
                business_id=current.business_id,
                idempotency_key=idempotency_key,
            )
            if concurrent is None:
                raise
            return concurrent
        return self._get_provider_dispatch(
            business_id=current.business_id,
            dispatch_id=dispatch_id,
        )

    def claim_due(
        self,
        *,
        limit: int = 10,
        lock_ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> list[Any]:
        batch_limit = max(1, min(int(limit), 100))
        lesson = super().claim_due(
            limit=batch_limit,
            lock_ttl_seconds=lock_ttl_seconds,
            now=now,
        )
        remaining = batch_limit - len(lesson)
        if remaining <= 0 or not self._provider_table_available():
            return list(lesson)
        partner = self._claim_provider_due(
            limit=remaining,
            lock_ttl_seconds=lock_ttl_seconds,
            now=now,
        )
        return [*lesson, *partner]

    def mark_sent(
        self,
        item: Any,
        *,
        provider_message_id: str,
        now: datetime | None = None,
    ) -> Any:
        if isinstance(item, ClaimedProviderDispatch):
            return self._mark_provider_sent(
                item,
                provider_message_id=provider_message_id,
                now=now,
            )
        return super().mark_sent(
            item,
            provider_message_id=provider_message_id,
            now=now,
        )

    def reschedule(
        self,
        item: Any,
        *,
        error: str,
        max_attempts: int = 8,
        now: datetime | None = None,
    ) -> Any:
        if isinstance(item, ClaimedProviderDispatch):
            return self._reschedule_provider(
                item,
                error=error,
                max_attempts=max_attempts,
                now=now,
            )
        return super().reschedule(
            item,
            error=error,
            max_attempts=max_attempts,
            now=now,
        )

    def release_lease(
        self,
        item: Any,
        *,
        reason: str = "worker_shutdown",
        now: datetime | None = None,
    ) -> Any:
        if isinstance(item, ClaimedProviderDispatch):
            return self._release_provider_lease(item, reason=reason, now=now)
        return super().release_lease(item, reason=reason, now=now)

    def _provider_table_available(self) -> bool:
        try:
            self._conn.execute(
                "SELECT 1 FROM provider_dispatch_outbox WHERE 1=0"
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return True

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
            SELECT {_PROVIDER_COLUMNS}, c.credential_reference
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
                    credential_reference=str(_value(row, "credential_reference", 21)),
                )
            )
        return claimed

    def _mark_provider_sent(
        self,
        item: ClaimedProviderDispatch,
        *,
        provider_message_id: str,
        now: datetime | None,
    ) -> ProviderDispatch:
        sent_at = (now or _utc_now()).replace(microsecond=0).isoformat()
        message_id = str(provider_message_id or "").strip()
        if not message_id:
            raise ValueError("provider_message_id must not be empty")
        cursor = self._conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET status='sent',provider_message_id=?,sent_at=?,updated_at=?,
                locked_at=NULL,lock_token=NULL,last_error=NULL
            WHERE id=? AND business_id=? AND status='sending' AND lock_token=?
            """,
            (
                message_id[:512],
                sent_at,
                sent_at,
                item.dispatch.id,
                item.dispatch.business_id,
                item.dispatch.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise DispatchLeaseLost("dispatch lease was lost before success")
        if item.dispatch.source_kind == _PARTNER_SOURCE_KIND:
            self._conn.execute(
                """
                UPDATE partner_candidates
                SET status='contacted',updated_at=?
                WHERE id=? AND business_id=? AND status='ready'
                """,
                (sent_at, item.dispatch.source_id, item.dispatch.business_id),
            )
        self._conn.execute(
            """
            UPDATE connections
            SET last_success_at=?,last_error_at=NULL,last_error_code=NULL,updated_at=?
            WHERE id=? AND business_id=?
            """,
            (
                sent_at,
                sent_at,
                item.dispatch.connection_id,
                item.dispatch.business_id,
            ),
        )
        return self._get_provider_dispatch(
            business_id=item.dispatch.business_id,
            dispatch_id=item.dispatch.id,
        )

    def _reschedule_provider(
        self,
        item: ClaimedProviderDispatch,
        *,
        error: str,
        max_attempts: int,
        now: datetime | None,
    ) -> ProviderDispatch:
        failed_at = (now or _utc_now()).replace(microsecond=0)
        attempts = item.dispatch.attempts + 1
        terminal = attempts >= max(1, int(max_attempts))
        delay_seconds = min(5 * (2 ** max(0, attempts - 1)), 900)
        available_at = (failed_at + timedelta(seconds=delay_seconds)).isoformat()
        status = DispatchStatus.DEAD if terminal else DispatchStatus.RETRY
        error_text = str(error or "dispatch_failed").strip()[:1000]
        cursor = self._conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET status=?,attempts=?,available_at=?,updated_at=?,locked_at=NULL,
                lock_token=NULL,last_error=?,dead_at=?
            WHERE id=? AND business_id=? AND status='sending' AND lock_token=?
            """,
            (
                status.value,
                attempts,
                available_at,
                failed_at.isoformat(),
                error_text,
                failed_at.isoformat() if terminal else None,
                item.dispatch.id,
                item.dispatch.business_id,
                item.dispatch.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise DispatchLeaseLost("dispatch lease was lost before retry")
        if terminal:
            self._conn.execute(
                """
                UPDATE connections
                SET status='attention',last_error_at=?,last_error_code=?,updated_at=?
                WHERE id=? AND business_id=? AND status='active'
                """,
                (
                    failed_at.isoformat(),
                    error_text[:240],
                    failed_at.isoformat(),
                    item.dispatch.connection_id,
                    item.dispatch.business_id,
                ),
            )
        else:
            self._conn.execute(
                """
                UPDATE connections
                SET last_error_at=?,last_error_code=?,updated_at=?
                WHERE id=? AND business_id=? AND status='active'
                """,
                (
                    failed_at.isoformat(),
                    error_text[:240],
                    failed_at.isoformat(),
                    item.dispatch.connection_id,
                    item.dispatch.business_id,
                ),
            )
        return self._get_provider_dispatch(
            business_id=item.dispatch.business_id,
            dispatch_id=item.dispatch.id,
        )

    def _release_provider_lease(
        self,
        item: ClaimedProviderDispatch,
        *,
        reason: str,
        now: datetime | None,
    ) -> ProviderDispatch:
        released = (now or _utc_now()).replace(microsecond=0).isoformat()
        cursor = self._conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET status='retry',available_at=?,updated_at=?,locked_at=NULL,
                lock_token=NULL,last_error=?
            WHERE id=? AND business_id=? AND status='sending' AND lock_token=?
            """,
            (
                released,
                released,
                str(reason or "worker_shutdown")[:1000],
                item.dispatch.id,
                item.dispatch.business_id,
                item.dispatch.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise DispatchLeaseLost("dispatch lease was lost before release")
        return self._get_provider_dispatch(
            business_id=item.dispatch.business_id,
            dispatch_id=item.dispatch.id,
        )

    def _find_provider_by_idempotency(
        self,
        *,
        business_id: str,
        idempotency_key: str,
    ) -> ProviderDispatch | None:
        row = self._conn.execute(
            f"SELECT {_PROVIDER_COLUMNS} FROM provider_dispatch_outbox "
            "WHERE business_id=? AND idempotency_key=? LIMIT 1",  # nosec B608
            (business_id, idempotency_key),
        ).fetchone()
        return None if row is None else _provider_dispatch_from_row(row)

    def _get_provider_dispatch(
        self,
        *,
        business_id: str,
        dispatch_id: str,
    ) -> ProviderDispatch:
        row = self._conn.execute(
            f"SELECT {_PROVIDER_COLUMNS} FROM provider_dispatch_outbox "
            "WHERE id=? AND business_id=? LIMIT 1",  # nosec B608
            (dispatch_id, business_id),
        ).fetchone()
        if row is None:
            raise PartnerNotFound("partner dispatch was not found")
        return _provider_dispatch_from_row(row)


__all__ = [
    "ClaimedProviderDispatch",
    "DispatchOutboxRepository",
    "ProviderDispatch",
]
