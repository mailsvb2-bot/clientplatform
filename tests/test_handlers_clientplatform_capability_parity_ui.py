from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.application.capability_parity import (
    AdvertisingCapabilityProjection,
    BusinessCapabilityProjection,
    CapabilityAvailability,
    MessengerCapabilityProjection,
)
from clientplatform.domain.ad_connections import AdConnectionStatus
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import PlatformRole
from handlers import clientplatform_admin as admin
from handlers import clientplatform_simple_experience as simple


def _projection(
    *,
    telegram: CapabilityAvailability,
    vk: CapabilityAvailability,
    max_channel: CapabilityAvailability,
    yandex: CapabilityAvailability | None = None,
) -> BusinessCapabilityProjection:
    items = tuple(
        MessengerCapabilityProjection(
            platform=platform,
            availability=availability,
            connection_statuses=(),
            runtime_enabled=availability != CapabilityAvailability.UNAVAILABLE,
            runtime_ready=availability != CapabilityAvailability.UNAVAILABLE,
            setup_available=True,
        )
        for platform, availability in (
            (ConnectionPlatform.TELEGRAM, telegram),
            (ConnectionPlatform.VK, vk),
            (ConnectionPlatform.MAX, max_channel),
        )
    )
    ad = None
    if yandex is not None:
        ad = AdvertisingCapabilityProjection(
            availability=yandex,
            connection_statuses=(AdConnectionStatus.ACTIVE,) if yandex == CapabilityAvailability.ACTIVE else (),
            runtime_enabled=yandex != CapabilityAvailability.UNAVAILABLE,
        )
    return BusinessCapabilityProjection(messengers=items, yandex_direct=ad)


async def _direct_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append((text, kwargs))


class CapabilityParityUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_business_capabilities_screen_shows_runtime_truth(self) -> None:
        projection = _projection(
            telegram=CapabilityAvailability.ACTIVE,
            vk=CapabilityAvailability.UNAVAILABLE,
            max_channel=CapabilityAvailability.CONNECTED_UNAVAILABLE,
            yandex=CapabilityAvailability.ACTIVE,
        )
        actor = SimpleNamespace(role=PlatformRole.OWNER)
        message = FakeMessage()
        with (
            patch.object(simple.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(simple.control, "get_business_profile", return_value=SimpleNamespace(activity_description="Помогаю клиентам")),
            patch.object(simple.control, "list_business_capabilities", return_value=[SimpleNamespace(title="Консультации")]),
            patch.object(simple.control, "list_accessible_businesses", return_value=[SimpleNamespace(business=SimpleNamespace(id="business-1", name="Практика"))]),
            patch.object(simple, "get_business_capability_projection", return_value=projection),
            patch.object(simple.asyncio, "to_thread", side_effect=_direct_thread),
            patch.object(simple.control, "_uuid_token", return_value="business-token"),
            patch.object(simple, "_ADVANCED_KEYBOARD", side_effect=lambda *_args: SimpleNamespace(inline_keyboard=[])),
        ):
            await simple.send_advanced_dashboard(message, user_id=7, business_id="business-1")

        text, kwargs = message.answers[-1]
        self.assertIn("🧩 Бизнес и возможности", text)
        self.assertIn("Telegram — ✅ работает", text)
        self.assertIn("ВКонтакте — ⏸ сейчас недоступно", text)
        self.assertIn("MAX — ⏸ подключено, но сейчас выключено", text)
        self.assertIn("Яндекс Директ — ✅ работает", text)
        self.assertIn("Консультации", text)
        labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertEqual(labels[:2], ["💬 Мессенджеры", "📣 Реклама"])

    async def test_admin_hides_disabled_channels_and_blocks_forged_connect(self) -> None:
        projection = _projection(
            telegram=CapabilityAvailability.CONNECTABLE,
            vk=CapabilityAvailability.UNAVAILABLE,
            max_channel=CapabilityAvailability.UNAVAILABLE,
        )
        ctx = SimpleNamespace(
            role=PlatformRole.OWNER,
            actor=SimpleNamespace(),
            business_token="business-token",
            business_id="business-1",
        )
        safe_edit = AsyncMock()
        issue = AsyncMock()
        with (
            patch.object(admin.asyncio, "to_thread", side_effect=_direct_thread),
            patch.object(admin, "get_business_capability_projection", return_value=projection),
            patch.object(admin, "available_staff_messenger_switches", return_value=()),
            patch.object(admin, "_safe_edit", safe_edit),
            patch.object(admin, "_set_current_section", new=AsyncMock()),
            patch.object(admin, "issue_native_messenger_setup", issue),
        ):
            await admin._render_messengers(object(), object(), ctx)
            await admin._render_messenger_connect(object(), object(), ctx, "vk")

        first_text = safe_edit.await_args_list[0].args[1]
        first_markup = safe_edit.await_args_list[0].args[2]
        labels = [button.text for row in first_markup.inline_keyboard for button in row]
        self.assertIn("ВКонтакте: ⏸ сейчас недоступен", first_text)
        self.assertIn("MAX: ⏸ сейчас недоступен", first_text)
        self.assertIn("✈️ Подключить Telegram", labels)
        self.assertNotIn("🔵 Подключить ВКонтакте", labels)
        self.assertNotIn("🟣 Подключить MAX", labels)
        self.assertIn("сейчас нельзя подключить", safe_edit.await_args_list[1].args[1])
        issue.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
