from __future__ import annotations

from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.native_messenger_setup_repository import (
    IssuedNativeMessengerSetup,
    NativeMessengerSetupGrant,
    NativeMessengerSetupRepository,
)
from services.db import get_db, get_db_ro


def issue_native_messenger_setup(
    *,
    actor: TenantContext,
    platform: ConnectionPlatform | str,
    ttl_seconds: int = 600,
) -> IssuedNativeMessengerSetup:
    with get_db() as conn:
        return NativeMessengerSetupRepository(conn).issue(
            actor=actor,
            platform=platform,
            ttl_seconds=ttl_seconds,
        )


def inspect_native_messenger_setup(*, token: str) -> NativeMessengerSetupGrant:
    with get_db_ro() as conn:
        return NativeMessengerSetupRepository(conn).inspect(token=token)


def consume_native_messenger_setup(*, token: str) -> NativeMessengerSetupGrant:
    with get_db() as conn:
        return NativeMessengerSetupRepository(conn).consume(token=token)


__all__ = [
    "consume_native_messenger_setup",
    "inspect_native_messenger_setup",
    "issue_native_messenger_setup",
]
