from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from a1.domain.connections import (
    ClaimedDispatch,
    ConnectionNotFound,
    ConnectionPlatform,
    Dispatch,
    DispatchInvariantViolation,
    DispatchLeaseLost,
    DispatchNotFound,
    DispatchStatus,
)
from a1.domain.programs import ContentKind, DeliveryStatus
from a1.domain.tenancy import TenantContext, normalize_uuid
from a1.infrastructure import TenancyRepository
from services.db.runtime import CONFIG


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _optional(row: Any, key: str, position: int) -> str | None:
    value = _value(row, key, position)
    return None if value is None else str(value)


def _dispatch_from_row(row: Any) -> Dispatch:
    return Dispatch(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        platform=ConnectionPlatform(str(_value(row, "platform", 2))),
        logical_delivery_id=str(_value(row, "logical_delivery_id", 3)),
        connection_id=str(_value(row, "connection_id", 4)),
        customer_identity_id=str(_value(row, "customer_identity_id", 5)),
        payload_kind=ContentKind(str(_value(row, "payload_kind", 6))),
        payload_ref=str(_value(row, "payload_ref", 7)),
        idempotency_key=str(_value(row, "idempotency_key", 8)),
        status=DispatchStatus(str(_value(row, "status", 9))),
        attempts=int(_value(row, "attempts", 10)),
        available_at=str(_value(row, "available_at", 11)),
        locked_at=_optional(row, "locked_at", 12),
        lock_token=_optional(row, "lock_token", 13),
        provider_message_id=_optional(row, "provider_message_id", 14),
        last_error=_optional(row, "last_error", 15),
        created_at=str(_value(row, "created_at", 16)),
        updated_at=str(_value(row, "updated_at", 17)),
        sent_at=_optional(row, "sent_at", 18),
        dead_at=_optional(row, "dead_at", 19),
    )


_DISPATCH_COLUMNS = """
id, business_id, platform, logical_delivery_id, connection_id,
customer_identity_id, payload_kind, payload_ref, idempotency_key,
status, attempts, available_at, locked_at, lock_token,
provider_message_id, last_error, created_at, updated_at, sent_at, dead_at
""".strip()


