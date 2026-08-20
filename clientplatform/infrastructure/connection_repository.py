from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.connections import (
    Connection,
    ConnectionInvariantViolation,
    ConnectionNotFound,
    ConnectionPlatform,
    ConnectionStatus,
    ConnectionType,
    ManagedBot,
    ManagedBotStatus,
    decode_permissions,
    encode_permissions,
    normalize_credential_reference,
    normalize_external_account_id,
    validate_connection_type_platform,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.connection_credentials import (
    ConnectionCredentialError,
    assert_connection_credential_reference_business,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _connection_from_row(row: Any) -> Connection:
    last_success_at = _value(row, "last_success_at", 11)
    last_error_at = _value(row, "last_error_at", 12)
    last_error_code = _value(row, "last_error_code", 13)
    return Connection(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        platform=ConnectionPlatform(str(_value(row, "platform", 2))),
        connection_type=ConnectionType(str(_value(row, "connection_type", 3))),
        external_account_id=str(_value(row, "external_account_id", 4)),
        credential_reference=str(_value(row, "credential_reference", 5)),
        permissions=decode_permissions(str(_value(row, "permissions_json", 6))),
        status=ConnectionStatus(str(_value(row, "status", 7))),
        created_by_member_id=str(_value(row, "created_by_member_id", 8)),
        created_at=str(_value(row, "created_at", 9)),
        updated_at=str(_value(row, "updated_at", 10)),
        last_success_at=None if last_success_at is None else str(last_success_at),
        last_error_at=None if last_error_at is None else str(last_error_at),
        last_error_code=None if last_error_code is None else str(last_error_code),
    )


def _managed_bot_from_row(row: Any) -> ManagedBot:
    revoked_at = _value(row, "revoked_at", 10)
    return ManagedBot(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        connection_id=str(_value(row, "connection_id", 2)),
        platform=ConnectionPlatform(str(_value(row, "platform", 3))),
        external_bot_id=str(_value(row, "external_bot_id", 4)),
        username=_value(row, "username", 5),
        display_name=_value(row, "display_name", 6),
        webhook_secret_reference=str(_value(row, "webhook_secret_reference", 7)),
        status=ManagedBotStatus(str(_value(row, "status", 8))),
        created_at=str(_value(row, "created_at", 9)),
        updated_at=str(_value(row, "updated_at", 11)),
        revoked_at=None if revoked_at is None else str(revoked_at),
    )


class ConnectionRepository:
    """Business connections storing only secret-manager references."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _resolve_actor(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        return current

    def create_connection(
        self,
        *,
        actor: TenantContext,
        platform: ConnectionPlatform | str,
        connection_type: ConnectionType | str,
        external_account_id: str,
        credential_reference: str,
        permissions: list[str] | tuple[str, ...] = (),
        now: str | None = None,
    ) -> Connection:
        current = self._resolve_actor(actor)
        normalized_platform, normalized_type = validate_connection_type_platform(
            platform=platform,
            connection_type=connection_type,
        )
        normalized_external_id = normalize_external_account_id(external_account_id)
        normalized_secret_ref = normalize_credential_reference(credential_reference)
        try:
            assert_connection_credential_reference_business(
                normalized_secret_ref, current.business_id
            )
        except ConnectionCredentialError as exc:
            raise ConnectionInvariantViolation(str(exc)) from None
        encoded_permissions = encode_permissions(permissions)
        timestamp = str(now or _utc_now())
        connection_id = str(uuid4())
        try:
            self._conn.execute(
                """
                INSERT INTO connections(
                    id, business_id, platform, connection_type,
                    external_account_id, credential_reference, permissions_json,
                    status, created_by_member_id, created_at, updated_at,
                    last_success_at, last_error_at, last_error_code
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    connection_id,
                    current.business_id,
                    normalized_platform.value,
                    normalized_type.value,
                    normalized_external_id,
                    normalized_secret_ref,
                    encoded_permissions,
                    current.membership_id,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            existing = self._find_by_external(
                business_id=current.business_id,
                platform=normalized_platform,
                connection_type=normalized_type,
                external_account_id=normalized_external_id,
            )
            if existing is None:
                raise
            return existing
        return self._get_connection(
            business_id=current.business_id,
            connection_id=connection_id,
        )

    def replace_credential_reference(
        self,
        *,
        actor: TenantContext,
        connection_id: str,
        credential_reference: str,
        now: str | None = None,
    ) -> Connection:
        current = self._resolve_actor(actor)
        normalized_connection_id = normalize_uuid(
            connection_id, field_name="connection_id"
        )
        normalized_reference = normalize_credential_reference(credential_reference)
        try:
            assert_connection_credential_reference_business(
                normalized_reference, current.business_id
            )
        except ConnectionCredentialError as exc:
            raise ConnectionInvariantViolation(str(exc)) from None
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE connections
            SET credential_reference=?, updated_at=?
            WHERE id=? AND business_id=? AND status!='revoked'
            """,
            (
                normalized_reference,
                timestamp,
                normalized_connection_id,
                current.business_id,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ConnectionNotFound("connection was not found in the business")
        return self._get_connection(
            business_id=current.business_id,
            connection_id=normalized_connection_id,
        )

    def activate_connection(
        self,
        *,
        actor: TenantContext,
        connection_id: str,
        now: str | None = None,
    ) -> Connection:
        current = self._resolve_actor(actor)
        normalized_connection_id = normalize_uuid(
            connection_id,
            field_name="connection_id",
        )
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE connections
            SET status='active', last_success_at=?, last_error_at=NULL,
                last_error_code=NULL, updated_at=?
            WHERE id=? AND business_id=? AND status IN ('pending', 'attention', 'disabled')
            """,
            (
                timestamp,
                timestamp,
                normalized_connection_id,
                current.business_id,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) == 0:
            current_connection = self._get_connection(
                business_id=current.business_id,
                connection_id=normalized_connection_id,
            )
            if current_connection.status != ConnectionStatus.ACTIVE:
                raise ConnectionNotFound("connection cannot be activated")
            return current_connection
        return self._get_connection(
            business_id=current.business_id,
            connection_id=normalized_connection_id,
        )

    def disable_connection(
        self,
        *,
        actor: TenantContext,
        connection_id: str,
        now: str | None = None,
    ) -> Connection:
        current = self._resolve_actor(actor)
        normalized_connection_id = normalize_uuid(
            connection_id,
            field_name="connection_id",
        )
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE connections
            SET status='disabled', updated_at=?
            WHERE id=? AND business_id=? AND status!='revoked'
            """,
            (timestamp, normalized_connection_id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ConnectionNotFound("connection was not found in the business")
        return self._get_connection(
            business_id=current.business_id,
            connection_id=normalized_connection_id,
        )

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
        connection = self._get_connection(
            business_id=current.business_id,
            connection_id=normalize_uuid(
                connection_id,
                field_name="connection_id",
            ),
        )
        if connection.connection_type not in {
            ConnectionType.TELEGRAM_MANAGED_BOT,
            ConnectionType.MAX_PERSONAL_BOT,
        }:
            raise ConnectionNotFound(
                "managed bot requires a managed or personal bot connection"
            )
        bot_id = str(uuid4())
        timestamp = str(now or _utc_now())
        normalized_external_bot_id = normalize_external_account_id(external_bot_id)
        webhook_reference = normalize_credential_reference(
            webhook_secret_reference
        )
        try:
            self._conn.execute(
                """
                INSERT INTO managed_bots(
                    id, business_id, connection_id, platform, external_bot_id,
                    username, display_name, webhook_secret_reference, status,
                    created_at, updated_at, revoked_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)
                """,
                (
                    bot_id,
                    current.business_id,
                    connection.id,
                    connection.platform.value,
                    normalized_external_bot_id,
                    None if username is None else str(username).strip() or None,
                    None if display_name is None else str(display_name).strip() or None,
                    webhook_reference,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                """
                SELECT id, business_id, connection_id, platform, external_bot_id,
                       username, display_name, webhook_secret_reference, status,
                       created_at, revoked_at, updated_at
                FROM managed_bots
                WHERE business_id=? AND platform=? AND external_bot_id=?
                LIMIT 1
                """,
                (
                    current.business_id,
                    connection.platform.value,
                    normalized_external_bot_id,
                ),
            ).fetchone()
            if row is None:
                raise
            return _managed_bot_from_row(row)
        return self._get_managed_bot(
            business_id=current.business_id,
            managed_bot_id=bot_id,
        )

    def list_connections(
        self,
        *,
        actor: TenantContext,
    ) -> list[Connection]:
        current = self._resolve_actor(actor)
        rows = self._conn.execute(
            """
            SELECT id, business_id, platform, connection_type,
                   external_account_id, credential_reference, permissions_json,
                   status, created_by_member_id, created_at, updated_at,
                   last_success_at, last_error_at, last_error_code
            FROM connections
            WHERE business_id=?
            ORDER BY created_at, id
            """,
            (current.business_id,),
        ).fetchall()
        return [_connection_from_row(row) for row in rows]

    def _find_by_external(
        self,
        *,
        business_id: str,
        platform: ConnectionPlatform,
        connection_type: ConnectionType,
        external_account_id: str,
    ) -> Connection | None:
        row = self._conn.execute(
            """
            SELECT id, business_id, platform, connection_type,
                   external_account_id, credential_reference, permissions_json,
                   status, created_by_member_id, created_at, updated_at,
                   last_success_at, last_error_at, last_error_code
            FROM connections
            WHERE business_id=? AND platform=? AND connection_type=?
              AND external_account_id=?
            LIMIT 1
            """,
            (
                business_id,
                platform.value,
                connection_type.value,
                external_account_id,
            ),
        ).fetchone()
        return None if row is None else _connection_from_row(row)

    def _get_connection(
        self,
        *,
        business_id: str,
        connection_id: str,
    ) -> Connection:
        row = self._conn.execute(
            """
            SELECT id, business_id, platform, connection_type,
                   external_account_id, credential_reference, permissions_json,
                   status, created_by_member_id, created_at, updated_at,
                   last_success_at, last_error_at, last_error_code
            FROM connections
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (connection_id, business_id),
        ).fetchone()
        if row is None:
            raise ConnectionNotFound("connection was not found in the business")
        return _connection_from_row(row)

    def _get_managed_bot(
        self,
        *,
        business_id: str,
        managed_bot_id: str,
    ) -> ManagedBot:
        row = self._conn.execute(
            """
            SELECT id, business_id, connection_id, platform, external_bot_id,
                   username, display_name, webhook_secret_reference, status,
                   created_at, revoked_at, updated_at
            FROM managed_bots
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (managed_bot_id, business_id),
        ).fetchone()
        if row is None:
            raise ConnectionNotFound("managed bot was not found in the business")
        return _managed_bot_from_row(row)
