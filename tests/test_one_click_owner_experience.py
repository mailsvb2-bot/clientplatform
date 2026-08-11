from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from clientplatform.domain.ad_connections import AdConnectionStatus
from clientplatform.domain.bookings import BookingSlotStatus
from handlers import clientplatform_one_click_experience as one_click


class FakeState:
    def __init__(self, data=None) -> None:
        self.data = dict(data or {})
        self.state = None
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def set_data(self, data):
        self.data = dict(data)

    async def update_data(self, **values):
        self.data.update(values)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.cleared = True
        self.data.clear()


async def immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def outbound_message():
    return SimpleNamespace(answer=AsyncMock())


def callback(data: str, out):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        bot=SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="clientplatform_bot"))
        ),
        message=out,
    )


def slot(*, slot_id="slot-1", start="2026-08-20T09:00:00+00:00"):
    return SimpleNamespace(
        slot=SimpleNamespace(
            id=slot_id,
            status=BookingSlotStatus.OPEN,
            starts_at=start,
        ),
        offering_title="Консультация",
        local_start="20.08.2026 12:00",
    )


def connection(*, connection_id="connection-1", login="owner"):
    return SimpleNamespace(
        id=connection_id,
        external_login=login,
        status=AdConnectionStatus.ACTIVE,
    )


def campaign(
    *,
    campaign_id="6001",
    name="Основная кампания",
    state="ON",
    status="ACCEPTED",
):
    return SimpleNamespace(
        campaign_id=campaign_id,
        name=name,
        state=state,
        status=status,
    )


def publication_job(
    *,
    connection_id="connection-1",
    campaign_id="6001",
    regions=(47,),
):
    return SimpleNamespace(
        connection_id=connection_id,
        external_campaign_id=campaign_id,
        region_ids=regions,
    )


