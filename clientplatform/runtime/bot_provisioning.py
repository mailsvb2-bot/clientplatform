from __future__ import annotations

import logging
import os
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from clientplatform.domain.bot_provisioning import (
    BotProvisioningVerificationFailed,
    ManagedBotProvisioningRequest,
    VerifiedTelegramBot,
)
from clientplatform.runtime.secrets import (
    ClientPlatformCredentialProvider,
    CredentialProvider,
    SecretReferenceError,
)

log = logging.getLogger(__name__)


class ManagedBotProvisioner(Protocol):
    async def provision(
        self,
        request: ManagedBotProvisioningRequest,
    ) -> VerifiedTelegramBot: ...

    async def rollback(self, request: ManagedBotProvisioningRequest) -> None: ...


def _public_base_url(value: str | None = None) -> str:
    """Deprecated compatibility parser; Telegram no longer consumes this URL."""

    raw = str(
        value
        if value is not None
        else os.getenv("TELEGRAM_WEBHOOK_PUBLIC_BASE_URL", "")
    ).strip()
    return raw.rstrip("/") + "/" if raw else ""


def _gateway_path_prefix(value: str | None = None) -> str:
    """Deprecated compatibility parser for old records and runbooks."""

    raw = str(
        value
        if value is not None
        else os.getenv(
            "CLIENTPLATFORM_BOT_GATEWAY_PATH_PREFIX",
            "/clientplatform/managed-bots",
        )
    ).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.rstrip("/") or "/clientplatform/managed-bots"


class BotFatherTelegramProvisioner:
    """Verify a Telegram bot credential and prepare it for long polling.

    The historical class name remains for compatibility with the BotFather
    fallback. Its credential resolver now also supports encrypted tokens of bots
    created through Telegram Managed Bots.
    """

    def __init__(
        self,
        *,
        credential_provider: CredentialProvider | None = None,
        public_base_url: str | None = None,
        gateway_path_prefix: str | None = None,
    ) -> None:
        self._credential_provider = (
            credential_provider or ClientPlatformCredentialProvider()
        )
        # Accepted only so old composition code and tests do not break during the
        # transport migration. Neither value participates in Telegram ingress.
        self._public_base_url_value = public_base_url
        self._gateway_path_prefix_value = gateway_path_prefix

    async def provision(
        self,
        request: ManagedBotProvisioningRequest,
    ) -> VerifiedTelegramBot:
        if request.credential_reference is None:
            raise BotProvisioningVerificationFailed(
                "telegram bot credential reference is unavailable"
            )
        try:
            token = self._credential_provider.resolve(request.credential_reference)
        except SecretReferenceError:
            raise BotProvisioningVerificationFailed(
                "managed bot credential reference cannot be resolved"
            ) from None

        bot = Bot(token=token)
        try:
            identity = await bot.get_me()
            external_bot_id = str(identity.id)
            username = str(identity.username or "").strip().lower()
            if not username:
                raise BotProvisioningVerificationFailed(
                    "Telegram bot does not expose a username"
                )
            verified = VerifiedTelegramBot(
                external_bot_id=external_bot_id,
                username=username,
                display_name=" ".join(
                    part
                    for part in (
                        str(identity.first_name or "").strip(),
                        str(identity.last_name or "").strip(),
                    )
                    if part
                )
                or request.display_name,
            )
            removed = await bot.delete_webhook(drop_pending_updates=False)
            if removed is not True:
                raise BotProvisioningVerificationFailed(
                    "Telegram did not confirm webhook removal before polling"
                )
            return verified
        except BotProvisioningVerificationFailed:
            raise
        except TelegramAPIError:
            raise BotProvisioningVerificationFailed(
                "Telegram bot verification or polling preparation failed"
            ) from None
        except (ValueError, TypeError, AttributeError):
            raise BotProvisioningVerificationFailed(
                "Telegram bot identity is invalid"
            ) from None
        finally:
            await bot.session.close()

    async def rollback(self, request: ManagedBotProvisioningRequest) -> None:
        """Keep webhook disabled when a database commit fails.

        Polling itself has no remote registration to roll back. Deleting any
        stale webhook is idempotent and preserves the polling-only invariant.
        """

        if request.credential_reference is None:
            return
        try:
            token = self._credential_provider.resolve(request.credential_reference)
        except SecretReferenceError:
            log.exception(
                "Managed bot polling rollback could not resolve credential reference",
                extra={"provisioning_request_id": request.id},
            )
            return
        bot = Bot(token=token)
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except TelegramAPIError:
            log.exception(
                "Managed bot polling rollback could not confirm webhook removal",
                extra={"provisioning_request_id": request.id},
            )
        finally:
            await bot.session.close()
