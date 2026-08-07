from __future__ import annotations

from datetime import datetime, timedelta, timezone

from clientplatform.application.bot_provisioning import finalize_botfather_provisioning
from clientplatform.domain.bot_provisioning import (
    BotProvisioningInvariantViolation,
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
from clientplatform.infrastructure.managed_bot_onboarding_repository import (
    ManagedBotOnboardingRepository,
)
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from clientplatform.runtime.bot_provisioning import (
    BotFatherTelegramProvisioner,
    ManagedBotProvisioner,
)
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from services.db import get_db, get_db_ro


_EVENT_CLOCK_SKEW = timedelta(seconds=60)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _created_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _aware(parsed).astimezone(timezone.utc)


class _ExpectedManagedBotProvisioner:
    """Require the token identity to match the Telegram creation event."""

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
                "managed bot token identity does not match the creation event"
            )
        return verified

    async def rollback(self, request: ManagedBotProvisioningRequest) -> None:
        await self._delegate.rollback(request)


def has_active_telegram_managed_bot(*, actor: TenantContext) -> bool:
    """Return whether the current business has an actually active Telegram route."""

    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        row = conn.execute(
            """
            SELECT 1
            FROM managed_bots AS bot
            JOIN connections AS connection
              ON connection.id=bot.connection_id
             AND connection.business_id=bot.business_id
             AND connection.platform=bot.platform
            WHERE bot.business_id=?
              AND bot.platform='telegram'
              AND bot.status='active'
              AND connection.status='active'
            LIMIT 1
            """,
            (current.business_id,),
        ).fetchone()
        return row is not None


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
    event_at: datetime | None = None,
    vault: ManagedBotCredentialVault | None = None,
    provisioner: ManagedBotProvisioner | None = None,
) -> ManagedBotProvisioningRequest:
    """Seal a Telegram-issued token, bind it to the durable request and verify it.

    The raw token is accepted only as an in-memory argument obtained from the
    manager bot API. Persistence receives encrypted ciphertext plus an opaque
    ``vault://`` reference; neither logs nor domain objects contain the token.
    """

    event_identity = VerifiedTelegramBot(
        external_bot_id=external_bot_id,
        username=username,
        display_name=display_name,
    )
    raw_token = str(token or "").strip()
    if not raw_token:
        raise ValueError("managed bot token is unavailable")
    selected_vault = vault or AgeManagedBotCredentialVault()
    with get_db() as conn:
        pending = ManagedBotOnboardingRepository(conn).pending_for_user(
            user_id=user_id
        )
        if event_at is not None:
            observed_at = _aware(event_at).astimezone(timezone.utc)
            request_created_at = _created_at(pending.request.created_at)
            if observed_at + _EVENT_CLOCK_SKEW < request_created_at:
                raise BotProvisioningInvariantViolation(
                    "managed bot creation event predates the active request"
                )
        credential_reference = ManagedBotCredentialStore(
            conn,
            vault=selected_vault,
        ).put(
            actor=pending.actor,
            external_bot_id=event_identity.external_bot_id,
            token=raw_token,
        )
        request = BotProvisioningRepository(conn).submit_secret_references(
            actor=pending.actor,
            request_id=pending.request.id,
            credential_reference=credential_reference,
            # The historical column is retained by the polling schema. It points
            # to the same encrypted token reference and is not used as a webhook
            # secret by the polling-only runtime.
            webhook_secret_reference=credential_reference,
        )

    delegate = provisioner or BotFatherTelegramProvisioner(
        credential_provider=EnvironmentCredentialProvider(
            managed_bot_vault=selected_vault
        )
    )
    selected_provisioner = _ExpectedManagedBotProvisioner(
        delegate,
        expected_external_bot_id=event_identity.external_bot_id,
    )
    return await finalize_botfather_provisioning(
        actor=pending.actor,
        request_id=request.id,
        provisioner=selected_provisioner,
    )


__all__ = [
    "begin_telegram_managed_bot_onboarding",
    "complete_telegram_managed_bot_onboarding",
    "has_active_telegram_managed_bot",
]
