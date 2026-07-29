from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from clientplatform.domain.bookings import CustomerBusinessLink
from clientplatform.domain.bot_gateway import (
    AdmittedIngressEvent,
    BotGatewayAdmissionRejected,
    BotGatewayLeaseLost,
    BotGatewayReplayConflict,
    ClaimedIngressEvent,
    IngressEvent,
    IngressEventStatus,
    ManagedBotRoute,
    ManagedBotRouteNotFound,
    normalize_provider_update_id,
)
from clientplatform.domain.customers import (
    normalize_optional_handle,
    normalize_optional_person_name,
)
from clientplatform.domain.tenancy import normalize_user_id
from services.db.core import PostgresCompatConnection


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _optional(row: Any, key: str, position: int) -> str | None:
    value = _value(row, key, position)
    return None if value is None else str(value)


def _gateway_lock_key(*parts: str) -> int:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.blake2b(
        payload,
        digest_size=8,
        person=b"cp-botgw-v1",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _serialize_write(conn: Any, *, namespace: str, subject: str) -> None:
    if isinstance(conn, PostgresCompatConnection):
        key = _gateway_lock_key(namespace, subject)
        conn.execute("SELECT pg_advisory_xact_lock(?)", (key,)).fetchone()
        return
    if isinstance(conn, sqlite3.Connection) and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _route_from_row(row: Any, *, offset: int = 0) -> ManagedBotRoute:
    return ManagedBotRoute(
        managed_bot_id=str(_value(row, "managed_bot_id", offset)),
        business_id=str(_value(row, "business_id", offset + 1)),
        connection_id=str(_value(row, "connection_id", offset + 2)),
        external_bot_id=str(_value(row, "external_bot_id", offset + 3)),
        credential_reference=str(_value(row, "credential_reference", offset + 4)),
        webhook_secret_reference=str(
            _value(row, "webhook_secret_reference", offset + 5)
        ),
        username=_optional(row, "username", offset + 6),
        display_name=_optional(row, "display_name", offset + 7),
    )


_EVENT_COLUMNS = """
id, business_id, managed_bot_id, provider_update_id, payload_sha256,
payload_json, status, attempts, available_at, locked_at, lock_token,
last_error_code, created_at, updated_at, processed_at, dead_at
""".strip()


def _event_from_row(row: Any, *, offset: int = 0) -> IngressEvent:
    return IngressEvent(
        id=str(_value(row, "id", offset)),
        business_id=str(_value(row, "business_id", offset + 1)),
        managed_bot_id=str(_value(row, "managed_bot_id", offset + 2)),
        provider_update_id=str(_value(row, "provider_update_id", offset + 3)),
        payload_sha256=str(_value(row, "payload_sha256", offset + 4)),
        payload_json=_optional(row, "payload_json", offset + 5),
        status=IngressEventStatus(str(_value(row, "status", offset + 6))),
        attempts=int(_value(row, "attempts", offset + 7)),
        available_at=str(_value(row, "available_at", offset + 8)),
        locked_at=_optional(row, "locked_at", offset + 9),
        lock_token=_optional(row, "lock_token", offset + 10),
        last_error_code=_optional(row, "last_error_code", offset + 11),
        created_at=str(_value(row, "created_at", offset + 12)),
        updated_at=str(_value(row, "updated_at", offset + 13)),
        processed_at=_optional(row, "processed_at", offset + 14),
        dead_at=_optional(row, "dead_at", offset + 15),
    )


_ROUTE_COLUMNS = """
mb.id AS managed_bot_id, mb.business_id, mb.connection_id,
mb.external_bot_id, c.credential_reference, mb.webhook_secret_reference,
mb.username, mb.display_name
""".strip()


class BotGatewayRepository:
    """Durable, replay-safe and tenant-scoped managed bot ingress."""

    def __init__(self, conn: Any):
        self._conn = conn

    def resolve_telegram_route(self, *, external_bot_id: int | str) -> ManagedBotRoute:
        normalized_bot_id = str(external_bot_id).strip()
        if not normalized_bot_id.isdigit() or int(normalized_bot_id) <= 0:
            raise ManagedBotRouteNotFound("managed Telegram bot route was not found")
        rows = self._conn.execute(
            f"""
            SELECT {_ROUTE_COLUMNS}
            FROM managed_bots mb
            JOIN connections c
              ON c.id=mb.connection_id AND c.business_id=mb.business_id
             AND c.platform=mb.platform AND c.status='active'
            JOIN businesses b
              ON b.id=mb.business_id AND b.status='active'
            WHERE mb.platform='telegram' AND mb.external_bot_id=?
              AND mb.status='active'
            LIMIT 2
            """,  # nosec B608 - static reviewed column list
            (normalized_bot_id,),
        ).fetchall()
        if len(rows) != 1:
            raise ManagedBotRouteNotFound("managed Telegram bot route was not found")
        return _route_from_row(rows[0])

    def admit_telegram_update(
        self,
        *,
        route: ManagedBotRoute,
        provider_update_id: int | str,
        payload: Mapping[str, Any],
        per_minute_limit: int = 120,
        queue_limit: int = 1000,
        max_payload_bytes: int = 262_144,
        now: datetime | None = None,
    ) -> AdmittedIngressEvent:
        update_id = normalize_provider_update_id(provider_update_id)
        payload_update_id = normalize_provider_update_id(payload.get("update_id", ""))
        if payload_update_id != update_id:
            raise BotGatewayAdmissionRejected("Telegram update id does not match payload")
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > max(1024, int(max_payload_bytes)):
            raise BotGatewayAdmissionRejected("Telegram update payload is too large")
        payload_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        admitted_at = (now or _utc_now()).replace(microsecond=0)
        timestamp = admitted_at.isoformat()

        _serialize_write(
            self._conn,
            namespace="telegram-ingress-admission",
            subject=route.managed_bot_id,
        )
        existing = self._find_event(
            managed_bot_id=route.managed_bot_id,
            provider_update_id=update_id,
        )
        if existing is not None:
            if existing.payload_sha256 != payload_sha256:
                raise BotGatewayReplayConflict(
                    "Telegram update id was reused with different content"
                )
            return AdmittedIngressEvent(event=existing, duplicate=True)

        minute_limit = max(1, min(int(per_minute_limit), 10_000))
        window_start = (admitted_at - timedelta(seconds=60)).isoformat()
        recent = self._conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM bot_gateway_ingress_events
            WHERE managed_bot_id=? AND created_at>=?
            """,
            (route.managed_bot_id, window_start),
        ).fetchone()
        if recent is not None and int(_value(recent, "c", 0)) >= minute_limit:
            raise BotGatewayAdmissionRejected("managed bot ingress rate limit exceeded")

        max_queue = max(1, min(int(queue_limit), 100_000))
        queued = self._conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM bot_gateway_ingress_events
            WHERE managed_bot_id=? AND status IN ('pending','processing','retry')
            """,
            (route.managed_bot_id,),
        ).fetchone()
        if queued is not None and int(_value(queued, "c", 0)) >= max_queue:
            raise BotGatewayAdmissionRejected("managed bot ingress queue is full")

        event_id = str(uuid.uuid4())
        try:
            self._conn.execute(
                """
                INSERT INTO bot_gateway_ingress_events(
                    id, business_id, managed_bot_id, provider_update_id,
                    payload_sha256, payload_json, status, attempts,
                    available_at, locked_at, lock_token, last_error_code,
                    created_at, updated_at, processed_at, dead_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, NULL,
                         ?, ?, NULL, NULL)
                """,
                (
                    event_id,
                    route.business_id,
                    route.managed_bot_id,
                    update_id,
                    payload_sha256,
                    encoded,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            concurrent = self._find_event(
                managed_bot_id=route.managed_bot_id,
                provider_update_id=update_id,
            )
            if concurrent is None:
                raise
            if concurrent.payload_sha256 != payload_sha256:
                raise BotGatewayReplayConflict(
                    "Telegram update id was reused with different content"
                ) from None
            return AdmittedIngressEvent(event=concurrent, duplicate=True)
        event = self._find_event(
            managed_bot_id=route.managed_bot_id,
            provider_update_id=update_id,
        )
        if event is None:
            raise BotGatewayAdmissionRejected("managed bot ingress event was not stored")
        return AdmittedIngressEvent(event=event, duplicate=False)

    def claim_due(
        self,
        *,
        limit: int = 10,
        lock_ttl_seconds: int = 300,
        now: datetime | None = None,
    ) -> list[ClaimedIngressEvent]:
        claim_now = (now or _utc_now()).replace(microsecond=0)
        now_iso = claim_now.isoformat()
        stale_before = (
            claim_now - timedelta(seconds=max(1, int(lock_ttl_seconds)))
        ).isoformat()
        batch_limit = max(1, min(int(limit), 100))
        lock_token = uuid.uuid4().hex

        if isinstance(self._conn, PostgresCompatConnection):
            rows = self._conn.execute(
                """
                WITH due AS (
                    SELECT e.id
                    FROM bot_gateway_ingress_events e
                    JOIN managed_bots mb
                      ON mb.id=e.managed_bot_id AND mb.business_id=e.business_id
                     AND mb.status='active' AND mb.platform='telegram'
                    JOIN connections c
                      ON c.id=mb.connection_id AND c.business_id=mb.business_id
                     AND c.status='active' AND c.platform='telegram'
                    WHERE (
                        (e.status IN ('pending','retry') AND e.available_at<=?)
                        OR (
                            e.status='processing' AND e.locked_at IS NOT NULL
                            AND e.locked_at<=?
                        )
                    )
                    ORDER BY e.available_at, e.id
                    LIMIT ?
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE bot_gateway_ingress_events e
                SET status='processing', locked_at=?, lock_token=?, updated_at=?
                FROM due
                WHERE e.id=due.id
                RETURNING e.id
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
            _serialize_write(
                self._conn,
                namespace="telegram-ingress-claim",
                subject="global",
            )
            rows = self._conn.execute(
                """
                SELECT e.id
                FROM bot_gateway_ingress_events e
                JOIN managed_bots mb
                  ON mb.id=e.managed_bot_id AND mb.business_id=e.business_id
                 AND mb.status='active' AND mb.platform='telegram'
                JOIN connections c
                  ON c.id=mb.connection_id AND c.business_id=mb.business_id
                 AND c.status='active' AND c.platform='telegram'
                WHERE (
                    (e.status IN ('pending','retry') AND e.available_at<=?)
                    OR (
                        e.status='processing' AND e.locked_at IS NOT NULL
                        AND e.locked_at<=?
                    )
                )
                ORDER BY e.available_at, e.id
                LIMIT ?
                """,
                (now_iso, stale_before, batch_limit),
            ).fetchall()
            ids = [str(_value(row, "id", 0)) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(
                "UPDATE bot_gateway_ingress_events "
                "SET status='processing',locked_at=?,lock_token=?,updated_at=? "
                f"WHERE id IN ({placeholders}) AND "  # nosec B608 - placeholders only
                "((status IN ('pending','retry') AND available_at<=?) OR "
                "(status='processing' AND locked_at IS NOT NULL AND locked_at<=?))",
                [now_iso, lock_token, now_iso, *ids, now_iso, stale_before],
            )

        claimed_rows = self._conn.execute(
            f"""
            SELECT {_EVENT_COLUMNS}, {_ROUTE_COLUMNS}
            FROM bot_gateway_ingress_events e
            JOIN managed_bots mb
              ON mb.id=e.managed_bot_id AND mb.business_id=e.business_id
             AND mb.status='active' AND mb.platform='telegram'
            JOIN connections c
              ON c.id=mb.connection_id AND c.business_id=mb.business_id
             AND c.status='active' AND c.platform='telegram'
            WHERE e.lock_token=? AND e.status='processing'
            ORDER BY e.available_at, e.id
            """,  # nosec B608 - static reviewed column lists
            (lock_token,),
        ).fetchall()
        return [
            ClaimedIngressEvent(
                event=_event_from_row(row),
                route=_route_from_row(row, offset=16),
            )
            for row in claimed_rows
        ]

    def mark_processed(
        self,
        item: ClaimedIngressEvent,
        *,
        now: datetime | None = None,
    ) -> IngressEvent:
        finished_at = (now or _utc_now()).replace(microsecond=0).isoformat()
        cursor = self._conn.execute(
            """
            UPDATE bot_gateway_ingress_events
            SET status='processed', payload_json=NULL, updated_at=?, processed_at=?,
                locked_at=NULL, lock_token=NULL, last_error_code=NULL
            WHERE id=? AND business_id=? AND status='processing' AND lock_token=?
            """,
            (
                finished_at,
                finished_at,
                item.event.id,
                item.event.business_id,
                item.event.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise BotGatewayLeaseLost("managed bot ingress lease was lost")
        return self._get_event(event_id=item.event.id)

    def reschedule(
        self,
        item: ClaimedIngressEvent,
        *,
        error_code: str,
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> IngressEvent:
        failed_at = (now or _utc_now()).replace(microsecond=0)
        attempts = item.event.attempts + 1
        terminal = attempts >= max(1, min(int(max_attempts), 20))
        delay_seconds = min(2 ** max(0, attempts), 300)
        available_at = (failed_at + timedelta(seconds=delay_seconds)).isoformat()
        status = IngressEventStatus.DEAD if terminal else IngressEventStatus.RETRY
        normalized_error = str(error_code or "gateway_processing_failed").strip()[:120]
        cursor = self._conn.execute(
            """
            UPDATE bot_gateway_ingress_events
            SET status=?, attempts=?, available_at=?, updated_at=?,
                locked_at=NULL, lock_token=NULL, last_error_code=?,
                payload_json=CASE WHEN ?='dead' THEN NULL ELSE payload_json END,
                dead_at=CASE WHEN ?='dead' THEN ? ELSE NULL END
            WHERE id=? AND business_id=? AND status='processing' AND lock_token=?
            """,
            (
                status.value,
                attempts,
                available_at,
                failed_at.isoformat(),
                normalized_error,
                status.value,
                status.value,
                failed_at.isoformat(),
                item.event.id,
                item.event.business_id,
                item.event.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise BotGatewayLeaseLost("managed bot ingress lease was lost")
        return self._get_event(event_id=item.event.id)

    def ensure_telegram_customer_link(
        self,
        *,
        route: ManagedBotRoute,
        telegram_user_id: int,
        username: str | None = None,
        display_name: str | None = None,
        now: datetime | None = None,
    ) -> CustomerBusinessLink:
        principal = normalize_user_id(telegram_user_id)
        _serialize_write(
            self._conn,
            namespace="managed-bot-customer-link",
            subject=f"{route.business_id}:{principal}",
        )
        existing = self._customer_link_row(
            business_id=route.business_id,
            telegram_user_id=principal,
        )
        if existing is not None:
            return CustomerBusinessLink(
                business_id=route.business_id,
                business_name=str(_value(existing, "business_name", 1)),
                customer_id=str(_value(existing, "customer_id", 0)),
            )

        owner = self._conn.execute(
            """
            SELECT bm.id AS member_id, b.name AS business_name
            FROM business_members bm
            JOIN businesses b ON b.id=bm.business_id AND b.status='active'
            WHERE bm.business_id=? AND bm.role='owner' AND bm.status='active'
            ORDER BY bm.created_at, bm.id
            LIMIT 1
            """,
            (route.business_id,),
        ).fetchone()
        if owner is None:
            raise BotGatewayAdmissionRejected("managed bot business has no active owner")

        timestamp = (now or _utc_now()).replace(microsecond=0).isoformat()
        customer_id = str(uuid.uuid4())
        identity_id = str(uuid.uuid4())
        normalized_name = normalize_optional_person_name(
            display_name,
            field_name="display_name",
        )
        normalized_username = normalize_optional_handle(username)
        self._conn.execute(
            """
            INSERT INTO customers(
                id, business_id, display_name, status, created_by_member_id,
                created_at, updated_at, archived_at
            ) VALUES(?, ?, ?, 'active', ?, ?, ?, NULL)
            """,
            (
                customer_id,
                route.business_id,
                normalized_name,
                str(_value(owner, "member_id", 0)),
                timestamp,
                timestamp,
            ),
        )
        try:
            self._conn.execute(
                """
                INSERT INTO customer_identities(
                    id, business_id, customer_id, platform, external_subject,
                    username, display_name, status, created_at, updated_at, revoked_at
                ) VALUES(?, ?, ?, 'telegram', ?, ?, ?, 'active', ?, ?, NULL)
                """,
                (
                    identity_id,
                    route.business_id,
                    customer_id,
                    str(principal),
                    normalized_username,
                    normalized_name,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            self._conn.execute(
                "DELETE FROM customers WHERE id=? AND business_id=?",
                (customer_id, route.business_id),
            )
            concurrent = self._customer_link_row(
                business_id=route.business_id,
                telegram_user_id=principal,
            )
            if concurrent is None:
                raise
            customer_id = str(_value(concurrent, "customer_id", 0))
        return CustomerBusinessLink(
            business_id=route.business_id,
            business_name=str(_value(owner, "business_name", 1)),
            customer_id=customer_id,
        )

    def health_snapshot(self) -> dict[str, int]:
        rows = self._conn.execute(
            """
            SELECT status, COUNT(*) AS c
            FROM bot_gateway_ingress_events
            GROUP BY status
            """
        ).fetchall()
        counts = {status.value: 0 for status in IngressEventStatus}
        for row in rows:
            counts[str(_value(row, "status", 0))] = int(_value(row, "c", 1))
        bots = self._conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM managed_bots mb
            JOIN connections c
              ON c.id=mb.connection_id AND c.business_id=mb.business_id
             AND c.platform=mb.platform AND c.status='active'
            JOIN businesses b ON b.id=mb.business_id AND b.status='active'
            WHERE mb.platform='telegram' AND mb.status='active'
            """
        ).fetchone()
        counts["active_bots"] = 0 if bots is None else int(_value(bots, "c", 0))
        return counts

    def _find_event(
        self,
        *,
        managed_bot_id: str,
        provider_update_id: str,
    ) -> IngressEvent | None:
        row = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM bot_gateway_ingress_events "
            "WHERE managed_bot_id=? AND provider_update_id=? LIMIT 1",  # nosec B608
            (managed_bot_id, provider_update_id),
        ).fetchone()
        return None if row is None else _event_from_row(row)

    def _get_event(self, *, event_id: str) -> IngressEvent:
        row = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM bot_gateway_ingress_events "
            "WHERE id=? LIMIT 1",  # nosec B608
            (event_id,),
        ).fetchone()
        if row is None:
            raise BotGatewayAdmissionRejected("managed bot ingress event disappeared")
        return _event_from_row(row)

    def _customer_link_row(
        self,
        *,
        business_id: str,
        telegram_user_id: int,
    ) -> Any | None:
        return self._conn.execute(
            """
            SELECT ci.customer_id, b.name AS business_name
            FROM customer_identities ci
            JOIN customers c
              ON c.id=ci.customer_id AND c.business_id=ci.business_id
             AND c.status='active'
            JOIN businesses b ON b.id=ci.business_id AND b.status='active'
            WHERE ci.business_id=? AND ci.platform='telegram'
              AND ci.external_subject=? AND ci.status='active'
            LIMIT 1
            """,
            (business_id, str(telegram_user_id)),
        ).fetchone()
