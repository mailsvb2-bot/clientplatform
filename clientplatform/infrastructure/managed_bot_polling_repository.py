from __future__ import annotations

from typing import Any

from clientplatform.domain.bot_gateway import ManagedBotRoute


_ROUTE_COLUMNS = """
mb.id AS managed_bot_id, mb.business_id, mb.connection_id,
mb.external_bot_id, c.credential_reference, mb.webhook_secret_reference,
mb.username, mb.display_name
""".strip()


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _optional(row: Any, key: str, position: int) -> str | None:
    value = _value(row, key, position)
    return None if value is None else str(value)


def _route_from_row(row: Any) -> ManagedBotRoute:
    return ManagedBotRoute(
        managed_bot_id=str(_value(row, "managed_bot_id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        connection_id=str(_value(row, "connection_id", 2)),
        external_bot_id=str(_value(row, "external_bot_id", 3)),
        credential_reference=str(_value(row, "credential_reference", 4)),
        webhook_secret_reference=str(_value(row, "webhook_secret_reference", 5)),
        username=_optional(row, "username", 6),
        display_name=_optional(row, "display_name", 7),
    )


class ManagedBotPollingRepository:
    """Read the exact active Telegram fleet eligible for long polling."""

    def __init__(self, conn: Any):
        self._conn = conn

    def list_active_routes(self, *, limit: int = 10_000) -> list[ManagedBotRoute]:
        bounded_limit = max(1, min(int(limit), 100_000))
        rows = self._conn.execute(
            f"""
            SELECT {_ROUTE_COLUMNS}
            FROM managed_bots mb
            JOIN connections c
              ON c.id=mb.connection_id AND c.business_id=mb.business_id
             AND c.platform=mb.platform AND c.status='active'
            JOIN businesses b
              ON b.id=mb.business_id AND b.status='active'
            WHERE mb.platform='telegram' AND mb.status='active'
            ORDER BY mb.id
            LIMIT ?
            """,  # nosec B608 - static reviewed projection
            (bounded_limit,),
        ).fetchall()
        return [_route_from_row(row) for row in rows]


__all__ = ["ManagedBotPollingRepository"]
