from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from clientplatform.domain.bot_gateway import (
    ClaimedIngressEvent,
    IngressEvent,
    IngressEventStatus,
    ManagedBotRoute,
)
from clientplatform.infrastructure.bot_gateway_repository import (
    BotGatewayRepository as _BaseBotGatewayRepository,
    _serialize_write,
    _utc_now,
    _value,
)
from services.db.core import PostgresCompatConnection


_EVENT_SELECT = """
e.id AS event_id,
e.business_id AS event_business_id,
e.managed_bot_id AS event_managed_bot_id,
e.provider_update_id AS event_provider_update_id,
e.payload_sha256 AS event_payload_sha256,
e.payload_json AS event_payload_json,
e.status AS event_status,
e.attempts AS event_attempts,
e.available_at AS event_available_at,
e.locked_at AS event_locked_at,
e.lock_token AS event_lock_token,
e.last_error_code AS event_last_error_code,
e.created_at AS event_created_at,
e.updated_at AS event_updated_at,
e.processed_at AS event_processed_at,
e.dead_at AS event_dead_at
""".strip()

_ROUTE_SELECT = """
mb.id AS route_managed_bot_id,
mb.business_id AS route_business_id,
mb.connection_id AS route_connection_id,
mb.external_bot_id AS route_external_bot_id,
c.credential_reference AS route_credential_reference,
mb.webhook_secret_reference AS route_webhook_secret_reference,
mb.username AS route_username,
mb.display_name AS route_display_name
""".strip()


def _optional(row: Any, key: str, position: int) -> str | None:
    value = _value(row, key, position)
    return None if value is None else str(value)


def _claimed_event(row: Any) -> IngressEvent:
    return IngressEvent(
        id=str(_value(row, "event_id", 0)),
        business_id=str(_value(row, "event_business_id", 1)),
        managed_bot_id=str(_value(row, "event_managed_bot_id", 2)),
        provider_update_id=str(_value(row, "event_provider_update_id", 3)),
        payload_sha256=str(_value(row, "event_payload_sha256", 4)),
        payload_json=_optional(row, "event_payload_json", 5),
        status=IngressEventStatus(str(_value(row, "event_status", 6))),
        attempts=int(_value(row, "event_attempts", 7)),
        available_at=str(_value(row, "event_available_at", 8)),
        locked_at=_optional(row, "event_locked_at", 9),
        lock_token=_optional(row, "event_lock_token", 10),
        last_error_code=_optional(row, "event_last_error_code", 11),
        created_at=str(_value(row, "event_created_at", 12)),
        updated_at=str(_value(row, "event_updated_at", 13)),
        processed_at=_optional(row, "event_processed_at", 14),
        dead_at=_optional(row, "event_dead_at", 15),
    )


def _claimed_route(row: Any) -> ManagedBotRoute:
    return ManagedBotRoute(
        managed_bot_id=str(_value(row, "route_managed_bot_id", 16)),
        business_id=str(_value(row, "route_business_id", 17)),
        connection_id=str(_value(row, "route_connection_id", 18)),
        external_bot_id=str(_value(row, "route_external_bot_id", 19)),
        credential_reference=str(_value(row, "route_credential_reference", 20)),
        webhook_secret_reference=str(
            _value(row, "route_webhook_secret_reference", 21)
        ),
        username=_optional(row, "route_username", 22),
        display_name=_optional(row, "route_display_name", 23),
    )


class BotGatewayRepository(_BaseBotGatewayRepository):
    """Canonical managed-bot repository with backend-safe claim semantics."""

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
                SELECT e.id AS event_id
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
            ids = [str(_value(row, "event_id", 0)) for row in rows]
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
            SELECT {_EVENT_SELECT}, {_ROUTE_SELECT}
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
                event=_claimed_event(row),
                route=_claimed_route(row),
            )
            for row in claimed_rows
        ]
