from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.domain.ad_connections import AdConnectionError, AdConnectionStatus
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.promotions import PromotionError
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.integrations.yandex_direct import YandexDirectError
from handlers import clientplatform_one_click_experience as one_click


class FakeState:
    def __init__(self, data=None) -> None:
        self.data = dict(data or {})
        self.state = None
        self.cleared = 0

    async def get_data(self):
        return dict(self.data)

    async def set_data(self, data):
        self.data = dict(data)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.cleared += 1
        self.data.clear()


async def immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def out_message():
    return SimpleNamespace(answer=AsyncMock())


def cb(data: str, out=None, *, username="clientplatform_bot"):
    out = out or out_message()
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        bot=SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username=username))
        ),
        message=out,
    )


def msg(text: str, *, username="clientplatform_bot"):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        bot=SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username=username))
        ),
    )


def open_slot(slot_id="slot-1"):
    return SimpleNamespace(
        slot=SimpleNamespace(
            id=slot_id,
            status=BookingSlotStatus.OPEN,
            starts_at="2026-08-20T09:00:00+00:00",
        ),
        local_start="20.08.2026 12:00",
        offering_title="Консультация",
    )


def active_connection(connection_id="connection-1", login="owner"):
    return SimpleNamespace(
        id=connection_id,
        external_login=login,
        status=AdConnectionStatus.ACTIVE,
    )


def campaign(campaign_id="6001", name="Campaign", *, state="ON", status="ACCEPTED"):
    return SimpleNamespace(
        campaign_id=campaign_id,
        name=name,
        state=state,
        status=status,
    )


def promotion():
    return SimpleNamespace(
        campaign=SimpleNamespace(
            id="promotion-1",
            source_token="source-token-0001",
            creative=SimpleNamespace(
                headline="Консультация",
                primary_text="Свободное время",
                description="Запись онлайн",
            ),
        )
    )


