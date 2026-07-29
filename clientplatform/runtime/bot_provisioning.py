from __future__ import annotations

import logging
import os
from typing import Protocol
from urllib.parse import urljoin

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from clientplatform.domain.bot_provisioning import (
    BotProvisioningVerificationFailed,
    ManagedBotProvisioningRequest,
    VerifiedTelegramBot,
)
from clientplatform.runtime.secrets import (
    EnvironmentCredentialProvider,
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
    raw = str(
        value
        if value is not None
        else os.getenv("TELEGRAM_WEBHOOK_PUBLIC_BASE_URL", "")
    ).strip()
    if not raw:
        raise BotProvisioningVerificationFailed(
            "telegram webhook public base URL is not configured"
        )
    if not raw.startswith("https://"):
        raise BotProvisioningVerificationFailed(
            "telegram webhook public base URL must use HTTPS"
        )
    return raw.rstrip("/") + "/"


def _gateway_path_prefix(value: str | None = None) -> str:
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
    raw = raw.rstrip("/")
    if not raw or "token" in raw.lower() or "secret" in raw.lower():
        raise BotProvisioningVerificationFailed(
            "managed bot gateway path prefix is unsafe"
        )
    return raw


class BotFatherTelegramProvisioner:
    """Verify an existing BotFather bot and configure its tokenless gateway URL."""

    def __init__(
        self,
        *,
        credential_provider: EnvironmentCredentialProvider | None = None,
        public_base_url: str | None = None,
        gateway_path_prefix: str | None = None,
    ) -> None:
        self._credential_provider = (
            credential_provider or EnvironmentCredentialProvider()
        )
        self._public_base_url = _public_base_url(public_base_url)
        self._gateway_path_prefix = _gateway_path_prefix(gateway_path_prefix)

    async def provision(
        self,
        request: ManagedBotProvisioningRequest,
    ) -> VerifiedTelegramBot:
        if request.credential_reference is None:
            raise BotProvisioningVerificationFailed(
                "telegram bot credential reference is unavailable"
            )
        if request.webhook_secret_reference is None:
            raise BotProvisioningVerificationFailed(
                "telegram webhook secret reference is unavailable"
            )
        try:
            token = self._credential_provider.resolve(request.credential_reference)
            webhook_secret = self._credential_provider.resolve(
                request.webhook_secret_reference
            )
        except SecretReferenceError:
            raise BotProvisioningVerificationFailed(
                "managed bot secret reference cannot be resolved"
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
            webhook_url = urljoin(
                self._public_base_url,
                (
                    f"/{self._gateway_path_prefix.lstrip('/')}/telegram/"
                    f"{verified.external_bot_id}"
                ),
            )
            configured = await bot.set_webhook(
                url=webhook_url,
                secret_token=webhook_secret,
                drop_pending_updates=False,
            )
            if configured is not True:
                raise BotProvisioningVerificationFailed(
                    "Telegram did not confirm webhook configuration"
                )
            return verified
        except BotProvisioningVerificationFailed:
            raise
        except (TelegramAPIError, ValueError, TypeError, AttributeError):
            raise BotProvisioningVerificationFailed(
                "Telegram bot verification or webhook configuration failed"
            ) from None
        finally:
            await bot.session.close()

    async def rollback(self, request: ManagedBotProvisioningRequest) -> None:
        if request.credential_reference is None:
            return
        try:
            token = self._credential_provider.resolve(request.credential_reference)
        except SecretReferenceError:
            log.exception(
                "Managed bot provisioning rollback could not resolve credential reference",
                extra={"provisioning_request_id": request.id},
            )
            return
        bot = Bot(token=token)
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except TelegramAPIError:
            log.exception(
                "Managed bot provisioning webhook rollback failed",
                extra={"provisioning_request_id": request.id},
            )
        finally:
            await bot.session.close()
