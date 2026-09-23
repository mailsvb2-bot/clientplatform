from __future__ import annotations

import os
from dataclasses import dataclass

from clientplatform.application.ad_connections import (
    ad_connections_enabled,
    yandex_direct_provider_configured,
)


@dataclass(frozen=True, slots=True)
class AdvertisingChannel:
    key: str
    label: str
    connection_kind: str
    public_url: str
    description: str
    managed_ready: bool = False


def _https_env(name: str, default: str = "") -> str:
    value = str(os.getenv(name, default) or "").strip()
    return value if value.startswith("https://") else ""


def advertising_channels() -> tuple[AdvertisingChannel, ...]:
    return (
        AdvertisingChannel(
            key="yandex_direct",
            label="Яндекс Директ",
            connection_kind="managed_oauth",
            public_url="https://direct.yandex.ru/",
            description=(
                "Полноценное подключение рекламного кабинета через OAuth. "
                "ClientPlatform не получает пароль и не запускает расходы без отдельного согласия."
            ),
            managed_ready=ad_connections_enabled() and yandex_direct_provider_configured(),
        ),
        AdvertisingChannel(
            key="vk_ads",
            label="VK Реклама",
            connection_kind="external_connector",
            public_url=_https_env("CLIENTPLATFORM_VK_ADS_CONNECT_URL"),
            description=(
                "Рекламный канал выделен отдельно. Пока коннектор не подтверждён API-авторизацией, "
                "ClientPlatform не помечает кабинет как подключённый."
            ),
        ),
        AdvertisingChannel(
            key="telegram_ads",
            label="Telegram Ads",
            connection_kind="external_console",
            public_url="https://ads.telegram.org/",
            description=(
                "Официальный рекламный кабинет Telegram. Открытие кабинета не считается "
                "API-подключением к ClientPlatform."
            ),
        ),
        AdvertisingChannel(
            key="other",
            label="Другой рекламный сервис",
            connection_kind="connector_required",
            public_url=_https_env("CLIENTPLATFORM_OTHER_AD_CONNECT_URL"),
            description=(
                "Точка расширения для агентского кабинета, рекламной сети или другого провайдера. "
                "Подключение появится после настройки безопасной интеграции с этим сервисом."
            ),
        ),
    )


def advertising_channel(key: str) -> AdvertisingChannel:
    normalized = str(key or "").strip().lower()
    for channel in advertising_channels():
        if channel.key == normalized:
            return channel
    raise ValueError("unknown advertising channel")


__all__ = ["AdvertisingChannel", "advertising_channel", "advertising_channels"]
