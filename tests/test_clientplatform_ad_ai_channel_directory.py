from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application import ad_channel_directory as directory
from clientplatform.application import native_member_interactions as native
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from services.messenger import reply_dispatcher


def _actor() -> TenantContext:
    return TenantContext(
        business_id="11111111-1111-1111-1111-111111111111",
        membership_id="22222222-2222-2222-2222-222222222222",
        user_id=101,
        role=PlatformRole.OWNER,
    )


def _commands(message) -> list[str]:
    return [button.command for row in message.rows for button in row]


def test_advertising_directory_exposes_truthful_channel_catalog() -> None:
    with (
        patch.object(directory, "ad_connections_enabled", return_value=True),
        patch.object(directory, "yandex_direct_provider_configured", return_value=True),
        patch.dict("os.environ", {}, clear=False),
    ):
        channels = directory.advertising_channels()

    assert [item.key for item in channels] == [
        "yandex_direct",
        "vk_ads",
        "telegram_ads",
        "other",
    ]
    assert channels[0].managed_ready is True
    assert channels[1].public_url == ""
    assert channels[2].public_url == "https://ads.telegram.org/"
    assert all(item.managed_ready is False for item in channels[1:])
    assert all("✅" not in item.description for item in channels[1:])


def test_native_ad_channel_screen_is_available_in_vk_and_max_contract() -> None:
    actor = _actor()
    with patch.object(native, "_active_yandex_connection", return_value=None):
        message = native._ad_channels_message(actor)

    commands = _commands(message)
    assert "cpm:ad-channel:yandex_direct" in commands
    assert "cpm:ad-channel:vk_ads" in commands
    assert "cpm:ad-channel:telegram_ads" in commands
    assert "cpm:ad-channel:other" in commands
    assert all(len(row) == 1 for row in message.rows)


def test_telegram_ads_detail_is_external_link_not_false_connection() -> None:
    actor = _actor()
    with patch.object(native, "_active_yandex_connection", return_value=None):
        message = native._ad_channel_message(actor, "telegram_ads")

    assert "связь с ClientPlatform пока не подтверждена" in message.text
    assert "cpm:ad-console:telegram_ads" in _commands(message)


def test_cross_messenger_renderer_resolves_ad_console_as_native_link() -> None:
    interaction = SimpleNamespace(
        rows=(
            (
                SimpleNamespace(
                    command="cpm:ad-console:telegram_ads",
                    label="Открыть Telegram Ads",
                ),
            ),
        ),
    )
    links = reply_dispatcher._clientplatform_runtime_button_links(
        interaction,
        business_id="11111111-1111-1111-1111-111111111111",
    )
    assert links == {
        "cpm:ad-console:telegram_ads": "https://ads.telegram.org/"
    }

    vk = reply_dispatcher._vk_clientplatform_keyboard(
        interaction,
        button_links=links,
        business_id="11111111-1111-1111-1111-111111111111",
    )
    assert '"type":"open_link"' in vk

    max_buttons = reply_dispatcher._max_clientplatform_attachments(
        interaction,
        button_links=links,
        business_id="11111111-1111-1111-1111-111111111111",
    )
    button = max_buttons[0]["payload"]["buttons"][0][0]
    assert button["type"] == "link"
    assert button["url"] == "https://ads.telegram.org/"
