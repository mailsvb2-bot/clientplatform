from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from clientplatform.domain.ad_connections import AdConnectionStatus
from clientplatform.domain.bookings import BookingSlotStatus
from handlers import clientplatform_ad_connections as ads_ui


async def immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def callback(data: str):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def active_connection(login: str = "mailsvb2"):
    return SimpleNamespace(
        external_login=login,
        status=AdConnectionStatus.ACTIVE,
    )


def open_slot():
    return SimpleNamespace(
        local_start="10.08.2026 12:00",
        offering_title="Замена раковины",
        slot=SimpleNamespace(
            id="slot-id",
            status=BookingSlotStatus.OPEN,
        ),
    )


class AdConnectionWorkspaceUxTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_account_workspace_hides_reconnect_and_booking_slots(self) -> None:
        cb = callback("cpa:home:business-token")
        outbound = SimpleNamespace(answer=AsyncMock())
        booking_slots = Mock(return_value=[open_slot()])

        with (
            patch.object(ads_ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ads_ui, "ad_connections_enabled", return_value=True),
            patch.object(ads_ui, "yandex_direct_provider_configured", return_value=True),
            patch.object(ads_ui.control, "_token_uuid", return_value="business-id"),
            patch.object(
                ads_ui.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(
                ads_ui,
                "list_ad_connections",
                return_value=[active_connection()],
            ),
            patch.object(ads_ui, "list_ad_publications", return_value=[]),
            patch.object(ads_ui.control, "list_booking_slots", booking_slots),
            patch.object(ads_ui.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(ads_ui, "_message", return_value=outbound),
        ):
            await ads_ui._workspace(cb, business_token="business-token")

        booking_slots.assert_not_called()
        rendered = outbound.answer.await_args.args[0]
        rows = outbound.answer.await_args.kwargs["reply_markup"]
        labels = [label for row in rows for label, _callback in row]
        callbacks = [_callback for row in rows for _label, _callback in row]

        self.assertIn("Яндекс Директ · mailsvb2 · ✅ подключён", rendered)
        self.assertIn("Здесь только управление подключением", rendered)
        self.assertNotIn("Замена раковины", rendered)
        self.assertNotIn("свободное время для рекламного черновика", rendered)
        self.assertIn("🎯 Создать рекламу", labels)
        self.assertIn("cpa:promote:business-token", callbacks)
        self.assertNotIn("➕ Подключить Яндекс Директ", labels)

    async def test_disconnected_workspace_offers_connect_but_not_create_ad(self) -> None:
        cb = callback("cpa:home:business-token")
        outbound = SimpleNamespace(answer=AsyncMock())

        with (
            patch.object(ads_ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ads_ui, "ad_connections_enabled", return_value=True),
            patch.object(ads_ui, "yandex_direct_provider_configured", return_value=True),
            patch.object(ads_ui.control, "_token_uuid", return_value="business-id"),
            patch.object(
                ads_ui.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(ads_ui, "list_ad_connections", return_value=[]),
            patch.object(ads_ui, "list_ad_publications", return_value=[]),
            patch.object(ads_ui.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(ads_ui, "_message", return_value=outbound),
        ):
            await ads_ui._workspace(cb, business_token="business-token")

        rows = outbound.answer.await_args.kwargs["reply_markup"]
        labels = [label for row in rows for label, _callback in row]
        self.assertIn("➕ Подключить Яндекс Директ", labels)
        self.assertNotIn("🎯 Создать рекламу", labels)
        self.assertNotIn("🔌 Отключить кабинет", labels)

    async def test_create_ad_step_rejects_without_active_connection(self) -> None:
        cb = callback("cpa:promote:business-token")
        outbound = SimpleNamespace(answer=AsyncMock())

        with (
            patch.object(ads_ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ads_ui.control, "_token_uuid", return_value="business-id"),
            patch.object(
                ads_ui.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(ads_ui, "list_ad_connections", return_value=[]),
            patch.object(ads_ui.control, "list_booking_slots", return_value=[]),
            patch.object(ads_ui, "_message", return_value=outbound),
        ):
            await ads_ui.open_ad_promotion_slots(cb)

        cb.answer.assert_awaited_once_with(
            "Сначала подключите рекламный кабинет",
            show_alert=True,
        )
        outbound.answer.assert_not_awaited()

    async def test_create_ad_step_explains_when_no_open_slots(self) -> None:
        cb = callback("cpa:promote:business-token")
        outbound = SimpleNamespace(answer=AsyncMock())

        with (
            patch.object(ads_ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ads_ui.control, "_token_uuid", return_value="business-id"),
            patch.object(
                ads_ui.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(
                ads_ui,
                "list_ad_connections",
                return_value=[active_connection()],
            ),
            patch.object(ads_ui.control, "list_booking_slots", return_value=[]),
            patch.object(ads_ui.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(ads_ui, "_message", return_value=outbound),
        ):
            await ads_ui.open_ad_promotion_slots(cb)

        cb.answer.assert_awaited_once_with()
        rendered = outbound.answer.await_args.args[0]
        rows = outbound.answer.await_args.kwargs["reply_markup"]
        labels = [label for row in rows for label, _callback in row]

        self.assertIn("Сейчас нет свободного времени", rendered)
        self.assertIn("разделе «Запись»", rendered)
        self.assertEqual(labels, ["⬅️ К рекламному кабинету"])

    async def test_create_ad_step_lists_open_slots_only_after_explicit_action(self) -> None:
        cb = callback("cpa:promote:business-token")
        outbound = SimpleNamespace(answer=AsyncMock())
        slot = open_slot()

        with (
            patch.object(ads_ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ads_ui.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(ads_ui.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(
                ads_ui.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(
                ads_ui,
                "list_ad_connections",
                return_value=[active_connection()],
            ),
            patch.object(ads_ui.control, "list_booking_slots", return_value=[slot]),
            patch.object(ads_ui.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(ads_ui, "_message", return_value=outbound),
        ):
            await ads_ui.open_ad_promotion_slots(cb)

        rendered = outbound.answer.await_args.args[0]
        rows = outbound.answer.await_args.kwargs["reply_markup"]
        labels = [label for row in rows for label, _callback in row]
        callbacks = [_callback for row in rows for _label, _callback in row]

        self.assertIn("🎯 Создать рекламу", rendered)
        self.assertIn("Выберите, какое свободное время рекламировать", rendered)
        self.assertIn("🎯 10.08.2026 12:00 · Замена раковины", labels)
        self.assertIn("cpa:slot:business-token:slot-id", callbacks)
        self.assertIn("⬅️ К рекламному кабинету", labels)


if __name__ == "__main__":
    unittest.main()