class DispatchOutboxRepository:
    """Materialize and lease provider delivery without exposing raw credentials."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def materialize(
        self,
        *,
        actor: TenantContext,
        logical_delivery_id: str,
        connection_id: str,
        customer_identity_id: str,
        now: str | None = None,
    ) -> Dispatch:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_deliveries()
        normalized_delivery_id = normalize_uuid(
            logical_delivery_id,
            field_name="logical_delivery_id",
        )
        normalized_connection_id = normalize_uuid(
            connection_id,
            field_name="connection_id",
        )
        normalized_identity_id = normalize_uuid(
            customer_identity_id,
            field_name="customer_identity_id",
        )
        timestamp = str(now or _utc_now().isoformat())

        logical = self._conn.execute(
            """
            SELECT d.id, d.status, d.enrollment_id, d.lesson_id,
                   e.customer_id, l.content_kind, l.content_ref
            FROM lesson_deliveries d
            JOIN enrollments e
              ON e.id=d.enrollment_id AND e.business_id=d.business_id
             AND e.program_id=d.program_id
            JOIN lessons l
              ON l.id=d.lesson_id AND l.business_id=d.business_id
             AND l.program_id=d.program_id
            WHERE d.id=? AND d.business_id=?
            LIMIT 1
            """,
            (normalized_delivery_id, current.business_id),
        ).fetchone()
        if logical is None:
            raise DispatchNotFound(
                "logical lesson delivery was not found in the business"
            )
        logical_status = DeliveryStatus(str(_value(logical, "status", 1)))
        if logical_status not in {DeliveryStatus.PENDING, DeliveryStatus.FAILED}:
            raise DispatchInvariantViolation(
                "only pending or failed logical delivery can be dispatched"
            )

        connection = self._conn.execute(
            """
            SELECT platform
            FROM connections
            WHERE id=? AND business_id=? AND status='active'
            LIMIT 1
            """,
            (normalized_connection_id, current.business_id),
        ).fetchone()
        if connection is None:
            raise ConnectionNotFound(
                "active connection was not found in the business"
            )
        platform = ConnectionPlatform(str(_value(connection, "platform", 0)))

        identity = self._conn.execute(
            """
            SELECT customer_id, platform
            FROM customer_identities
            WHERE id=? AND business_id=? AND status='active'
            LIMIT 1
            """,
            (normalized_identity_id, current.business_id),
        ).fetchone()
        if identity is None:
            raise DispatchNotFound(
                "active customer identity was not found in the business"
            )
        if str(_value(identity, "customer_id", 0)) != str(
            _value(logical, "customer_id", 4)
        ):
            raise DispatchInvariantViolation(
                "customer identity belongs to another customer"
            )
        if ConnectionPlatform(str(_value(identity, "platform", 1))) != platform:
            raise DispatchInvariantViolation(
                "connection and customer identity platforms do not match"
            )

        existing = self._find_materialized(
            business_id=current.business_id,
            logical_delivery_id=normalized_delivery_id,
            connection_id=normalized_connection_id,
            customer_identity_id=normalized_identity_id,
        )
        if existing is not None:
            return existing

        dispatch_id = str(uuid.uuid4())
        idempotency_key = (
            f"delivery:{normalized_delivery_id}:connection:{normalized_connection_id}:"
            f"identity:{normalized_identity_id}"
        )
        try:
            self._conn.execute(
                """
                INSERT INTO delivery_dispatch_outbox(
                    id, business_id, platform, logical_delivery_id,
                    connection_id, customer_identity_id, payload_kind,
                    payload_ref, idempotency_key, status, attempts,
                    available_at, locked_at, lock_token, provider_message_id,
                    last_error, created_at, updated_at, sent_at, dead_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?,
                         NULL, NULL, NULL, NULL, ?, ?, NULL, NULL)
                """,
                (
                    dispatch_id,
                    current.business_id,
                    platform.value,
                    normalized_delivery_id,
                    normalized_connection_id,
                    normalized_identity_id,
                    str(_value(logical, "content_kind", 5)),
                    str(_value(logical, "content_ref", 6)),
                    idempotency_key,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            concurrent = self._find_materialized(
                business_id=current.business_id,
                logical_delivery_id=normalized_delivery_id,
                connection_id=normalized_connection_id,
                customer_identity_id=normalized_identity_id,
            )
            if concurrent is None:
                raise
            return concurrent
        return self.get_dispatch(
            actor=current,
            dispatch_id=dispatch_id,
        )

    def get_dispatch(
        self,
        *,
        actor: TenantContext,
        dispatch_id: str,
    ) -> Dispatch:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_deliveries()
        normalized_dispatch_id = normalize_uuid(
            dispatch_id,
            field_name="dispatch_id",
        )
        row = self._conn.execute(
            f"SELECT {_DISPATCH_COLUMNS} FROM delivery_dispatch_outbox "
            "WHERE id=? AND business_id=? LIMIT 1",  # nosec B608 - static columns
            (normalized_dispatch_id, current.business_id),
        ).fetchone()
        if row is None:
            raise DispatchNotFound("dispatch was not found in the active business")
        return _dispatch_from_row(row)

    def claim_due(
        self,
        *,
        limit: int = 10,
        lock_ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> list[ClaimedDispatch]:
        claim_now = (now or _utc_now()).replace(microsecond=0)
        now_iso = claim_now.isoformat()
        stale_before = (
            claim_now - timedelta(seconds=max(1, int(lock_ttl_seconds)))
        ).isoformat()
        batch_limit = max(1, min(int(limit), 100))
        lock_token = uuid.uuid4().hex

        if CONFIG.uses_postgres:
            rows = self._conn.execute(
                """
                WITH due AS (
                    SELECT d.id
                    FROM delivery_dispatch_outbox d
                    JOIN connections c
                      ON c.id=d.connection_id AND c.business_id=d.business_id
                     AND c.platform=d.platform AND c.status='active'
                    JOIN customer_identities ci
                      ON ci.id=d.customer_identity_id
                     AND ci.business_id=d.business_id
                     AND ci.platform=d.platform AND ci.status='active'
                    WHERE (
                        (d.status IN ('pending','retry') AND d.available_at<=?)
                        OR (
                            d.status='sending' AND d.locked_at IS NOT NULL
                            AND d.locked_at<=?
                        )
                    )
                    ORDER BY d.available_at, d.id
                    LIMIT ?
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE delivery_dispatch_outbox d
                SET status='sending', locked_at=?, lock_token=?, updated_at=?
                FROM due
                WHERE d.id=due.id
                RETURNING d.id
                """,
                (
                    now_iso,
                    stale_before,
                    batch_limit,
                    now_iso,
                    lock_token,
                    now_iso,
                ),
            ).fetchall()
            if not rows:
                return []
        else:
            rows = self._conn.execute(
                """
                SELECT d.id
                FROM delivery_dispatch_outbox d
                JOIN connections c
                  ON c.id=d.connection_id AND c.business_id=d.business_id
                 AND c.platform=d.platform AND c.status='active'
                JOIN customer_identities ci
                  ON ci.id=d.customer_identity_id
                 AND ci.business_id=d.business_id
                 AND ci.platform=d.platform AND ci.status='active'
                WHERE (
                    (d.status IN ('pending','retry') AND d.available_at<=?)
                    OR (
                        d.status='sending' AND d.locked_at IS NOT NULL
                        AND d.locked_at<=?
                    )
                )
                ORDER BY d.available_at, d.id
                LIMIT ?
                """,
                (now_iso, stale_before, batch_limit),
            ).fetchall()
            ids = [str(_value(row, "id", 0)) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(
                "UPDATE delivery_dispatch_outbox "
                "SET status='sending',locked_at=?,lock_token=?,updated_at=? "
                f"WHERE id IN ({placeholders}) AND "  # nosec B608 - placeholders only
                "((status IN ('pending','retry') AND available_at<=?) OR "
                "(status='sending' AND locked_at IS NOT NULL AND locked_at<=?))",
                [now_iso, lock_token, now_iso, *ids, now_iso, stale_before],
            )

        claimed_rows = self._conn.execute(
            """
            SELECT d.id, d.business_id, d.platform, d.logical_delivery_id,
                   d.connection_id, d.customer_identity_id, d.payload_kind,
                   d.payload_ref, d.idempotency_key, d.status, d.attempts,
                   d.available_at, d.locked_at, d.lock_token,
                   d.provider_message_id, d.last_error, d.created_at,
                   d.updated_at, d.sent_at, d.dead_at,
                   ci.external_subject, c.credential_reference
            FROM delivery_dispatch_outbox d
            JOIN connections c
              ON c.id=d.connection_id AND c.business_id=d.business_id
             AND c.platform=d.platform AND c.status='active'
            JOIN customer_identities ci
              ON ci.id=d.customer_identity_id AND ci.business_id=d.business_id
             AND ci.platform=d.platform AND ci.status='active'
            WHERE d.lock_token=? AND d.status='sending'
            ORDER BY d.available_at, d.id
            """,
            (lock_token,),
        ).fetchall()
        return [
            ClaimedDispatch(
                dispatch=_dispatch_from_row(row),
                external_subject=str(_value(row, "external_subject", 20)),
                credential_reference=str(
                    _value(row, "credential_reference", 21)
                ),
            )
            for row in claimed_rows
        ]

    def mark_sent(
        self,
        item: ClaimedDispatch,
        *,
        provider_message_id: str,
        now: datetime | None = None,
    ) -> Dispatch:
        sent_at = (now or _utc_now()).replace(microsecond=0).isoformat()
        normalized_provider_id = str(provider_message_id or "").strip()
        if not normalized_provider_id:
            raise ValueError("provider_message_id must not be empty")
        cursor = self._conn.execute(
            """
            UPDATE delivery_dispatch_outbox
            SET status='sent', provider_message_id=?, sent_at=?, updated_at=?,
                locked_at=NULL, lock_token=NULL, last_error=NULL
            WHERE id=? AND business_id=? AND status='sending' AND lock_token=?
            """,
            (
                normalized_provider_id[:512],
                sent_at,
                sent_at,
                item.dispatch.id,
                item.dispatch.business_id,
                item.dispatch.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise DispatchLeaseLost("dispatch lease was lost before success")
        self._conn.execute(
            """
            UPDATE lesson_deliveries
            SET status='sent', attempts=attempts+1, sent_at=?, failed_at=NULL,
                last_error=NULL, updated_at=?
            WHERE id=? AND business_id=? AND status IN ('pending','failed')
            """,
            (
                sent_at,
                sent_at,
                item.dispatch.logical_delivery_id,
                item.dispatch.business_id,
            ),
        )
        self._conn.execute(
            """
            UPDATE lesson_progress
            SET status='delivered', delivered_at=?, updated_at=?
            WHERE business_id=? AND enrollment_id=(
                SELECT enrollment_id FROM lesson_deliveries
                WHERE id=? AND business_id=?
            ) AND lesson_id=(
                SELECT lesson_id FROM lesson_deliveries
                WHERE id=? AND business_id=?
            ) AND status='pending'
            """,
            (
                sent_at,
                sent_at,
                item.dispatch.business_id,
                item.dispatch.logical_delivery_id,
                item.dispatch.business_id,
                item.dispatch.logical_delivery_id,
                item.dispatch.business_id,
            ),
        )
        self._conn.execute(
            """
            UPDATE connections
            SET last_success_at=?, last_error_at=NULL, last_error_code=NULL,
                updated_at=?
            WHERE id=? AND business_id=?
            """,
            (
                sent_at,
                sent_at,
                item.dispatch.connection_id,
                item.dispatch.business_id,
            ),
        )
        return self._get_dispatch_system(
            business_id=item.dispatch.business_id,
            dispatch_id=item.dispatch.id,
        )

    def reschedule(
        self,
        item: ClaimedDispatch,
        *,
        error: str,
        max_attempts: int = 8,
        now: datetime | None = None,
    ) -> Dispatch:
        failed_at = (now or _utc_now()).replace(microsecond=0)
        attempts = item.dispatch.attempts + 1
        terminal = attempts >= max(1, int(max_attempts))
        delay_seconds = min(5 * (2 ** max(0, attempts - 1)), 900)
        available_at = (failed_at + timedelta(seconds=delay_seconds)).isoformat()
        status = DispatchStatus.DEAD if terminal else DispatchStatus.RETRY
        error_text = str(error or "dispatch_failed").strip()[:1000]
        cursor = self._conn.execute(
            """
            UPDATE delivery_dispatch_outbox
            SET status=?, attempts=?, available_at=?, updated_at=?,
                locked_at=NULL, lock_token=NULL, last_error=?, dead_at=?
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
        self._conn.execute(
            """
            UPDATE connections
            SET status='attention', last_error_at=?, last_error_code=?,
                updated_at=?
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
        if terminal:
            self._conn.execute(
                """
                UPDATE lesson_deliveries
                SET status='failed', attempts=?, failed_at=?, last_error=?,
                    updated_at=?
                WHERE id=? AND business_id=? AND status IN ('pending','failed')
                """,
                (
                    attempts,
                    failed_at.isoformat(),
                    error_text,
                    failed_at.isoformat(),
                    item.dispatch.logical_delivery_id,
                    item.dispatch.business_id,
                ),
            )
        return self._get_dispatch_system(
            business_id=item.dispatch.business_id,
            dispatch_id=item.dispatch.id,
        )

    def release_lease(
        self,
        item: ClaimedDispatch,
        *,
        reason: str = "worker_shutdown",
        now: datetime | None = None,
    ) -> Dispatch:
        released_at = (now or _utc_now()).replace(microsecond=0).isoformat()
        cursor = self._conn.execute(
            """
            UPDATE delivery_dispatch_outbox
            SET status='retry', available_at=?, updated_at=?, locked_at=NULL,
                lock_token=NULL, last_error=?
            WHERE id=? AND business_id=? AND status='sending' AND lock_token=?
            """,
            (
                released_at,
                released_at,
                str(reason or "worker_shutdown")[:1000],
                item.dispatch.id,
                item.dispatch.business_id,
                item.dispatch.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise DispatchLeaseLost("dispatch lease was lost before release")
        return self._get_dispatch_system(
            business_id=item.dispatch.business_id,
            dispatch_id=item.dispatch.id,
        )

    def _find_materialized(
        self,
        *,
        business_id: str,
        logical_delivery_id: str,
        connection_id: str,
        customer_identity_id: str,
    ) -> Dispatch | None:
        row = self._conn.execute(
            f"SELECT {_DISPATCH_COLUMNS} FROM delivery_dispatch_outbox "
            "WHERE business_id=? AND logical_delivery_id=? "
            "AND connection_id=? AND customer_identity_id=? LIMIT 1",  # nosec B608
            (
                business_id,
                logical_delivery_id,
                connection_id,
                customer_identity_id,
            ),
        ).fetchone()
        return None if row is None else _dispatch_from_row(row)

    def _get_dispatch_system(
        self,
        *,
        business_id: str,
        dispatch_id: str,
    ) -> Dispatch:
        row = self._conn.execute(
            f"SELECT {_DISPATCH_COLUMNS} FROM delivery_dispatch_outbox "
            "WHERE id=? AND business_id=? LIMIT 1",  # nosec B608
            (dispatch_id, business_id),
        ).fetchone()
        if row is None:
            raise DispatchNotFound("dispatch was not found")
        return _dispatch_from_row(row)
