from __future__ import annotations

import os
from contextlib import ExitStack
from unittest.mock import patch

from config.settings import settings
from scripts import clientplatform_messenger_channels_preflight as preflight


def _canonical_only_settings() -> ExitStack:
    stack = ExitStack()
    values = {
        "TELEGRAM_BOT_USERNAME": "",
        "MESSENGER_PUBLIC_BASE_URL": "https://client.example.test",
        "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL": "",
        "MAX_BOT_TOKEN": "",
        "MAX_WEBHOOK_SECRET": "",
        "MAX_BOT_NAME": "",
        "MAX_BOT_LINK_BASE": "",
        "VK_GROUP_ID": "",
        "VK_GROUP_TOKEN": "",
        "VK_CONFIRMATION_TOKEN": "",
        "VK_SECRET": "",
    }
    for name, value in values.items():
        stack.enter_context(patch.object(settings, name, value))
    return stack


def test_canonical_vk_max_preflight_does_not_require_telegram_control_bot() -> None:
    with patch.dict(
        os.environ,
        {
            "APP_ENV": "production",
            "TELEGRAM_TRANSPORT": "polling",
            "CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": "1",
            "CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED": "0",
            "MAX_WEBHOOK_ENABLED": "0",
            "VK_WEBHOOK_ENABLED": "0",
        },
        clear=False,
    ), _canonical_only_settings():
        inspected = preflight.inspect_messenger_channels()

    assert inspected.omnichannel_enabled
    assert inspected.omnichannel_ready
    assert inspected.webhook_runtime_ready
    assert "TELEGRAM_BOT_USERNAME" not in inspected.missing
    assert inspected.ok


def test_legacy_vk_flag_still_requires_legacy_vk_contract() -> None:
    with patch.dict(
        os.environ,
        {
            "APP_ENV": "production",
            "TELEGRAM_TRANSPORT": "polling",
            "CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": "1",
            "MAX_WEBHOOK_ENABLED": "0",
            "VK_WEBHOOK_ENABLED": "1",
        },
        clear=False,
    ), _canonical_only_settings():
        inspected = preflight.inspect_messenger_channels()

    assert "VK_GROUP_ID" in inspected.missing
    assert "VK_GROUP_TOKEN" in inspected.missing
    assert "VK_CONFIRMATION_TOKEN" in inspected.missing
    assert not inspected.ok
