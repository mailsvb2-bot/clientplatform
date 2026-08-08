from __future__ import annotations

import asyncio

from clientplatform.domain.bot_provisioning import (
    BotProvisioningStatus,
    BotProvisioningVerificationFailed,
    ManagedBotProvisioningRequest,
    VerifiedTelegramBot,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure import BotProvisioningRepository
from clientplatform.infrastructure.bot_provisioning_repository import (
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
    webhook_secret_reference: str | None = None,
) -> ManagedBotProvisioningRequest:
    """Store the BotFather token reference for polling-only Telegram ingress.

    The schema retains the historical webhook-reference column for backward
    compatibility. New polling connections mirror the reviewed credential
    reference into that unused column and never resolve it as a webhook secret.
    """

    compatibility_reference = webhook_secret_reference or credential_reference
    with get_db() as conn:
        return BotProvisioningRepository(conn).submit_secret_references(
            actor=actor,
            request_id=request_id,
            credential_reference=credential_reference,
            webhook_secret_reference=compatibility_reference,
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


async def _rollback_then_fail_verification(
    *,
    adapter: ManagedBotProvisioner,
    actor: TenantContext,
    lease: ProvisioningVerificationLease,
    error_code: str,
) -> Exception | None:
    """Attempt external rollback without letting it strand VERIFYING state.

    Rollback still runs first so a retry cannot race a known external cleanup.
    If rollback itself fails, the durable FAILED transition is nevertheless
    recorded in ``finally`` and the caller can preserve the original failure as
    the primary exception while attaching a diagnostic note.
    """

    rollback_error: Exception | None = None
    try:
        await adapter.rollback(lease.request)
    except Exception as exc:  # validator: allow-wide-except
        rollback_error = exc
    finally:
        await asyncio.to_thread(
            _fail_verification,
            actor=actor,
            lease=lease,
            error_code=error_code,
        )
    return rollback_error


def _annotate_rollback_failure(
    primary_error: BaseException,
    rollback_error: Exception | None,
) -> None:
    if rollback_error is not None:
        primary_error.add_note("managed bot rollback failed during compensation")


async def finalize_botfather_provisioning(
    *,
    actor: TenantContext,
    request_id: str,
    provisioner: ManagedBotProvisioner | None = None,
) -> ManagedBotProvisioningRequest:
    """Verify BotFather credentials, remove any webhook and commit polling.

    Telegram network work happens outside the database transaction. The final
    connection and managed-bot rows are committed atomically. If that commit
    fails, the compensating action again removes any webhook, preserving the
    polling-only boundary.
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
    except asyncio.CancelledError as exc:
        rollback_error = await _rollback_then_fail_verification(
            adapter=adapter,
            actor=actor,
            lease=lease,
            error_code="verification_cancelled",
        )
        _annotate_rollback_failure(exc, rollback_error)
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
    except asyncio.CancelledError as exc:
        rollback_error = await _rollback_then_fail_verification(
            adapter=adapter,
            actor=actor,
            lease=lease,
            error_code="commit_cancelled",
        )
        _annotate_rollback_failure(exc, rollback_error)
        raise
    except Exception as exc:  # validator: allow-wide-except
        rollback_error = await _rollback_then_fail_verification(
            adapter=adapter,
            actor=actor,
            lease=lease,
            error_code="provisioning_commit_failed",
        )
        _annotate_rollback_failure(exc, rollback_error)
        raise
