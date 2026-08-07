from __future__ import annotations

import asyncio
import re

from clientplatform.application.bot_provisioning import finalize_botfather_provisioning
from clientplatform.domain.bot_provisioning import (
    BotProvisioningProvider,
    BotProvisioningVerificationFailed,
    ManagedBotProvisioningRequest,
    VerifiedTelegramBot,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.bot_provisioning_repository import (
    BotProvisioningRepository,
)
from clientplatform.infrastructure.managed_bot_credentials import (
    AgeManagedBotCredentialVault,
    ManagedBotCredentialStore,
    ManagedBotCredentialVault,
)
from clientplatform.runtime.bot_provisioning import (
    BotFatherTelegramProvisioner,
    ManagedBotProvisioner,
)
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from services.db import get_db


_TOKEN_RE = re.compile(r"([1-9][0-9]{4,}):([A-Za-z0-9_-]{20,})")


def _bot_id_from_token(token: str) -> str:
    match = _TOKEN_RE.fullmatch(str(token or "").strip())
    if match is None:
        raise ValueError("Telegram bot token has an invalid format")
    return match.group(1)


class _ExpectedExistingBotProvisioner:
    """Require Telegram getMe identity to match the token's bot-id prefix."""

    def __init__(
        self,
        delegate: ManagedBotProvisioner,
        *,
        expected_external_bot_id: str,
    ) -> None:
        self._delegate = delegate
        self._expected_external_bot_id = str(expected_external_bot_id)

    async def provision(
        self,
        request: ManagedBotProvisioningRequest,
    ) -> VerifiedTelegramBot:
        verified = await self._delegate.provision(request)
        if verified.external_bot_id != self._expected_external_bot_id:
            raise BotProvisioningVerificationFailed(
                "Telegram bot token identity does not match its bot id"
            )
        return verified

    async def rollback(self, request: ManagedBotProvisioningRequest) -> None:
        await self._delegate.rollback(request)


async def connect_existing_telegram_bot(
    *,
    actor: TenantContext,
    token: str,
    idempotency_key: str,
    vault: ManagedBotCredentialVault | None = None,
    provisioner: ManagedBotProvisioner | None = None,
) -> ManagedBotProvisioningRequest:
    """Connect an existing BotFather bot from one transient token submission.

    The raw token is never stored in the provisioning or connection tables. It
    is sealed immediately and only the opaque vault reference is persisted.
    Telegram verification then reopens it through the normal credential
    provider, so the existing durable provisioning state machine remains the
    single source of truth.
    """

    raw_token = str(token or "").strip()
    external_bot_id = _bot_id_from_token(raw_token)
    selected_vault = vault or AgeManagedBotCredentialVault()

    with get_db() as conn:
        repository = BotProvisioningRepository(conn)
        request = repository.create_request(
            actor=actor,
            provider=BotProvisioningProvider.BOTFATHER,
            idempotency_key=idempotency_key,
            requested_username=None,
            display_name=None,
        )
        credential_reference = ManagedBotCredentialStore(
            conn,
            vault=selected_vault,
        ).put(
            actor=actor,
            external_bot_id=external_bot_id,
            token=raw_token,
        )
        request = repository.submit_secret_references(
            actor=actor,
            request_id=request.id,
            credential_reference=credential_reference,
            webhook_secret_reference=credential_reference,
        )

    delegate = provisioner or BotFatherTelegramProvisioner(
        credential_provider=EnvironmentCredentialProvider(
            managed_bot_vault=selected_vault,
        )
    )
    selected_provisioner = _ExpectedExistingBotProvisioner(
        delegate,
        expected_external_bot_id=external_bot_id,
    )
    try:
        return await finalize_botfather_provisioning(
            actor=actor,
            request_id=request.id,
            provisioner=selected_provisioner,
        )
    except asyncio.CancelledError:
        with get_db() as conn:
            ManagedBotCredentialStore(conn, vault=selected_vault).revoke(
                actor=actor,
                reference=credential_reference,
            )
        raise
    except Exception:  # validator: allow-wide-except
        with get_db() as conn:
            ManagedBotCredentialStore(conn, vault=selected_vault).revoke(
                actor=actor,
                reference=credential_reference,
            )
        raise


__all__ = [
    "connect_existing_telegram_bot",
]
