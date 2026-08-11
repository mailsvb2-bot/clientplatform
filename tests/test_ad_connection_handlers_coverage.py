from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from clientplatform.domain.ad_connections import (
    AdConnectionStatus,
    AdPublicationStatus,
)
from clientplatform.domain.bookings import BookingSlotStatus
from handlers import clientplatform_ad_connections as ad_handlers
from handlers import clientplatform_ad_disconnect as disconnect_handlers
from runtime import ad_oauth_http


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


class FakeRouter:
    def __init__(self) -> None:
        self.routes = []

    def add_get(self, path, handler):
        self.routes.append((path, handler))


class FakeWebApp(dict):
    def __init__(self) -> None:
        super().__init__()
        self.router = FakeRouter()


async def immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def callback(data: str = ""):
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="clientplatform_bot"))
    )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        bot=bot,
    )


def message(text: str = ""):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def outbound_message():
    return SimpleNamespace(answer=AsyncMock())


def connection(
    *,
    connection_id: str = "connection-1",
    login: str = "vasya",
    status=AdConnectionStatus.ACTIVE,
):
    return SimpleNamespace(
        id=connection_id,
        external_login=login,
        status=status,
    )


def campaign(*, campaign_id: str = "6001", name: str = "Рабочая кампания"):
    return SimpleNamespace(
        campaign_id=campaign_id,
        name=name,
        state="ON",
        status="ACCEPTED",
    )