class OneClickEdgeCoverageTests(unittest.IsolatedAsyncioTestCase):
    def base_patches(self, out):
        return (
            patch.object(one_click.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(one_click.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(one_click.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(one_click.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(one_click.control, "_callback_message", return_value=out),
        )

    async def test_small_helpers_cover_invalid_and_message_paths(self) -> None:
        self.assertEqual(one_click._indexed_choice({"x": ["a"]}, "x", "cpo:x:0"), "a")
        self.assertIsNone(one_click._indexed_choice({"x": ["a"]}, "x", "cpo:x:no"))
        self.assertIsNone(one_click._indexed_choice({"x": "bad"}, "x", "cpo:x:0"))
        self.assertIsNone(one_click._indexed_choice({"x": []}, "x", "cpo:x:2"))
        message = msg("hello")
        self.assertIs(one_click._target(message), message)
        self.assertEqual(one_click._user_id(message), 101)
        missing_user = SimpleNamespace(from_user=None)
        with self.assertRaises(ValueError):
            one_click._user_id(missing_user)
        self.assertEqual(await one_click._username(message), "clientplatform_bot")
        with self.assertRaises(RuntimeError):
            await one_click._username(msg("", username=""))

    async def test_dashboard_without_open_slots_uses_zero_status(self) -> None:
        out = out_message()
        snapshot = (
            "actor",
            SimpleNamespace(business=SimpleNamespace(name="Бизнес")),
            object(), [], [], [], [],
        )
        with (
            patch.object(one_click.simple, "_business_snapshot", new=AsyncMock(return_value=snapshot)),
            patch.object(one_click.control, "_uuid_token", side_effect=lambda value: value),
        ):
            await one_click.send_one_click_dashboard(out, user_id=101, business_id="business-1")
        self.assertIn("Свободного времени пока нет", out.answer.await_args.args[0])

    async def test_reload_slot_returns_current_open_slot_or_none(self) -> None:
        current = open_slot()
        closed = open_slot("closed")
        closed.slot.status = BookingSlotStatus.BOOKED
        with (
            patch.object(one_click.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(one_click.control, "list_booking_slots", return_value=[closed, current]),
        ):
            self.assertIs(await one_click._reload_slot("actor", "slot-1"), current)
            self.assertIsNone(await one_click._reload_slot("actor", "missing"))

    async def test_fallback_handles_stale_slot_and_promotion_failure(self) -> None:
        out = out_message()
        callback = cb("cpo:start:business-1", out)
        state = FakeState()
        with patch.object(one_click.control, "_callback_message", return_value=out):
            await one_click._fallback(
                callback, state, actor="actor", business_token="business-1",
                slot=None, reason="Yandex unavailable",
            )
        self.assertIn("Свободное время уже изменилось", out.answer.await_args.args[0])

        out.answer.reset_mock()
        state = FakeState()
        with (
            patch.object(one_click.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(one_click.control, "_callback_message", return_value=out),
            patch.object(one_click, "create_slot_promotion", side_effect=PromotionError("fail")),
        ):
            await one_click._fallback(
                callback, state, actor="actor", business_token="business-1",
                slot=open_slot(), reason="Yandex unavailable",
            )
        self.assertIn("Не удалось собрать запасной вариант", out.answer.await_args.args[0])

    async def test_fallback_handles_missing_bot_username(self) -> None:
        out = out_message()
        callback = cb("cpo:start:business-1", out, username="")
        state = FakeState()
        with (
            patch.object(one_click.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(one_click.control, "_callback_message", return_value=out),
            patch.object(one_click, "create_slot_promotion", return_value=promotion()),
        ):
            await one_click._fallback(
                callback, state, actor="actor", business_token="business-1",
                slot=open_slot(), reason="Yandex unavailable",
            )
        self.assertIn("Не удалось собрать запасной вариант", out.answer.await_args.args[0])

    async def test_prepare_draft_failure_boundaries_are_user_safe(self) -> None:
        data = {
            "business_id": "business-1",
            "business_token": "business-1",
            "slot_id": "slot-1",
            "connection_id": "connection-1",
            "external_campaign_id": "6001",
            "external_campaign_name": "Campaign",
        }
        for failing_step in ("promotion", "username", "draft"):
            with self.subTest(failing_step=failing_step):
                out = out_message()
                callback = cb("cpo:region:47", out, username="" if failing_step == "username" else "bot")
                state = FakeState(data)
                patches = [
                    patch.object(one_click.asyncio, "to_thread", new=immediate_to_thread),
                    patch.object(one_click.control, "_actor", new=AsyncMock(return_value="actor")),
                    patch.object(one_click.control, "_callback_message", return_value=out),
                ]
                if failing_step == "promotion":
                    extra = [patch.object(one_click, "create_slot_promotion", side_effect=PromotionError("fail"))]
                elif failing_step == "username":
                    extra = [patch.object(one_click, "create_slot_promotion", return_value=promotion())]
                else:
                    extra = [
                        patch.object(one_click, "create_slot_promotion", return_value=promotion()),
                        patch.object(one_click, "create_ad_publication_draft", side_effect=AdConnectionError("fail")),
                    ]
                with patches[0], patches[1], patches[2], *extra:
                    await one_click._prepare_draft(callback, state, data=data, region_ids=(47,))
                self.assertIn("Ничего не запущено", out.answer.await_args.args[0])

    async def test_choose_connection_provider_and_permission_errors_use_fallback(self) -> None:
        data = {
            "business_id": "business-1",
            "business_token": "business-1",
            "slot_id": "slot-1",
        }
        for error in (YandexDirectError("temporary", retryable=True), TenantPermissionDenied("owner only")):
            with self.subTest(error=type(error).__name__):
                callback = cb("cpo:connection:0")
                state = FakeState(data)
                with (
                    patch.object(one_click.asyncio, "to_thread", new=immediate_to_thread),
                    patch.object(one_click.control, "_actor", new=AsyncMock(return_value="actor")),
                    patch.object(one_click, "list_yandex_direct_campaigns", side_effect=error),
                    patch.object(one_click, "_reload_slot", new=AsyncMock(return_value=open_slot())),
                    patch.object(one_click, "_fallback", new=AsyncMock()) as fallback,
                ):
                    await one_click._choose_connection(
                        callback, state, data=data, connection_id="connection-1"
                    )
                fallback.assert_awaited_once()

    async def test_multiple_campaigns_require_one_choice(self) -> None:
        out = out_message()
        callback = cb("cpo:connection:0", out)
        state = FakeState()
        data = {
            "business_id": "business-1",
            "business_token": "business-1",
            "slot_id": "slot-1",
        }
        with (
            patch.object(one_click.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(one_click.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(one_click.control, "_callback_message", return_value=out),
            patch.object(one_click, "list_yandex_direct_campaigns", return_value=[campaign("1", "One"), campaign("2", "Two")]),
            patch.object(one_click, "list_ad_publications", return_value=[]),
        ):
            await one_click._choose_connection(callback, state, data=data, connection_id="connection-1")
        self.assertEqual(state.state, one_click.OneClickOwnerState.selecting_campaign)
        self.assertEqual(len(state.data["campaigns"]), 2)
        self.assertIn("несколько готовых кампаний", out.answer.await_args.args[0])

    async def test_no_connection_opens_oauth_and_oauth_failure_falls_back(self) -> None:
        for fail in (False, True):
            with self.subTest(fail=fail):
                out = out_message()
                callback = cb("cpo:start:business-1", out)
                state = FakeState()
                patches = self.base_patches(out)
                oauth_effect = AdConnectionError("no oauth") if fail else None
                oauth_result = None if fail else SimpleNamespace(authorization_url="https://oauth.example")
                with (
                    patches[0], patches[1], patches[2], patches[3], patches[4],
                    patch.object(one_click, "ad_connections_enabled", return_value=True),
                    patch.object(one_click, "yandex_direct_provider_configured", return_value=True),
                    patch.object(one_click.control, "list_booking_slots", return_value=[open_slot()]),
                    patch.object(one_click, "list_ad_connections", return_value=[]),
                    patch.object(one_click, "list_ad_publications", return_value=[]),
                    patch.object(one_click, "start_yandex_direct_oauth", side_effect=oauth_effect, return_value=oauth_result),
                    patch.object(one_click, "_fallback", new=AsyncMock()) as fallback,
                ):
                    await one_click.get_clients_one_click(callback, state)
                if fail:
                    fallback.assert_awaited_once()
                else:
                    fallback.assert_not_awaited()
                    self.assertIn("Один раз подключите Яндекс", out.answer.await_args.args[0])

    async def test_disabled_direct_goes_straight_to_fallback(self) -> None:
        out = out_message()
        callback = cb("cpo:start:business-1", out)
        state = FakeState()
        patches = self.base_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(one_click, "ad_connections_enabled", return_value=False),
            patch.object(one_click.control, "list_booking_slots", return_value=[open_slot()]),
            patch.object(one_click, "_fallback", new=AsyncMock()) as fallback,
        ):
            await one_click.get_clients_one_click(callback, state)
        fallback.assert_awaited_once()

    async def test_connection_and_campaign_callback_validation(self) -> None:
        out = out_message()
        state = FakeState({"connection_ids": ["c1"]})
        invalid = cb("cpo:connection:no", out)
        with patch.object(one_click, "_choose_connection", new=AsyncMock()) as choose:
            await one_click.choose_one_click_connection(invalid, state)
        choose.assert_not_awaited()
        invalid.answer.assert_awaited_once()

        valid = cb("cpo:connection:0", out)
        with patch.object(one_click, "_choose_connection", new=AsyncMock()) as choose:
            await one_click.choose_one_click_connection(valid, state)
        choose.assert_awaited_once()

        bad_campaign = cb("cpo:campaign:0", out)
        state = FakeState({"campaigns": [{"id": "", "name": ""}]})
        with patch.object(one_click, "_choose_campaign", new=AsyncMock()) as choose_campaign:
            await one_click.choose_one_click_campaign(bad_campaign, state)
        choose_campaign.assert_not_awaited()

        good_campaign = cb("cpo:campaign:0", out)
        state = FakeState({"campaigns": [{"id": "6001", "name": "Campaign"}]})
        with patch.object(one_click, "_choose_campaign", new=AsyncMock()) as choose_campaign:
            await one_click.choose_one_click_campaign(good_campaign, state)
        choose_campaign.assert_awaited_once()

    async def test_region_buttons_and_text_cover_fast_and_custom_paths(self) -> None:
        base = {
            "business_id": "business-1",
            "business_token": "business-1",
            "slot_id": "slot-1",
            "connection_id": "connection-1",
            "external_campaign_id": "6001",
            "external_campaign_name": "Campaign",
        }
        out = out_message()
        other = cb("cpo:region:other", out)
        with patch.object(one_click.control, "_callback_message", return_value=out):
            await one_click.choose_one_click_region(other, FakeState(base))
        self.assertIn("Напишите город", out.answer.await_args.args[0])

        invalid = cb("cpo:region:999", out)
        await one_click.choose_one_click_region(invalid, FakeState(base))
        invalid.answer.assert_awaited_once()

        fast = cb("cpo:region:47", out)
        with patch.object(one_click, "_prepare_draft", new=AsyncMock()) as prepare:
            await one_click.choose_one_click_region(fast, FakeState(base))
        prepare.assert_awaited_once()

        text_message = msg("Москва")
        with patch.object(one_click, "_prepare_draft", new=AsyncMock()) as prepare:
            await one_click.receive_one_click_region(text_message, FakeState(base))
        prepare.assert_awaited_once()

        numeric_message = msg("54, 55")
        with patch.object(one_click, "_prepare_draft", new=AsyncMock()) as prepare:
            await one_click.receive_one_click_region(numeric_message, FakeState(base))
        prepare.assert_awaited_once()

        bad_message = msg("не знаю")
        await one_click.receive_one_click_region(bad_message, FakeState(base))
        self.assertIn("Напишите город", bad_message.answer.await_args.args[0])

    async def test_secondary_menus_are_grouped(self) -> None:
        out = out_message()
        patches = self.base_patches(out)
        with patches[1], patches[3], patches[4]:
            await one_click.open_work_tools(cb("cpo:work:business-1", out))
        labels = [button.text for row in out.answer.await_args.kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertEqual(labels, ["🧰 Мои услуги", "📅 Мой календарь", "🔗 Моя страница", "⬅️ Назад"])

        out.answer.reset_mock()
        with patches[1], patches[3], patches[4]:
            await one_click.open_ad_tools(cb("cpo:ads:business-1", out))
        labels = [button.text for row in out.answer.await_args.kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn("🚀 Получить клиентов", labels)
        self.assertIn("📣 Яндекс Директ", labels)


if __name__ == "__main__":
    unittest.main()
