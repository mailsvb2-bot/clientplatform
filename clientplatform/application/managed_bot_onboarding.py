from __future__ import annotations

from clientplatform.application.bot_provisioning import finalize_botfather_provisioning
from clientplatform.domain.bot_provisioning import ManagedBotProvisioningRequest
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.bot_provisioning_repository import (
    BotProvisioningRepository,
)
from clientplatform.infrastructure.managed_bot_credentials import (
    AgeManagedBotCredentialVault,
    ManagedBotCredentialStore,
    ManagedBotCredentialVault,
)
from clientplatform.infrastructure.managed_bot_onboarding_repository import (
    ManagedBotOnboardingRepository,
)
from clientplatform.runtime.bot_provisioning import (
    BotFatherTelegramProvisioner,
    ManagedBotProvisioner,
)
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from services.db import get_db


def begin_telegram_managed_bot_onboarding(
    *,
    actor: TenantContext,
    idempotency_key: str,
    display_name: str | None = None,
) -> ManagedBotProvisioningRequest:
    """Create one durable Managed Bots request for the initiating Telegram user."""

    with get_db() as conn:
        return ManagedBotOnboardingRepository(conn).create(
            actor=actor,
            idempotency_key=idempotency_key,
            # Suggested usernames in Telegram are advisory. Do not persist one as
            # a verification invariant because the owner is allowed to edit it.
            suggested_username=None,
            display_name=display_name,
        )


async def complete_telegram_managed_bot_onboarding(
    *,
    user_id: int,
    external_bot_id: str,
    username: str,
    display_name: str | None,
    token: str,
    vault: ManagedBotCredentialVault | None = None,
    provisioner: ManagedBotProvisioner | None = None,
) -> ManagedBotProvisioningRequest:
    """Seal a Telegram-issued token, bind it to the durable request and verify it.

    The raw token is accepted only as an in-memory argument obtained from the
    manager bot API. Persistence receives encrypted ciphertext plus an opaque
    ``vault://`` reference; neither logs nor domain objects contain the token.
    """

    raw_token = str(token or "").strip()
    if not raw_token:
        raise ValueError("managed bot token is unavailable")
    selected_vault = vault or AgeManagedBotCredentialVault()
    with get_db() as conn:
        pending = ManagedBotOnboardingRepository(conn).pending_for_user(
            user_id=user_id
        )
        credential_reference = ManagedBotCredentialStore(
            conn,
            vault=selected_vault,
        ).put(
            actor=pending.actor,
            external_bot_id=external_bot_id,
            token=raw_token,
        )
        request = BotProvisioningRepository(conn).submit_secret_references(
            actor=pending.actor,
            request_id=pending.request.id,
            credential_reference=credential_reference,
            # The historical column is retained by the polling schema. It points
            # to the same encrypted token reference and is never used as a webhook secret.
            webhook_secret_reference=credential_reference,
        )

    selected_provisioner = provisioner or BotFatherTelegramProvisioner(
        credential_provider=EnvironmentCredentialProvider(
            managed_bot_vault=selected_vault
        )
    )
    completed = await finalize_botfather_provisioning(
        actor=pending.actor,
        request_id=request.id,
        provisioner=selected_provisioner,
    )
    # The verifier reads the authoritative Telegram identity from the child bot.
    # These inputs are used only to fail early on obviously mismatched events.
    if completed.external_bot_id != str(external_bot_id):
        raise RuntimeError("managed bot identity changed during provisioning")
    if completed.verified_username != str(username or "").strip().lstrip("@").lower():
        raise RuntimeError("managed bot username changed during provisioning")
    return completed


__all__ = [
    "begin_telegram_managed_bot_onboarding",
    "complete_telegram_managed_bot_onboarding",
]