class AdConnectionTelegramJourneyTests(unittest.IsolatedAsyncioTestCase):
    def common_patches(self, module, out):
        return (
            patch.object(module.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(module.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(module.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(module.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(module.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(module, "_message", return_value=out),
        )

    async def test_workspace_disabled_and_enabled_with_active_slot(self) -> None:
        out = outbound_message()
        cb = callback("cpa:home:business-1")
        patches = self.common_patches(ad_handlers, out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(ad_handlers, "ad_connections_enabled", return_value=False),
            patch.object(
                ad_handlers,
                "yandex_direct_provider_configured",
                return_value=True,
            ),
        ):
            await ad_handlers.open_ad_connections(cb)
        cb.answer.assert_awaited_once()
        self.assertIn("ещё не включено", out.answer.await_args.args[0])

        out.answer.reset_mock()
        cb.answer.reset_mock()
        slot = SimpleNamespace(
            slot=SimpleNamespace(id="slot-1", status=BookingSlotStatus.OPEN),
            local_start="10 августа, 12:00",
            offering_title="Замена раковины",
        )
        job = SimpleNamespace(
            external_campaign_name="Локальные услуги",
            external_campaign_id="6001",
            status=AdPublicationStatus.SUBMITTED,
        )
        patches = self.common_patches(ad_handlers, out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(ad_handlers, "ad_connections_enabled", return_value=True),
            patch.object(
                ad_handlers,
                "yandex_direct_provider_configured",
                return_value=True,
            ),
            patch.object(
                ad_handlers,
                "list_ad_connections",
                return_value=[connection()],
            ),
            patch.object(
                ad_handlers.control,
                "list_booking_slots",
                return_value=[slot],
            ),
            patch.object(ad_handlers, "list_ad_publications", return_value=[job]),
        ):
            await ad_handlers.open_ad_connections(cb)
        rendered = out.answer.await_args.args[0]
        self.assertIn("vasya", rendered)
        self.assertIn("Локальные услуги", rendered)
        self.assertIn("Выберите свободное время", rendered)
        rows = out.answer.await_args.kwargs["reply_markup"]
        self.assertTrue(any("Отключить кабинет" in item[0] for row in rows for item in row))

    async def test_workspace_without_connections_or_open_slots(self) -> None:
        out = outbound_message()
        cb = callback("cpa:home:business-1")
        closed_slot = SimpleNamespace(
            slot=SimpleNamespace(id="slot-1", status=BookingSlotStatus.BOOKED),
            local_start="10 августа, 12:00",
            offering_title="Услуга",
        )
        patches = self.common_patches(ad_handlers, out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(ad_handlers, "ad_connections_enabled", return_value=True),
            patch.object(
                ad_handlers,
                "yandex_direct_provider_configured",
                return_value=True,
            ),
            patch.object(ad_handlers, "list_ad_connections", return_value=[]),
            patch.object(
                ad_handlers.control,
                "list_booking_slots",
                return_value=[closed_slot],
            ),
            patch.object(ad_handlers, "list_ad_publications", return_value=[]),
        ):
            await ad_handlers._workspace(cb, business_token="business-1")
        rendered = out.answer.await_args.args[0]
        self.assertIn("кабинет пока не подключён", rendered)
        self.assertIn("Сначала опубликуйте", rendered)

    async def test_connect_success_and_sanitized_failure(self) -> None:
        out = outbound_message()
        cb = callback("cpa:connect:business-1")
        start = SimpleNamespace(
            authorization_url="https://oauth.yandex.ru/authorize?safe=1"
        )
        patches = self.common_patches(ad_handlers, out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(ad_handlers, "start_yandex_direct_oauth", return_value=start),
        ):
            await ad_handlers.connect_yandex_direct(cb)
        markup = out.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(
            markup.inline_keyboard[0][0].url,
            start.authorization_url,
        )

        cb.answer.reset_mock()
        out.answer.reset_mock()
        patches = self.common_patches(ad_handlers, out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                ad_handlers,
                "start_yandex_direct_oauth",
                side_effect=RuntimeError("secret provider detail"),
            ),
        ):
            await ad_handlers.connect_yandex_direct(cb)
        cb.answer.assert_awaited_once_with(
            "Не удалось начать подключение",
            show_alert=True,
        )
        out.answer.assert_not_awaited()

    async def test_choose_connection_rejects_empty_and_builds_source_link(self) -> None:
        out = outbound_message()
        cb = callback("cpa:slot:business-1:slot-1")
        state = FakeState()
        patches = self.common_patches(ad_handlers, out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(ad_handlers, "list_ad_connections", return_value=[]),
        ):
            await ad_handlers.choose_ad_connection(cb, state)
        cb.answer.assert_awaited_once_with(
            "Сначала подключите рекламный кабинет",
            show_alert=True,
        )

        cb.answer.reset_mock()
        view = SimpleNamespace(
            campaign=SimpleNamespace(id="promotion-1", source_token="source-1")
        )
        patches = self.common_patches(ad_handlers, out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                ad_handlers,
                "list_ad_connections",
                return_value=[connection()],
            ),
            patch.object(ad_handlers, "create_slot_promotion", return_value=view),
            patch.object(
                ad_handlers,
                "promotion_start_payload",
                return_value="promotion_source-1",
            ),
        ):
            await ad_handlers.choose_ad_connection(cb, state)
        self.assertEqual(state.state, ad_handlers.AdConnectionState.selecting_connection)
        self.assertEqual(state.data["connection_ids"], ["connection-1"])
        self.assertEqual(
            state.data["source_url"],
            "https://t.me/clientplatform_bot?start=promotion_source-1",
        )
        self.assertIn("Какой личный", out.answer.await_args.args[0])

    async def test_choose_connection_handles_missing_bot_username(self) -> None:
        out = outbound_message()
        cb = callback("cpa:slot:business-1:slot-1")
        cb.bot.get_me = AsyncMock(return_value=SimpleNamespace(username=""))
        state = FakeState()
        view = SimpleNamespace(
            campaign=SimpleNamespace(id="promotion-1", source_token="source-1")
        )
        patches = self.common_patches(ad_handlers, out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                ad_handlers,
                "list_ad_connections",
                return_value=[connection()],
            ),
            patch.object(ad_handlers, "create_slot_promotion", return_value=view),
        ):
            await ad_handlers.choose_ad_connection(cb, state)
        cb.answer.assert_awaited_once_with(
            "Не удалось подготовить объявление",
            show_alert=True,
        )

    async def test_campaign_selection_success_empty_and_invalid_callback(self) -> None:
        out = outbound_message()
        cb = callback("cpa:conn:0")
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "connection_ids": ["connection-1"],
            }
        )
        patches = self.common_patches(ad_handlers, out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                ad_handlers,
                "list_yandex_direct_campaigns",
                return_value=[campaign()],
            ),
        ):
            await ad_handlers.choose_yandex_campaign(cb, state)
        cb.answer.assert_awaited_once_with("Загружаю кампании…")
        self.assertEqual(state.state, ad_handlers.AdConnectionState.selecting_campaign)
        self.assertEqual(state.data["connection_id"], "connection-1")
        self.assertEqual(state.data["yandex_campaigns"][0]["id"], "6001")

        cb.answer.reset_mock()
        out.answer.reset_mock()
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "connection_ids": ["connection-1"],
            }
        )
        patches = self.common_patches(ad_handlers, out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(ad_handlers, "list_yandex_direct_campaigns", return_value=[]),
        ):
            await ad_handlers.choose_yandex_campaign(cb, state)
        cb.answer.assert_awaited_once_with("Загружаю кампании…")
        self.assertIn(
            "В кабинете нет подходящей активной текстовой кампании",
            out.answer.await_args.args[0],
        )

        out.answer.reset_mock()
        cb = callback("cpa:conn:not-a-number")
        patches = self.common_patches(ad_handlers, out)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await ad_handlers.choose_yandex_campaign(cb, state)
        cb.answer.assert_awaited_once_with("Загружаю кампании…")
        self.assertIn(
            "Не удалось получить кампании Яндекса",
            out.answer.await_args.args[0],
        )

    async def test_region_request_and_publication_preview(self) -> None:
        out = outbound_message()
        cb = callback("cpa:campaign:0")
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "promotion_campaign_id": "promotion-1",
                "connection_id": "connection-1",
                "source_url": "https://t.me/bot?start=source",
                "yandex_campaigns": [{"id": "6001", "name": "Кампания"}],
            }
        )
        patches = self.common_patches(ad_handlers, out)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await ad_handlers.request_ad_regions(cb, state)
        self.assertEqual(state.state, ad_handlers.AdConnectionState.waiting_regions)
        self.assertEqual(state.data["external_campaign_id"], "6001")
        self.assertIn("Нижний Новгород", out.answer.await_args.args[0])

        bad_cb = callback("cpa:campaign:8")
        bad_state = FakeState({"yandex_campaigns": []})
        patches = self.common_patches(ad_handlers, out)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await ad_handlers.request_ad_regions(bad_cb, bad_state)
        bad_cb.answer.assert_awaited_once_with(
            "Кампания больше не найдена",
            show_alert=True,
        )

        msg = message("47, 213")
        job = SimpleNamespace(
            id="job-1",
            region_ids=(47, 213),
            title="Замена раковины",
            text="Запишитесь онлайн",
            source_url="https://t.me/bot?start=source",
        )
        draft = SimpleNamespace(job=job, campaign_name="Кампания")
        state.data.update(
            {
                "external_campaign_name": "Кампания",
                "external_campaign_id": "6001",
            }
        )
        with (
            patch.object(ad_handlers.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ad_handlers.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(ad_handlers.control, "_user_id", return_value=101),
            patch.object(ad_handlers.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(ad_handlers, "create_ad_publication_draft", return_value=draft),
        ):
            await ad_handlers.prepare_ad_publication(msg, state)
        self.assertEqual(state.state, ad_handlers.AdConnectionState.confirming_publication)
        self.assertEqual(state.data["job_id"], "job-1")
        rendered = msg.answer.await_args.args[0]
        self.assertIn("Проверьте рекламный черновик", rendered)
        self.assertIn("DRAFT", rendered)
        self.assertIn("расходов автоматически не будет", rendered)

        invalid = message("не регион")
        with patch.object(ad_handlers.control, "_user_id", return_value=101):
            await ad_handlers.prepare_ad_publication(invalid, FakeState({}))
        self.assertIn("Не удалось распознать регион", invalid.answer.await_args.args[0])

    async def test_confirmation_success_and_missing_state_failure(self) -> None:
        out = outbound_message()
        cb = callback("cpa:confirm")
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "job_id": "job-1",
            }
        )
        queued = SimpleNamespace(status=AdPublicationStatus.QUEUED)
        patches = self.common_patches(ad_handlers, out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(ad_handlers, "confirm_ad_publication", return_value=queued),
        ):
            await ad_handlers.confirm_yandex_publication(cb, state)
        self.assertTrue(state.cleared)
        cb.answer.assert_awaited_once_with("Черновик принят")
        self.assertIn("защищённую очередь", out.answer.await_args.args[0])

        failed_cb = callback("cpa:confirm")
        patches = self.common_patches(ad_handlers, out)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await ad_handlers.confirm_yandex_publication(failed_cb, FakeState({}))
        failed_cb.answer.assert_awaited_once_with(
            "Не удалось поставить черновик в очередь",
            show_alert=True,
        )


