from __future__ import annotations

import asyncio
import logging

from clientplatform.application.connections import (
    activate_managed_bot,
    disable_managed_bot,
)
from clientplatform.domain.managed_bot_owner import (
    ManagedBotOwnerLifecycleResult,
    ManagedBotOwnerSnapshot,
    ManagedBotWebhookMaterial,
    ManagedBotWebhookOperationFailed,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure import ConnectionRepository
from clientplatform.infrastructure.managed_bot_credentials import (
    AgeManagedBotCredentialVault,
    ManagedBotCredentialError,
    ManagedBotCredentialStore,
)
from clientplatform.infrastructure.managed_bot_owner_repository import (
    ManagedBotOwnerRepository,
)
from clientplatform.runtime.managed_bot_owner import (
    ManagedBotWebhookController,
    TelegramManagedBotWebhookController,
)
from services.db import get_db, get_db_ro

log = logging.getLogger(__name__)


def get_managed_bot_owner_snapshot(
    *,
    actor: TenantContext,
    managed_bot_id: str,
) -> ManagedBotOwnerSnapshot:
    with get_db_ro() as conn:
        return ManagedBotOwnerRepository(conn).snapshot(
            actor=actor,
            managed_bot_id=managed_bot_id,
        )


def _get_webhook_material(
    *,
    actor: TenantContext,
    managed_bot_id: str,
) -> ManagedBotWebhookMaterial:
    with get_db_ro() as conn:
        return ManagedBotOwnerRepository(conn).webhook_material(
            actor=actor,
            managed_bot_id=managed_bot_id,
        )


def _revoke_managed_bot_and_credential(
    *,
    actor: TenantContext,
    managed_bot_id: str,
    material: ManagedBotWebhookMaterial,
) -> None:
    """Revoke the route and make a managed-bot token unrecoverable atomically."""

    with get_db() as conn:
        ConnectionRepository(conn).revoke_managed_bot(
            actor=actor,
            managed_bot_id=managed_bot_id,
        )
        if not material.credential_reference.startswith("vault://managed-bot/"):
            return
        store = ManagedBotCredentialStore(
            conn,
            vault=AgeManagedBotCredentialVault(),
        )
        if not store.revoke(
            actor=actor,
            reference=material.credential_reference,
        ):
            raise ManagedBotCredentialError(
                "managed bot credential was not active during revocation"
            )
        cursor = conn.execute(
            """
            UPDATE managed_bot_credentials
            SET ciphertext='revoked'
            WHERE business_id=? AND external_bot_id=? AND status='revoked'
            """,
            (material.business_id, material.external_bot_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ManagedBotCredentialError(
                "managed bot credential erasure did not affect exactly one record"
            )


async def _snapshot_async(
    *,
    actor: TenantContext,
    managed_bot_id: str,
) -> ManagedBotOwnerSnapshot:
    return await asyncio.to_thread(
        get_managed_bot_owner_snapshot,
        actor=actor,
        managed_bot_id=managed_bot_id,
    )


async def _rollback_webhook(
    controller: ManagedBotWebhookController,
    material: ManagedBotWebhookMaterial,
) -> None:
    try:
        await controller.detach(material)
    except ManagedBotWebhookOperationFailed:
        log.exception(
            "Managed bot activation rollback could not detach Telegram webhook",
            extra={"managed_bot_id": material.managed_bot_id},
        )


async def disable_managed_bot_for_owner(
    *,
    actor: TenantContext,
    managed_bot_id: str,
    controller: ManagedBotWebhookController | None = None,
) -> ManagedBotOwnerLifecycleResult:
    material = await asyncio.to_thread(
        _get_webhook_material,
        actor=actor,
        managed_bot_id=managed_bot_id,
    )
    await asyncio.to_thread(
        disable_managed_bot,
        actor=actor,
        managed_bot_id=managed_bot_id,
    )
    adapter = controller or TelegramManagedBotWebhookController()
    warning_code: str | None = None
    try:
        await adapter.detach(material)
    except ManagedBotWebhookOperationFailed:
        warning_code = "webhook_detach_failed"
    snapshot = await _snapshot_async(
        actor=actor,
        managed_bot_id=managed_bot_id,
    )
    return ManagedBotOwnerLifecycleResult(
        snapshot=snapshot,
        webhook_synchronized=warning_code is None,
        warning_code=warning_code,
    )


async def activate_managed_bot_for_owner(
    *,
    actor: TenantContext,
    managed_bot_id: str,
    controller: ManagedBotWebhookController | None = None,
) -> ManagedBotOwnerLifecycleResult:
    material = await asyncio.to_thread(
        _get_webhook_material,
        actor=actor,
        managed_bot_id=managed_bot_id,
    )
    adapter = controller or TelegramManagedBotWebhookController()
    await adapter.attach(material)
    try:
        await asyncio.to_thread(
            activate_managed_bot,
            actor=actor,
            managed_bot_id=managed_bot_id,
        )
    except asyncio.CancelledError:
        await _rollback_webhook(adapter, material)
        raise
    except Exception:  # validator: allow-wide-except
        await _rollback_webhook(adapter, material)
        raise
    snapshot = await _snapshot_async(
        actor=actor,
        managed_bot_id=managed_bot_id,
    )
    return ManagedBotOwnerLifecycleResult(
        snapshot=snapshot,
        webhook_synchronized=True,
    )


async def revoke_managed_bot_for_owner(
    *,
    actor: TenantContext,
    managed_bot_id: str,
    controller: ManagedBotWebhookController | None = None,
) -> ManagedBotOwnerLifecycleResult:
    material = await asyncio.to_thread(
        _get_webhook_material,
        actor=actor,
        managed_bot_id=managed_bot_id,
    )
    await asyncio.to_thread(
        _revoke_managed_bot_and_credential,
        actor=actor,
        managed_bot_id=managed_bot_id,
        material=material,
    )
    adapter = controller or TelegramManagedBotWebhookController()
    warning_code: str | None = None
    try:
        await adapter.detach(material)
    except ManagedBotWebhookOperationFailed:
        warning_code = "webhook_detach_failed"
    snapshot = await _snapshot_async(
        actor=actor,
        managed_bot_id=managed_bot_id,
    )
    return ManagedBotOwnerLifecycleResult(
        snapshot=snapshot,
        webhook_synchronized=warning_code is None,
        warning_code=warning_code,
    )
