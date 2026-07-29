from __future__ import annotations

from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from clientplatform.domain.managed_bot_owner import (
    ManagedBotWebhookMaterial,
    ManagedBotWebhookOperationFailed,
)
from clientplatform.runtime.secrets import (
    EnvironmentCredentialProvider,
    SecretReferenceError,
)


class ManagedBotWebhookController(Protocol):
    """Compatibility protocol for the managed bot remote transport boundary."""

    async def attach(self, material: ManagedBotWebhookMaterial) -> None: ...

    async def detach(self, material: ManagedBotWebhookMaterial) -> None: ...


class TelegramManagedBotWebhookController:
    """Prepare or stop a verified managed Telegram bot for long polling.

    The historical class name remains to avoid a broad API break. Both lifecycle
    operations enforce that no Telegram webhook exists; the polling gateway owns
    update consumption according to the local active/disabled/revoked state.
    """

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

    async def _remove_webhook(self, material: ManagedBotWebhookMaterial) -> None:
        token = self._resolve_token(material)
        bot = Bot(token=token)
        try:
            removed = await bot.delete_webhook(drop_pending_updates=False)
            if removed is not True:
                raise ManagedBotWebhookOperationFailed(
                    "Telegram did not confirm webhook removal for polling"
                )
        except ManagedBotWebhookOperationFailed:
            raise
        except TelegramAPIError:
            raise ManagedBotWebhookOperationFailed(
                "Telegram polling preparation failed"
            ) from None
        finally:
            await bot.session.close()

    async def attach(self, material: ManagedBotWebhookMaterial) -> None:
        token = self._resolve_token(material)
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
            removed = await bot.delete_webhook(drop_pending_updates=False)
            if removed is not True:
                raise ManagedBotWebhookOperationFailed(
                    "Telegram did not confirm webhook removal for polling"
                )
        except ManagedBotWebhookOperationFailed:
            raise
        except TelegramAPIError:
            raise ManagedBotWebhookOperationFailed(
                "Telegram polling activation failed"
            ) from None
        except (ValueError, TypeError, AttributeError):
            raise ManagedBotWebhookOperationFailed(
                "Telegram bot identity is invalid"
            ) from None
        finally:
            await bot.session.close()

    async def detach(self, material: ManagedBotWebhookMaterial) -> None:
        await self._remove_webhook(material)