class AdConnectionDisconnectJourneyTests(unittest.IsolatedAsyncioTestCase):
    def common_patches(self, out):
        return (
            patch.object(
                disconnect_handlers.asyncio,
                "to_thread",
                new=immediate_to_thread,
            ),
            patch.object(
                disconnect_handlers.control,
                "_token_uuid",
                side_effect=lambda value: value,
            ),
            patch.object(
                disconnect_handlers.control,
                "_uuid_token",
                side_effect=lambda value: value,
            ),
            patch.object(
                disconnect_handlers.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(
                disconnect_handlers.control,
                "_keyboard",
                side_effect=lambda rows: rows,
            ),
            patch.object(disconnect_handlers, "_message", return_value=out),
        )

    async def test_list_disconnectable_success_empty_and_error(self) -> None:
        out = outbound_message()
        cb = callback("cpa:disconnects:business-1")
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                disconnect_handlers,
                "list_ad_connections",
                return_value=[
                    connection(),
                    connection(
                        connection_id="revoked",
                        status=AdConnectionStatus.REVOKED,
                    ),
                ],
            ),
        ):
            await disconnect_handlers.list_disconnectable_ad_connections(cb)
        self.assertIn("Выберите кабинет", out.answer.await_args.args[0])
        rows = out.answer.await_args.kwargs["reply_markup"]
        self.assertTrue(any("vasya" in item[0] for row in rows for item in row))

        out.answer.reset_mock()
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(disconnect_handlers, "list_ad_connections", return_value=[]),
        ):
            await disconnect_handlers.list_disconnectable_ad_connections(cb)
        self.assertIn("Активных подключений нет", out.answer.await_args.args[0])

        failed = callback("cpa:disconnects:business-1")
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                disconnect_handlers,
                "list_ad_connections",
                side_effect=RuntimeError("database unavailable"),
            ),
        ):
            await disconnect_handlers.list_disconnectable_ad_connections(failed)
        failed.answer.assert_awaited_once_with(
            "Не удалось открыть подключения",
            show_alert=True,
        )

    async def test_disconnect_confirmation_success_missing_and_error(self) -> None:
        out = outbound_message()
        cb = callback("cpa:disconnect:business-1:connection-1")
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                disconnect_handlers,
                "list_ad_connections",
                return_value=[connection()],
            ),
        ):
            await disconnect_handlers.confirm_ad_connection_disconnect(cb)
        self.assertIn("Отключить Яндекс", out.answer.await_args.args[0])
        self.assertIn("vasya", out.answer.await_args.args[0])

        missing = callback("cpa:disconnect:business-1:unknown")
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                disconnect_handlers,
                "list_ad_connections",
                return_value=[connection()],
            ),
        ):
            await disconnect_handlers.confirm_ad_connection_disconnect(missing)
        missing.answer.assert_awaited_once_with(
            "Кабинет не найден",
            show_alert=True,
        )

        failed = callback("cpa:disconnect:business-1:connection-1")
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                disconnect_handlers,
                "list_ad_connections",
                side_effect=ValueError("bad row"),
            ),
        ):
            await disconnect_handlers.confirm_ad_connection_disconnect(failed)
        failed.answer.assert_awaited_once_with(
            "Не удалось проверить кабинет",
            show_alert=True,
        )

    async def test_revoke_success_and_sanitized_failure(self) -> None:
        out = outbound_message()
        cb = callback("cpa:revoke:business-1:connection-1")
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                disconnect_handlers,
                "disconnect_ad_connection",
                return_value=connection(),
            ),
        ):
            await disconnect_handlers.revoke_ad_connection(cb)
        cb.answer.assert_awaited_once_with("Доступ удалён")
        self.assertIn("кабинет отключён", out.answer.await_args.args[0])

        failed = callback("cpa:revoke:business-1:connection-1")
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                disconnect_handlers,
                "disconnect_ad_connection",
                side_effect=RuntimeError("secret token detail"),
            ),
        ):
            await disconnect_handlers.revoke_ad_connection(failed)
        failed.answer.assert_awaited_once_with(
            "Не удалось отключить кабинет",
            show_alert=True,
        )
        out.answer.assert_awaited_once()


