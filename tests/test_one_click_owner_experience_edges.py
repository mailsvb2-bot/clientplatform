from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.domain.ad_connections import AdConnectionError, AdConnectionStatus
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.promotions import PromotionError
from handlers import clientplatform_one_click_experience as one_click


class State:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.state = None
        self.clear_count = 0

    async def get_data(self):
        return dict(self.data)

    async def set_data(self, data):
        self.data = dict(data)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.clear_count += 1
        self.data.clear()


async def direct(function, *args, **kwargs):
    return function(*args, **kwargs)


def out():
    return SimpleNamespace(answer=AsyncMock())


def callback(data: str, target=None, username="clientplatform_bot"):
    target = target or out()
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        message=target,
        bot=SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username=username))
        ),
    )


def message(text: str, username="clientplatform_bot"):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        bot=SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username=username))
        ),
    )


def slot(slot_id="slot-1", status=BookingSlotStatus.OPEN):
    return SimpleNamespace(
        slot=SimpleNamespace(
            id=slot_id,
            status=status,
            starts_at="2026-08-20T09:00:00+00:00",
        ),
        local_start="20.08.2026 12:00",
        offering_title="Консультация",
    )


def connection(connection_id="connection-1", login="owner"):
    return SimpleNamespace(
        id=connection_id,
        external_login=login,
        status=AdConnectionStatus.ACTIVE,
    )


