from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.connections import (
    ConnectionNotFound,
    ManagedBot,
    ManagedBotStatus,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.connection_repository import (
    ConnectionRepository as _BaseConnectionRepository,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ConnectionRepository(_BaseConnectionRepository):
    """Canonical connection repository with managed-bot lifecycle transitions."""

    def disable_managed_bot(
        self,
        *,
        actor: TenantContext,
        managed_bot_id: str,
        now: str | None = None,
    ) -> ManagedBot:
        current = self._resolve_actor(actor)
        normalized_id = normalize_uuid(
            managed_bot_id,
            field_name="managed_bot_id",
        )
        bot = self._get_managed_bot(
            business_id=current.business_id,
            managed_bot_id=normalized_id,
        )
        if bot.status == ManagedBotStatus.REVOKED:
            raise ConnectionNotFound("revoked managed bot cannot be disabled")
        if bot.status == ManagedBotStatus.DISABLED:
            return bot
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE managed_bots
            SET status='disabled', updated_at=?
            WHERE id=? AND business_id=? AND status='active'
            """,
            (timestamp, normalized_id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ConnectionNotFound("active managed bot was not found")
        self._conn.execute(
            """
            UPDATE connections
            SET status='disabled', updated_at=?
            WHERE id=? AND business_id=? AND status!='revoked'
            """,
            (timestamp, bot.connection_id, current.business_id),
        )
        return self._get_managed_bot(
            business_id=current.business_id,
            managed_bot_id=normalized_id,
        )

    def activate_managed_bot(
        self,
        *,
        actor: TenantContext,
        managed_bot_id: str,
        now: str | None = None,
    ) -> ManagedBot:
        current = self._resolve_actor(actor)
        normalized_id = normalize_uuid(
            managed_bot_id,
            field_name="managed_bot_id",
        )
        bot = self._get_managed_bot(
            business_id=current.business_id,
            managed_bot_id=normalized_id,
        )
        if bot.status == ManagedBotStatus.REVOKED:
            raise ConnectionNotFound("revoked managed bot cannot be activated")
        if bot.status == ManagedBotStatus.ACTIVE:
            return bot
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE managed_bots
            SET status='active', updated_at=?, revoked_at=NULL
            WHERE id=? AND business_id=? AND status='disabled'
            """,
            (timestamp, normalized_id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ConnectionNotFound("disabled managed bot was not found")
        connection_cursor = self._conn.execute(
            """
            UPDATE connections
            SET status='active', last_success_at=?, last_error_at=NULL,
                last_error_code=NULL, updated_at=?
            WHERE id=? AND business_id=? AND status IN ('pending','attention','disabled')
            """,
            (
                timestamp,
                timestamp,
                bot.connection_id,
                current.business_id,
            ),
        )
        if int(getattr(connection_cursor, "rowcount", 0) or 0) != 1:
            raise ConnectionNotFound("managed bot connection cannot be activated")
        return self._get_managed_bot(
            business_id=current.business_id,
            managed_bot_id=normalized_id,
        )

    def revoke_managed_bot(
        self,
        *,
        actor: TenantContext,
        managed_bot_id: str,
        now: str | None = None,
    ) -> ManagedBot:
        current = self._resolve_actor(actor)
        normalized_id = normalize_uuid(
            managed_bot_id,
            field_name="managed_bot_id",
        )
        bot = self._get_managed_bot(
            business_id=current.business_id,
            managed_bot_id=normalized_id,
        )
        if bot.status == ManagedBotStatus.REVOKED:
            return bot
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE managed_bots
            SET status='revoked', updated_at=?, revoked_at=?
            WHERE id=? AND business_id=? AND status IN ('active','disabled')
            """,
            (
                timestamp,
                timestamp,
                normalized_id,
                current.business_id,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ConnectionNotFound("managed bot cannot be revoked")
        self._conn.execute(
            """
            UPDATE connections
            SET status='revoked', updated_at=?
            WHERE id=? AND business_id=?
            """,
            (timestamp, bot.connection_id, current.business_id),
        )
        return self._get_managed_bot(
            business_id=current.business_id,
            managed_bot_id=normalized_id,
        )