class OneClickOwnerExperienceTests(unittest.IsolatedAsyncioTestCase):
    def common_patches(self, out):
        return (
            patch.object(one_click.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(one_click.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(one_click.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(
                one_click.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(one_click.control, "_callback_message", return_value=out),
        )

    async def test_dashboard_has_one_primary_action_and_two_secondary_actions(self) -> None:
        out = outbound_message()
        snapshot = (
            "actor",
            SimpleNamespace(business=SimpleNamespace(name="Мой бизнес")),
            object(),
            [],
            [],
            [],
            [slot()],
        )
        with (
            patch.object(
                one_click.simple,
                "_business_snapshot",
                new=AsyncMock(return_value=snapshot),
            ),
            patch.object(one_click.control, "_uuid_token", side_effect=lambda value: value),
        ):
            await one_click.send_one_click_dashboard(
                out,
                user_id=101,
                business_id="business-1",
            )
        text = out.answer.await_args.args[0]
        markup = out.answer.await_args.kwargs["reply_markup"]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("одной кнопки", text)
        self.assertEqual(
            labels,
            ["🚀 Получить клиентов", "👥 Клиенты и запись", "⚙️ Ещё"],
        )

    async def test_no_open_slot_reduces_flow_to_one_required_next_action(self) -> None:
        out = outbound_message()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(one_click, "list_ad_connections", return_value=[]),
            patch.object(one_click.control, "list_booking_slots", return_value=[]),
            patch.object(one_click, "list_ad_publications", return_value=[]),
        ):
            await one_click.get_clients_one_click(cb, state)
        cb.answer.assert_awaited_once_with("Готовлю всё сам…")
        text = out.answer.await_args.args[0]
        markup = out.answer.await_args.kwargs["reply_markup"]
        self.assertIn("Свободного времени пока нет", text)
        self.assertEqual(markup.inline_keyboard[0][0].text, "➕ Открыть время")
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "cps:firstbook:business-1")

    async def test_one_connection_and_unready_campaign_falls_back_to_ready_share(self) -> None:
        out = outbound_message()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        current_slot = slot()
        promotion = SimpleNamespace(
            campaign=SimpleNamespace(
                source_token="source-1",
                creative=SimpleNamespace(
                    headline="Консультация",
                    primary_text="Есть свободное время.",
                    description="Запись онлайн",
                ),
            )
        )
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(one_click, "ad_connections_enabled", return_value=True),
            patch.object(one_click, "yandex_direct_provider_configured", return_value=True),
            patch.object(one_click, "list_ad_connections", return_value=[connection()]),
            patch.object(one_click.control, "list_booking_slots", return_value=[current_slot]),
            patch.object(one_click, "list_ad_publications", return_value=[]),
            patch.object(
                one_click,
                "list_yandex_direct_campaigns",
                return_value=[campaign(status="DRAFT")],
            ),
            patch.object(one_click, "create_slot_promotion", return_value=promotion),
        ):
            await one_click.get_clients_one_click(cb, state)
        text = out.answer.await_args.args[0]
        markup = out.answer.await_args.kwargs["reply_markup"]
        self.assertIn("Уже можно привлекать клиентов", text)
        self.assertIn("ждать оплату или модерацию", text)
        self.assertEqual(markup.inline_keyboard[0][0].text, "📨 Отправить объявление")
        self.assertTrue(str(markup.inline_keyboard[0][0].url).startswith("https://t.me/share/url?"))

    async def test_previous_safe_choices_make_direct_preparation_one_click(self) -> None:
        out = outbound_message()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        current_slot = slot()
        recent = publication_job(regions=(47,))
        promotion = SimpleNamespace(campaign=SimpleNamespace(id="promotion-1", source_token="source-1"))
        draft = SimpleNamespace(
            campaign_name="Основная кампания",
            job=SimpleNamespace(
                id="job-2",
                region_ids=(47,),
                title="Консультация",
                text="Свободное время для записи",
            ),
        )
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(one_click, "ad_connections_enabled", return_value=True),
            patch.object(one_click, "yandex_direct_provider_configured", return_value=True),
            patch.object(one_click, "list_ad_connections", return_value=[connection()]),
            patch.object(one_click.control, "list_booking_slots", return_value=[current_slot]),
            patch.object(one_click, "list_ad_publications", return_value=[recent]),
            patch.object(one_click, "list_yandex_direct_campaigns", return_value=[campaign()]),
            patch.object(one_click, "create_slot_promotion", return_value=promotion),
            patch.object(one_click, "create_ad_publication_draft", return_value=draft) as create_draft,
        ):
            await one_click.get_clients_one_click(cb, state)
        create_draft.assert_called_once()
        self.assertEqual(state.state, one_click.ad.AdConnectionState.confirming_publication)
        self.assertEqual(state.data["job_id"], "job-2")
        text = out.answer.await_args.args[0]
        self.assertIn("✅ Реклама подготовлена", text)
        self.assertIn("Ничего не запущено", text)
        markup = out.answer.await_args.kwargs["reply_markup"]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("🖼 Создать красивую картинку", labels)
        self.assertIn(one_click.ad._CONFIRM_DRAFT_LABEL, labels)

    async def test_first_direct_run_asks_only_for_missing_region(self) -> None:
        out = outbound_message()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(one_click, "ad_connections_enabled", return_value=True),
            patch.object(one_click, "yandex_direct_provider_configured", return_value=True),
            patch.object(one_click, "list_ad_connections", return_value=[connection()]),
            patch.object(one_click.control, "list_booking_slots", return_value=[slot()]),
            patch.object(one_click, "list_ad_publications", return_value=[]),
            patch.object(one_click, "list_yandex_direct_campaigns", return_value=[campaign()]),
        ):
            await one_click.get_clients_one_click(cb, state)
        self.assertEqual(state.state, one_click.OneClickOwnerState.waiting_region)
        text = out.answer.await_args.args[0]
        self.assertIn("Нужна только география", text)
        markup = out.answer.await_args.kwargs["reply_markup"]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("Нижний Новгород", labels)
        self.assertIn("Москва", labels)
        self.assertIn("Санкт-Петербург", labels)

    async def test_multiple_accounts_are_not_guessed_without_history(self) -> None:
        out = outbound_message()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(one_click, "ad_connections_enabled", return_value=True),
            patch.object(one_click, "yandex_direct_provider_configured", return_value=True),
            patch.object(
                one_click,
                "list_ad_connections",
                return_value=[
                    connection(connection_id="connection-1", login="one"),
                    connection(connection_id="connection-2", login="two"),
                ],
            ),
            patch.object(one_click.control, "list_booking_slots", return_value=[slot()]),
            patch.object(one_click, "list_ad_publications", return_value=[]),
        ):
            await one_click.get_clients_one_click(cb, state)
        self.assertEqual(state.state, one_click.OneClickOwnerState.selecting_connection)
        text = out.answer.await_args.args[0]
        self.assertIn("несколько рекламных кабинетов", text)
        markup = out.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].text, "Яндекс · one")
        self.assertEqual(markup.inline_keyboard[1][0].text, "Яндекс · two")

    async def test_more_menu_hides_advanced_actions_from_home(self) -> None:
        out = outbound_message()
        cb = callback("cpo:more:business-1", out)
        patches = self.common_patches(out)
        with patches[1], patches[3], patches[4]:
            await one_click.open_more(cb)
        markup = out.answer.await_args.kwargs["reply_markup"]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(
            labels,
            [
                "🧰 Услуги и расписание",
                "📣 Реклама и продвижение",
                "🤝 Партнёрства",
                "⚙️ Настройки",
                "🏠 В кабинет",
            ],
        )


if __name__ == "__main__":
    unittest.main()