class AdOAuthHttpJourneyTests(unittest.IsolatedAsyncioTestCase):
    async def test_enablement_and_exact_route_registration(self) -> None:
        app = FakeWebApp()
        bot = object()
        with (
            patch.object(ad_oauth_http, "ad_connections_enabled", return_value=False),
            patch.object(
                ad_oauth_http,
                "yandex_direct_provider_configured",
                return_value=True,
            ),
        ):
            self.assertFalse(ad_oauth_http.ad_oauth_http_enabled())

        with (
            patch.object(ad_oauth_http, "ad_connections_enabled", return_value=True),
            patch.object(
                ad_oauth_http,
                "yandex_direct_provider_configured",
                return_value=True,
            ),
        ):
            self.assertTrue(ad_oauth_http.ad_oauth_http_enabled())
            ad_oauth_http.register_ad_oauth_routes(app, bot=bot)
        self.assertIs(app["clientplatform_ad_oauth_bot"], bot)
        self.assertEqual(
            app.router.routes,
            [
                (
                    "/oauth/yandex-direct/callback",
                    ad_oauth_http.yandex_direct_oauth_callback,
                )
            ],
        )

    async def test_cancelled_and_malformed_callbacks(self) -> None:
        cancelled = SimpleNamespace(
            query={"error": "access_denied"},
            app={},
        )
        response = await ad_oauth_http.yandex_direct_oauth_callback(cancelled)
        self.assertEqual(response.status, 400)
        self.assertIn("Подключение отменено", response.text)

        malformed = SimpleNamespace(query={"state": "only-state"}, app={})
        response = await ad_oauth_http.yandex_direct_oauth_callback(malformed)
        self.assertEqual(response.status, 400)
        self.assertIn("Некорректный ответ", response.text)

    async def test_provider_failure_is_sanitized(self) -> None:
        request = SimpleNamespace(
            query={"state": "state", "code": "code"},
            app={},
        )
        with (
            patch.object(ad_oauth_http.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(
                ad_oauth_http,
                "complete_yandex_direct_oauth",
                side_effect=RuntimeError("secret provider payload"),
            ),
        ):
            response = await ad_oauth_http.yandex_direct_oauth_callback(request)
        self.assertEqual(response.status, 400)
        self.assertIn("Не удалось подключить", response.text)
        self.assertNotIn("secret", response.text)

    async def test_success_notifies_telegram_and_handles_notification_failure(self) -> None:
        completion = SimpleNamespace(
            user_id=101,
            connection=SimpleNamespace(external_login="vasya"),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        request = SimpleNamespace(
            query={"state": "state", "code": "code"},
            app={"clientplatform_ad_oauth_bot": bot},
        )
        with (
            patch.object(ad_oauth_http.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(
                ad_oauth_http,
                "complete_yandex_direct_oauth",
                return_value=completion,
            ),
        ):
            response = await ad_oauth_http.yandex_direct_oauth_callback(request)
        self.assertEqual(response.status, 200)
        self.assertIn("Кабинет подключён", response.text)
        bot.send_message.assert_awaited_once()
        self.assertIn("vasya", bot.send_message.await_args.args[1])

        bot.send_message = AsyncMock(side_effect=OSError("telegram unavailable"))
        with (
            patch.object(ad_oauth_http.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(
                ad_oauth_http,
                "complete_yandex_direct_oauth",
                return_value=completion,
            ),
        ):
            response = await ad_oauth_http.yandex_direct_oauth_callback(request)
        self.assertEqual(response.status, 200)
        self.assertIn("успешно подключён", response.text)

        no_bot_request = SimpleNamespace(
            query={"state": "state", "code": "code"},
            app={},
        )
        with (
            patch.object(ad_oauth_http.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(
                ad_oauth_http,
                "complete_yandex_direct_oauth",
                return_value=completion,
            ),
        ):
            response = await ad_oauth_http.yandex_direct_oauth_callback(no_bot_request)
        self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
