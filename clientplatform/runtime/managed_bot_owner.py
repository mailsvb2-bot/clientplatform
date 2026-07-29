from __future__ import annotations

from typing import Protocol
from urllib.parse import urljoin

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from clientplatform.domain.bot_provisioning import BotProvisioningVerificationFailed
from clientplatform.domain.managed_bot_owner import (
    ManagedBotWebhookMaterial,
    ManagedBotWebhookOperationFailed,
)
from clientplatform.runtime.bot_provisioning import (
    _gateway_path_prefix,
    _public_base_url,
)
from clientplatform.runtime.secrets import (
    EnvironmentCredentialProvider,
    SecretReferenceError,
)


class ManagedBotWebhookController(Protocol):
    async def attach(self, material: ManagedBotWebhookMaterial) -> None: ...

    async def detach(self, material: ManagedBotWebhookMaterial) -> None: ...


class TelegramManagedBotWebhookController:
    """Synchronize a verified managed bot with the tokenless gateway URL."""

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
        self._public_base_url_value = public_base_url
        self._gateway_path_prefix_value = gateway_path_prefix

    def _resolve_token(self, material: ManagedBotWebhookMaterial) -> str:
        try:
            return self._credential_provider.resolve(material.credential_reference)
        except SecretReferenceError:
            raise ManagedBotWebhookOperationFailed(
                "managed bot credential reference cannot be resolved"
            ) from None

    def _resolve_webhook_secret(self, material: ManagedBotWebhookMaterial) -> str:
        try:
            return self._credential_provider.resolve(
                material.webhook_secret_reference
            )
        except SecretReferenceError:
            raise ManagedBotWebhookOperationFailed(
                "managed bot webhook reference cannot be resolved"
            ) from None

    def _webhook_url(self, material: ManagedBotWebhookMaterial) -> str:
        try:
            public_base_url = _public_base_url(self._public_base_url_value)
            gateway_path_prefix = _gateway_path_prefix(
                self._gateway_path_prefix_value
            )
        except BotProvisioningVerificationFailed:
            raise ManagedBotWebhookOperationFailed(
                "managed bot webhook route is not configured safely"
            ) from None
        return urljoin(
            public_base_url,
            (
                f"/{gateway_path_prefix.lstrip('/')}/telegram/"
                f"{material.external_bot_id}"
            ),
        )

    async def attach(self, material: ManagedBotWebhookMaterial) -> None:
        token = self._resolve_token(material)
        webhook_secret = self._resolve_webhook_secret(material)
        webhook_url = self._webhook_url(material)
        bot = Bot(token=token)
        try:
            identity = await bot.get_me()
            if str(identity.id) != material.external_bot_id:
                raise ManagedBotWebhookOperationFailed(
                    "Telegram bot identity no longer matches the managed route"
                )
            observed_username = str(identity.username or "").strip().lower()
            expected_username = str(material.username or "").strip().lower()
            if expected_username and observed_username != expected_username:
                raise ManagedBotWebhookOperationFailed(
                    "Telegram bot username no longer matches the managed route"
                )
            configured = await bot.set_webhook(
                url=webhook_url,
                secret_token=webhook_secret,
                drop_pending_updates=False,
            )
            if configured is not True:
                raise ManagedBotWebhookOperationFailed(
                    "Telegram did not confirm webhook configuration"
                )
        except ManagedBotWebhookOperationFailed:
            raise
        except TelegramAPIError:
            raise ManagedBotWebhookOperationFailed(
                "Telegram webhook configuration failed"
            ) from None
        except (ValueError, TypeError, AttributeError):
            raise ManagedBotWebhookOperationFailed(
                "Telegram bot identity is invalid"
            ) from None
        finally:
            await bot.session.close()

    async def detach(self, material: ManagedBotWebhookMaterial) -> None:
        token = self._resolve_token(material)
        bot = Bot(token=token)
        try:
            removed = await bot.delete_webhook(drop_pending_updates=False)
            if removed is not True:
                raise ManagedBotWebhookOperationFailed(
                    "Telegram did not confirm webhook removal"
                )
        except ManagedBotWebhookOperationFailed:
            raise
        except TelegramAPIError:
            raise ManagedBotWebhookOperationFailed(
                "Telegram webhook removal failed"
            ) from None
        finally:
            await bot.session.close()