def publication(connection_id="connection-1", regions=(47,)):
    return SimpleNamespace(
        connection_id=connection_id,
        external_campaign_id="historical-provider-id",
        region_ids=regions,
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


def managed_draft():
    return SimpleNamespace(
        campaign_name="ClientPlatform managed",
        job=SimpleNamespace(
            id="job-1",
            external_campaign_id="managed-7001",
            title="Консультация",
            text="Свободное время",
        ),
    )


def base_data():
    return {
        "business_id": "business-1",
        "business_token": "business-1",
        "slot_id": "slot-1",
        "connection_id": "connection-1",
    }


class OneClickEdgeCoverageTests(unittest.IsolatedAsyncioTestCase):
    def common(self, target):
        return (
            patch.object(one_click.asyncio, "to_thread", new=direct),
            patch.object(one_click.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(one_click.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(one_click.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(one_click.control, "_callback_message", return_value=target),
        )

    async def test_helper_invalid_branches_and_missing_username(self):
        self.assertEqual(one_click._indexed_choice({"v": ["x"]}, "v", "cpo:x:0"), "x")
        self.assertIsNone(one_click._indexed_choice({"v": ["x"]}, "v", "cpo:x:no"))
        self.assertIsNone(one_click._indexed_choice({"v": "x"}, "v", "cpo:x:0"))
        self.assertIsNone(one_click._indexed_choice({"v": []}, "v", "cpo:x:1"))
        msg = message("hi")
        self.assertEqual(one_click._user_id(msg), 101)
        with self.assertRaises(ValueError):
            one_click._user_id(SimpleNamespace(from_user=None))
        self.assertEqual(await one_click._username(msg), "clientplatform_bot")
        with self.assertRaises(RuntimeError):
            await one_click._username(message("", username=""))

    async def test_dashboard_zero_open_slots_branch(self):
        target = out()
        snapshot = (
            "actor",
            SimpleNamespace(business=SimpleNamespace(name="Бизнес")),
            object(), [], [], [], [],
        )
        with (
            patch.object(one_click.simple, "_business_snapshot", new=AsyncMock(return_value=snapshot)),
            patch.object(one_click.control, "_uuid_token", side_effect=lambda value: value),
        ):
            await one_click.send_one_click_dashboard(target, user_id=101, business_id="business-1")
        self.assertIn("Свободного времени пока нет", target.answer.await_args.args[0])

    async def test_reload_slot_open_and_missing(self):
        current = slot()
        closed = slot("closed", BookingSlotStatus.BOOKED)
        with (
            patch.object(one_click.asyncio, "to_thread", new=direct),
            patch.object(one_click.control, "list_booking_slots", return_value=[closed, current]),
        ):
            self.assertIs(await one_click._reload_slot("actor", "slot-1"), current)
            self.assertIsNone(await one_click._reload_slot("actor", "missing"))

    async def test_fallback_stale_slot(self):
        target = out()
        state = State()
        with patch.object(one_click.control, "_callback_message", return_value=target):
            await one_click._fallback(
                callback("cpo:start:business-1", target),
                state,
                actor="actor",
                business_token="business-1",
                slot=None,
                reason="provider down",
            )
        self.assertIn("Свободное время уже изменилось", target.answer.await_args.args[0])
        self.assertEqual(state.clear_count, 1)

    async def test_fallback_promotion_and_username_failures(self):
        target = out()
        with (
            patch.object(one_click.asyncio, "to_thread", new=direct),
            patch.object(one_click.control, "_callback_message", return_value=target),
            patch.object(one_click, "create_slot_promotion", side_effect=PromotionError("fail")),
        ):
            await one_click._fallback(
                callback("cpo:start:business-1", target),
                State(), actor="actor", business_token="business-1",
                slot=slot(), reason="provider down",
            )
        self.assertIn("Не удалось собрать запасной вариант", target.answer.await_args.args[0])

        target.answer.reset_mock()
        with (
            patch.object(one_click.asyncio, "to_thread", new=direct),
            patch.object(one_click.control, "_callback_message", return_value=target),
            patch.object(one_click, "create_slot_promotion", return_value=promotion()),
        ):
            await one_click._fallback(
                callback("cpo:start:business-1", target, username=""),
                State(), actor="actor", business_token="business-1",
                slot=slot(), reason="provider down",
            )
        self.assertIn("Не удалось собрать запасной вариант", target.answer.await_args.args[0])

    async def test_prepare_draft_promotion_failure(self):
        target = out()
        data = base_data()
        with (
            patch.object(one_click.asyncio, "to_thread", new=direct),
            patch.object(one_click.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(one_click.control, "_callback_message", return_value=target),
            patch.object(one_click, "create_slot_promotion", side_effect=PromotionError("fail")),
        ):
            await one_click._prepare_draft(
                callback("cpo:region:47", target), State(data), data=data, region_ids=(47,)
            )
        self.assertIn("Ничего не запущено", target.answer.await_args.args[0])

    async def test_prepare_draft_username_and_managed_draft_failures(self):
        data = base_data()
        target = out()
        with (
            patch.object(one_click.asyncio, "to_thread", new=direct),
            patch.object(one_click.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(one_click.control, "_callback_message", return_value=target),
            patch.object(one_click, "create_slot_promotion", return_value=promotion()),
        ):
            await one_click._prepare_draft(
                callback("cpo:region:47", target, username=""),
                State(data), data=data, region_ids=(47,)
            )
        self.assertIn("Ничего не запущено", target.answer.await_args.args[0])

        target.answer.reset_mock()
        with (
            patch.object(one_click.asyncio, "to_thread", new=direct),
            patch.object(one_click.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(one_click.control, "_callback_message", return_value=target),
            patch.object(one_click, "create_slot_promotion", return_value=promotion()),
            patch.object(one_click, "create_managed_ad_publication_draft", side_effect=AdConnectionError("fail")),
        ):
            await one_click._prepare_draft(
                callback("cpo:region:47", target), State(data), data=data, region_ids=(47,)
            )
        self.assertIn("Ничего не запущено", target.answer.await_args.args[0])

    async def test_choose_connection_provider_history_failure_asks_region_safely(self):
        target = out()
        data = base_data()
        with (
            patch.object(one_click.asyncio, "to_thread", new=direct),
            patch.object(one_click.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(one_click.control, "_callback_message", return_value=target),
            patch.object(one_click, "list_ad_publications", side_effect=AdConnectionError("down")),
        ):
            state = State(data)
            await one_click._choose_connection(
                callback("cpo:connection:0", target), state,
                data=data, connection_id="connection-1",
            )
        self.assertEqual(state.state, one_click.OneClickOwnerState.waiting_region)
        self.assertIn("указать регион", target.answer.await_args.args[0].lower())

    async def test_choose_connection_reuses_matching_account_region(self):
        data = base_data()
        prepare = AsyncMock()
        with (
            patch.object(one_click.asyncio, "to_thread", new=direct),
            patch.object(one_click.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(
                one_click,
                "list_ad_publications",
                return_value=[publication("other", (2,)), publication("connection-1", (47,))],
            ),
            patch.object(one_click, "_prepare_draft", new=prepare),
        ):
            await one_click._choose_connection(
                callback("cpo:connection:0"), State(data),
                data=data, connection_id="connection-1",
            )
        prepare.assert_awaited_once()
        self.assertEqual(prepare.await_args.kwargs["region_ids"], (47,))

    async def test_no_connection_oauth_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                target = out()
                common = self.common(target)
                fallback = AsyncMock()
                oauth = SimpleNamespace(authorization_url="https://oauth.example")
                side_effect = AdConnectionError("no oauth") if fail else None
                with (
                    common[0], common[1], common[2], common[3], common[4],
                    patch.object(one_click, "ad_connections_enabled", return_value=True),
                    patch.object(one_click, "yandex_direct_provider_configured", return_value=True),
                    patch.object(one_click.control, "list_booking_slots", return_value=[slot()]),
                    patch.object(one_click, "list_ad_connections", return_value=[]),
                    patch.object(one_click, "list_ad_publications", return_value=[]),
                    patch.object(one_click, "start_yandex_direct_oauth", return_value=oauth, side_effect=side_effect),
                    patch.object(one_click, "_fallback", new=fallback),
                ):
                    await one_click.get_clients_one_click(callback("cpo:start:business-1", target), State())
                if fail:
                    fallback.assert_awaited_once()
                else:
                    fallback.assert_not_awaited()
                    self.assertIn("Один раз подключите Яндекс", target.answer.await_args.args[0])

    async def test_direct_disabled_and_connection_error_go_to_fallback(self):
        target = out()
        common = self.common(target)
        fallback = AsyncMock()
        with (
            common[0], common[1], common[2], common[3], common[4],
            patch.object(one_click, "ad_connections_enabled", return_value=False),
            patch.object(one_click.control, "list_booking_slots", return_value=[slot()]),
            patch.object(one_click, "_fallback", new=fallback),
        ):
            await one_click.get_clients_one_click(callback("cpo:start:business-1", target), State())
        fallback.assert_awaited_once()

        fallback.reset_mock()
        with (
            common[0], common[1], common[2], common[3], common[4],
            patch.object(one_click, "ad_connections_enabled", return_value=True),
            patch.object(one_click, "yandex_direct_provider_configured", return_value=True),
            patch.object(one_click.control, "list_booking_slots", return_value=[slot()]),
            patch.object(one_click, "list_ad_connections", side_effect=AdConnectionError("db")),
            patch.object(one_click, "list_ad_publications", return_value=[]),
            patch.object(one_click, "_fallback", new=fallback),
        ):
            await one_click.get_clients_one_click(callback("cpo:start:business-1", target), State())
        fallback.assert_awaited_once()

    async def test_connection_callback_selection_valid_and_stale(self):
        target = out()
        choose = AsyncMock()
        with patch.object(one_click, "_choose_connection", new=choose):
            await one_click.choose_one_click_connection(
                callback("cpo:connection:no", target), State({"connection_ids": ["c1"]})
            )
        choose.assert_not_awaited()

        choose = AsyncMock()
        with patch.object(one_click, "_choose_connection", new=choose):
            await one_click.choose_one_click_connection(
                callback("cpo:connection:0", target), State({"connection_ids": ["c1"]})
            )
        choose.assert_awaited_once()
        self.assertFalse(hasattr(one_click, "choose_one_click_campaign"))

    async def test_region_fast_other_numeric_and_invalid_text(self):
        data = base_data()
        target = out()
        with patch.object(one_click.control, "_callback_message", return_value=target):
            await one_click.choose_one_click_region(callback("cpo:region:other", target), State(data))
        self.assertIn("Напишите город", target.answer.await_args.args[0])

        invalid = callback("cpo:region:999", target)
        await one_click.choose_one_click_region(invalid, State(data))
        invalid.answer.assert_awaited_once()

        prepare = AsyncMock()
        with patch.object(one_click, "_prepare_draft", new=prepare):
            await one_click.choose_one_click_region(callback("cpo:region:47", target), State(data))
        prepare.assert_awaited_once()

        prepare = AsyncMock()
        with patch.object(one_click, "_prepare_draft", new=prepare):
            await one_click.receive_one_click_region(message("Москва"), State(data))
            await one_click.receive_one_click_region(message("54,55"), State(data))
        self.assertEqual(prepare.await_count, 2)

        bad_message = message("не знаю")
        await one_click.receive_one_click_region(bad_message, State(data))
        self.assertIn("Напишите город", bad_message.answer.await_args.args[0])

    async def test_secondary_work_and_ad_menus(self):
        target = out()
        common = self.common(target)
        with common[1], common[3], common[4]:
            await one_click.open_work_tools(callback("cpo:work:business-1", target))
        labels = [
            button.text
            for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(
            labels,
            ["🧰 Мои услуги", "📅 Мой календарь", "🔗 Моя страница", "⬅️ Назад"],
        )

        target.answer.reset_mock()
        with common[1], common[3], common[4]:
            await one_click.open_ad_tools(callback("cpo:ads:business-1", target))
        labels = [
            button.text
            for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("🚀 Найти новых клиентов", labels)
        self.assertNotIn("🚀 Получить клиентов", labels)
        self.assertIn("📣 Яндекс Директ", labels)


if __name__ == "__main__":
    unittest.main()
