from __future__ import annotations

from typing import Any

from clientplatform.domain.connections import ConnectionNotFound
from clientplatform.domain.managed_bot_owner import (
    ManagedBotOwnerSnapshot,
    ManagedBotWebhookMaterial,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _optional(row: Any, key: str, position: int) -> str | None:
    value = _value(row, key, position)
    return None if value is None else str(value)


class ManagedBotOwnerRepository:
    """Tenant-scoped owner read model without exposing secret references."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _resolve_actor(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        return current

    def webhook_material(
        self,
        *,
        actor: TenantContext,
        managed_bot_id: str,
    ) -> ManagedBotWebhookMaterial:
        current = self._resolve_actor(actor)
        normalized_id = normalize_uuid(
            managed_bot_id,
            field_name="managed_bot_id",
        )
        row = self._conn.execute(
            """
            SELECT mb.id, mb.business_id, mb.connection_id, mb.external_bot_id,
                   mb.username, c.credential_reference,
                   mb.webhook_secret_reference
            FROM managed_bots mb
            JOIN connections c
              ON c.id=mb.connection_id AND c.business_id=mb.business_id
             AND c.platform=mb.platform
            WHERE mb.id=? AND mb.business_id=? AND mb.platform='telegram'
              AND c.platform='telegram'
            LIMIT 1
            """,
            (normalized_id, current.business_id),
        ).fetchone()
        if row is None:
            raise ConnectionNotFound("managed bot was not found in the business")
        return ManagedBotWebhookMaterial(
            managed_bot_id=str(_value(row, "id", 0)),
            business_id=str(_value(row, "business_id", 1)),
            connection_id=str(_value(row, "connection_id", 2)),
            external_bot_id=str(_value(row, "external_bot_id", 3)),
            username=_optional(row, "username", 4),
            credential_reference=str(_value(row, "credential_reference", 5)),
            webhook_secret_reference=str(
                _value(row, "webhook_secret_reference", 6)
            ),
        )

    def snapshot(
        self,
        *,
        actor: TenantContext,
        managed_bot_id: str,
    ) -> ManagedBotOwnerSnapshot:
        current = self._resolve_actor(actor)
        normalized_id = normalize_uuid(
            managed_bot_id,
            field_name="managed_bot_id",
        )
        row = self._conn.execute(
            """
            SELECT mb.id, mb.business_id, mb.connection_id, mb.external_bot_id,
                   mb.username, mb.display_name, mb.status AS bot_status,
                   c.status AS connection_status,
                   COALESCE(SUM(CASE WHEN e.status='pending' THEN 1 ELSE 0 END), 0)
                       AS pending_events,
                   COALESCE(SUM(CASE WHEN e.status='processing' THEN 1 ELSE 0 END), 0)
                       AS processing_events,
                   COALESCE(SUM(CASE WHEN e.status='retry' THEN 1 ELSE 0 END), 0)
                       AS retry_events,
                   COALESCE(SUM(CASE WHEN e.status='processed' THEN 1 ELSE 0 END), 0)
                       AS processed_events,
                   COALESCE(SUM(CASE WHEN e.status='dead' THEN 1 ELSE 0 END), 0)
                       AS dead_events,
                   mb.updated_at AS bot_updated_at,
                   c.updated_at AS connection_updated_at,
                   MAX(e.updated_at) AS last_event_at,
                   MAX(e.processed_at) AS last_processed_at,
                   MAX(e.dead_at) AS last_dead_at,
                   c.last_success_at AS last_connection_success_at,
                   c.last_error_at AS last_connection_error_at
            FROM managed_bots mb
            JOIN connections c
              ON c.id=mb.connection_id AND c.business_id=mb.business_id
             AND c.platform=mb.platform
            LEFT JOIN bot_gateway_ingress_events e
              ON e.managed_bot_id=mb.id AND e.business_id=mb.business_id
            WHERE mb.id=? AND mb.business_id=? AND mb.platform='telegram'
              AND c.platform='telegram'
            GROUP BY mb.id, mb.business_id, mb.connection_id, mb.external_bot_id,
                     mb.username, mb.display_name, mb.status, c.status,
                     mb.updated_at, c.updated_at, c.last_success_at, c.last_error_at
            LIMIT 1
            """,
            (normalized_id, current.business_id),
        ).fetchone()
        if row is None:
            raise ConnectionNotFound("managed bot was not found in the business")
        return ManagedBotOwnerSnapshot(
            managed_bot_id=str(_value(row, "id", 0)),
            business_id=str(_value(row, "business_id", 1)),
            connection_id=str(_value(row, "connection_id", 2)),
            external_bot_id=str(_value(row, "external_bot_id", 3)),
            username=_optional(row, "username", 4),
            display_name=_optional(row, "display_name", 5),
            bot_status=str(_value(row, "bot_status", 6)),
            connection_status=str(_value(row, "connection_status", 7)),
            pending_events=int(_value(row, "pending_events", 8)),
            processing_events=int(_value(row, "processing_events", 9)),
            retry_events=int(_value(row, "retry_events", 10)),
            processed_events=int(_value(row, "processed_events", 11)),
            dead_events=int(_value(row, "dead_events", 12)),
            bot_updated_at=str(_value(row, "bot_updated_at", 13)),
            connection_updated_at=str(_value(row, "connection_updated_at", 14)),
            last_event_at=_optional(row, "last_event_at", 15),
            last_processed_at=_optional(row, "last_processed_at", 16),
            last_dead_at=_optional(row, "last_dead_at", 17),
            last_connection_success_at=_optional(
                row,
                "last_connection_success_at",
                18,
            ),
            last_connection_error_at=_optional(
                row,
                "last_connection_error_at",
                19,
            ),
        )
