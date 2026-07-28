from __future__ import annotations

from clientplatform.domain.connections import (
    Connection,
    ConnectionPlatform,
    ConnectionType,
    Dispatch,
    ManagedBot,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure import ConnectionRepository, DispatchOutboxRepository
from services.db import get_db, get_db_ro


def create_connection(
    *,
    actor: TenantContext,
    platform: ConnectionPlatform | str,
    connection_type: ConnectionType | str,
    external_account_id: str,
    credential_reference: str,
    permissions: list[str] | tuple[str, ...] = (),
) -> Connection:
    with get_db() as conn:
        return ConnectionRepository(conn).create_connection(
            actor=actor,
            platform=platform,
            connection_type=connection_type,
            external_account_id=external_account_id,
            credential_reference=credential_reference,
            permissions=permissions,
        )


def activate_connection(
    *,
    actor: TenantContext,
    connection_id: str,
) -> Connection:
    with get_db() as conn:
        return ConnectionRepository(conn).activate_connection(
            actor=actor,
            connection_id=connection_id,
        )


def disable_connection(
    *,
    actor: TenantContext,
    connection_id: str,
) -> Connection:
    with get_db() as conn:
        return ConnectionRepository(conn).disable_connection(
            actor=actor,
            connection_id=connection_id,
        )


def register_managed_bot(
    *,
    actor: TenantContext,
    connection_id: str,
    external_bot_id: str,
    webhook_secret_reference: str,
    username: str | None = None,
    display_name: str | None = None,
) -> ManagedBot:
    with get_db() as conn:
        return ConnectionRepository(conn).register_managed_bot(
            actor=actor,
            connection_id=connection_id,
            external_bot_id=external_bot_id,
            webhook_secret_reference=webhook_secret_reference,
            username=username,
            display_name=display_name,
        )


def list_connections(*, actor: TenantContext) -> list[Connection]:
    with get_db_ro() as conn:
        return ConnectionRepository(conn).list_connections(actor=actor)


def prepare_lesson_dispatch(
    *,
    actor: TenantContext,
    logical_delivery_id: str,
    connection_id: str,
    customer_identity_id: str,
) -> Dispatch:
    with get_db() as conn:
        return DispatchOutboxRepository(conn).materialize(
            actor=actor,
            logical_delivery_id=logical_delivery_id,
            connection_id=connection_id,
            customer_identity_id=customer_identity_id,
        )


def get_dispatch(
    *,
    actor: TenantContext,
    dispatch_id: str,
) -> Dispatch:
    with get_db_ro() as conn:
        return DispatchOutboxRepository(conn).get_dispatch(
            actor=actor,
            dispatch_id=dispatch_id,
        )
