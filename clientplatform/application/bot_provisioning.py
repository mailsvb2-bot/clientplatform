from __future__ import annotations

import asyncio

from clientplatform.domain.bot_provisioning import (
    BotProvisioningStatus,
    BotProvisioningVerificationFailed,
    ManagedBotProvisioningRequest,
    VerifiedTelegramBot,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.bot_provisioning_repository import (
    BotProvisioningRepository,
    ProvisioningVerificationLease,
)
from clientplatform.runtime.bot_provisioning import (
    BotFatherTelegramProvisioner,
    ManagedBotProvisioner,
)
from services.db import get_db, get_db_ro


def create_botfather_provisioning(
    *,
    actor: TenantContext,
    idempotency_key: str,
    requested_username: str | None = None,
    display_name: str | None = None,
) -> ManagedBotProvisioningRequest:
    with get_db() as conn:
        return BotProvisioningRepository(conn).create_request(
            actor=actor,
            provider="botfather",
            idempotency_key=idempotency_key,
            requested_username=requested_username,
            display_name=display_name,
        )


def submit_botfather_secret_references(
    *,
    actor: TenantContext,
    request_id: str,
    credential_reference: str,
    webhook_secret_reference: str,
) -> ManagedBotProvisioningRequest:
    with get_db() as conn:
        return BotProvisioningRepository(conn).submit_secret_references(
            actor=actor,
            request_id=request_id,
            credential_reference=credential_reference,
            webhook_secret_reference=webhook_secret_reference,
        )


def cancel_botfather_provisioning(
    *,
    actor: TenantContext,
    request_id: str,
) -> ManagedBotProvisioningRequest:
    with get_db() as conn:
        return BotProvisioningRepository(conn).cancel(
            actor=actor,
            request_id=request_id,
        )


def get_bot_provisioning(
    *,
    actor: TenantContext,
    request_id: str,
) -> ManagedBotProvisioningRequest:
    with get_db_ro() as conn:
        return BotProvisioningRepository(conn).get(
            actor=actor,
            request_id=request_id,
        )


def list_bot_provisioning_requests(
    *,
    actor: TenantContext,
    limit: int = 50,
) -> list[ManagedBotProvisioningRequest]:
    with get_db_ro() as conn:
        return BotProvisioningRepository(conn).list_for_business(
            actor=actor,
            limit=limit,
        )


def _begin_verification(
    *,
    actor: TenantContext,
    request_id: str,
) -> ProvisioningVerificationLease:
    with get_db() as conn:
        return BotProvisioningRepository(conn).begin_verification(
            actor=actor,
            request_id=request_id,
        )


def _complete_verification(
    *,
    actor: TenantContext,
    lease: ProvisioningVerificationLease,
    verified_bot: VerifiedTelegramBot,
) -> ManagedBotProvisioningRequest:
    with get_db() as conn:
        return BotProvisioningRepository(conn).complete_verified(
            actor=actor,
            lease=lease,
            verified_bot=verified_bot,
        )


def _fail_verification(
    *,
    actor: TenantContext,
    lease: ProvisioningVerificationLease,
    error_code: str,
) -> ManagedBotProvisioningRequest:
    with get_db() as conn:
        return BotProvisioningRepository(conn).fail_verification(
            actor=actor,
            lease=lease,
            error_code=error_code,
        )


async def finalize_botfather_provisioning(
    *,
    actor: TenantContext,
    request_id: str,
    provisioner: ManagedBotProvisioner | None = None,
) -> ManagedBotProvisioningRequest:
    """Verify BotFather credentials, configure webhook and commit the route.

    Telegram network work happens outside the database transaction. The final
    connection and managed-bot rows are committed atomically. If that commit
    fails, the configured webhook is removed as a compensating action.
    """

    current = await asyncio.to_thread(
        get_bot_provisioning,
        actor=actor,
        request_id=request_id,
    )
    if current.status == BotProvisioningStatus.COMPLETED:
        return current

    lease = await asyncio.to_thread(
        _begin_verification,
        actor=actor,
        request_id=request_id,
    )
    adapter = provisioner or BotFatherTelegramProvisioner()
    try:
        verified_bot = await adapter.provision(lease.request)
    except asyncio.CancelledError:
        await adapter.rollback(lease.request)
        await asyncio.to_thread(
            _fail_verification,
            actor=actor,
            lease=lease,
            error_code="verification_cancelled",
        )
        raise
    except BotProvisioningVerificationFailed:
        await asyncio.to_thread(
            _fail_verification,
            actor=actor,
            lease=lease,
            error_code="telegram_verification_failed",
        )
        raise
    except Exception as exc:  # validator: allow-wide-except
        await asyncio.to_thread(
            _fail_verification,
            actor=actor,
            lease=lease,
            error_code="provisioner_failed",
        )
        raise BotProvisioningVerificationFailed(
            "managed bot provisioner failed"
        ) from exc

    try:
        return await asyncio.to_thread(
            _complete_verification,
            actor=actor,
            lease=lease,
            verified_bot=verified_bot,
        )
    except asyncio.CancelledError:
        await adapter.rollback(lease.request)
        await asyncio.to_thread(
            _fail_verification,
            actor=actor,
            lease=lease,
            error_code="commit_cancelled",
        )
        raise
    except Exception:  # validator: allow-wide-except
        await adapter.rollback(lease.request)
        await asyncio.to_thread(
            _fail_verification,
            actor=actor,
            lease=lease,
            error_code="provisioning_commit_failed",
        )
        raise
