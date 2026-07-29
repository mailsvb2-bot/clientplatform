from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from clientplatform.domain.connections import (
    ConnectionInvariantViolation,
    ConnectionNotFound,
    ConnectionType,
    ManagedBot,
    ManagedBotStatus,
    normalize_external_account_id,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.connection_repository import (
    ConnectionRepository as _BaseConnectionRepository,
    _managed_bot_from_row,
)
from services.db.core import PostgresCompatConnection


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _registration_lock_key(subject: str) -> int:
    digest = hashlib.blake2b(
        str(subject).encode("utf-8"),
        digest_size=8,
        person=b"cp-botlife-v1",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _serialize_registration(
    conn: object,
    *,
    business_id: str,
    platform: str,
    external_bot_id: str,
) -> None:
    subjects = sorted(
        {
            f"business:{business_id}:{platform}",
            f"external:{platform}:{external_bot_id}",
        }
    )
    if isinstance(conn, PostgresCompatConnection):
        for subject in subjects:
            conn.execute(
                "SELECT pg_advisory_xact_lock(?)",
                (_registration_lock_key(subject),),
            ).fetchone()
        return
    if isinstance(conn, sqlite3.Connection) and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


class ConnectionRepository(_BaseConnectionRepository):
    """Canonical connection repository with managed-bot lifecycle transitions."""

    def _locked_managed_bot(
        self,
        *,
        actor: TenantContext,
        managed_bot_id: str,
    ) -> tuple[TenantContext, str, ManagedBot]:
        current = self._resolve_actor(actor)
        normalized_id = normalize_uuid(
            managed_bot_id,
            field_name="managed_bot_id",
        )
        observed = self._get_managed_bot(
            business_id=current.business_id,
            managed_bot_id=normalized_id,
        )
        _serialize_registration(
            self._conn,
            business_id=current.business_id,
            platform=observed.platform.value,
            external_bot_id=observed.external_bot_id,
        )
        current_bot = self._get_managed_bot(
            business_id=current.business_id,
            managed_bot_id=normalized_id,
        )
        return current, normalized_id, current_bot

    def register_managed_bot(
        self,
        *,
        actor: TenantContext,
        connection_id: str,
        external_bot_id: str,
        webhook_secret_reference: str,
        username: str | None = None,
        display_name: str | None = None,
        now: str | None = None,
    ) -> ManagedBot:
        current = self._resolve_actor(actor)
        normalized_connection_id = normalize_uuid(
            connection_id,
            field_name="connection_id",
        )
        connection = self._get_connection(
            business_id=current.business_id,
            connection_id=normalized_connection_id,
        )
        if connection.connection_type not in {
            ConnectionType.TELEGRAM_MANAGED_BOT,
            ConnectionType.MAX_PERSONAL_BOT,
        }:
            raise ConnectionNotFound(
                "managed bot requires a managed or personal bot connection"
            )
        normalized_external_bot_id = normalize_external_account_id(external_bot_id)
        _serialize_registration(
            self._conn,
            business_id=current.business_id,
            platform=connection.platform.value,
            external_bot_id=normalized_external_bot_id,
        )
        external_row = self._conn.execute(
            """
            SELECT id, business_id, connection_id, platform, external_bot_id,
                   username, display_name, webhook_secret_reference, status,
                   created_at, revoked_at, updated_at
            FROM managed_bots
            WHERE platform=? AND external_bot_id=?
            LIMIT 1
            """,
            (connection.platform.value, normalized_external_bot_id),
        ).fetchone()
        if external_row is not None:
            existing = _managed_bot_from_row(external_row)
            if (
                existing.business_id != current.business_id
                or existing.connection_id != connection.id
            ):
                raise ConnectionInvariantViolation(
                    "managed bot is already bound to another tenant route"
                )
            return existing
        active_row = self._conn.execute(
            """
            SELECT id
            FROM managed_bots
            WHERE business_id=? AND platform=? AND status='active'
            LIMIT 1
            """,
            (current.business_id, connection.platform.value),
        ).fetchone()
        if active_row is not None:
            raise ConnectionInvariantViolation(
                "business already has an active managed bot for this platform"
            )
        return super().register_managed_bot(
            actor=current,
            connection_id=connection.id,
            external_bot_id=normalized_external_bot_id,
            webhook_secret_reference=webhook_secret_reference,
            username=username,
            display_name=display_name,
            now=now,
        )

    def _terminate_managed_bot_ingress(
        self,
        *,
        managed_bot_id: str,
        business_id: str,
        timestamp: str,
        reason: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE bot_gateway_ingress_events
            SET status='dead', payload_json=NULL, updated_at=?, dead_at=?,
                locked_at=NULL, lock_token=NULL, last_error_code=?
            WHERE managed_bot_id=? AND business_id=?
              AND status IN ('pending','processing','retry')
            """,
            (
                timestamp,
                timestamp,
                reason,
                managed_bot_id,
                business_id,
            ),
        )

    def disable_managed_bot(
        self,
        *,
        actor: TenantContext,
        managed_bot_id: str,
        now: str | None = None,
    ) -> ManagedBot:
        current, normalized_id, bot = self._locked_managed_bot(
            actor=actor,
            managed_bot_id=managed_bot_id,
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
        self._terminate_managed_bot_ingress(
            managed_bot_id=normalized_id,
            business_id=current.business_id,
            timestamp=timestamp,
            reason="managed_bot_disabled",
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
        current, normalized_id, bot = self._locked_managed_bot(
            actor=actor,
            managed_bot_id=managed_bot_id,
        )
        if bot.status == ManagedBotStatus.REVOKED:
            raise ConnectionNotFound("revoked managed bot cannot be activated")
        if bot.status == ManagedBotStatus.ACTIVE:
            return bot
        conflict = self._conn.execute(
            """
            SELECT id
            FROM managed_bots
            WHERE business_id=? AND platform=? AND status='active' AND id!=?
            LIMIT 1
            """,
            (
                current.business_id,
                bot.platform.value,
                normalized_id,
            ),
        ).fetchone()
        if conflict is not None:
            raise ConnectionInvariantViolation(
                "business already has another active managed bot for this platform"
            )
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
        current, normalized_id, bot = self._locked_managed_bot(
            actor=actor,
            managed_bot_id=managed_bot_id,
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
        self._terminate_managed_bot_ingress(
            managed_bot_id=normalized_id,
            business_id=current.business_id,
            timestamp=timestamp,
            reason="managed_bot_revoked",
        )
        return self._get_managed_bot(
            business_id=current.business_id,
            managed_bot_id=normalized_id,
        )
