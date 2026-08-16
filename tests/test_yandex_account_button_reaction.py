from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.integrations.yandex_direct import YandexDirectError
from handlers import clientplatform_ad_connections as ui


async def immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def callback() -> SimpleNamespace:
    return SimpleNamespace(
        data="cpa:conn:0",
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def state() -> SimpleNamespace:
    return SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                "business_id": "business-id",
                "business_token": "business-token",
                "connection_ids": ["connection-id"],
            }
        ),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
    )


def target_message() -> SimpleNamespace:
    return SimpleNamespace(answer=AsyncMock())


class YandexAccountButtonReactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_is_acknowledged_before_provider_io_and_failure_is_visible(self) -> None:
        cb = callback()
        st = state()
        target = target_message()

        def fail_provider(*, actor, connection_id):
            del actor, connection_id
            self.assertEqual(cb.answer.await_count, 1)
            raise YandexDirectError("provider_transport_unavailable", retryable=True)

        with (
            patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ui.control, "_actor", new=AsyncMock(return_value=object())),
            patch.object(ui, "list_yandex_direct_campaigns", side_effect=fail_provider),
            patch.object(ui, "_message", return_value=target),
            patch.object(ui.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            await ui.choose_yandex_campaign(cb, st)

        cb.answer.assert_awaited_once_with("Загружаю кампании…")
        target.answer.assert_awaited_once()
        self.assertIn("Не удалось получить кампании Яндекса", target.answer.await_args.args[0])
        callbacks = [value for row in target.answer.await_args.kwargs["reply_markup"] for _, value in row]
        self.assertIn("cpa:conn:0", callbacks)
        self.assertIn("cpa:home:business-token", callbacks)

    async def test_success_does_not_answer_same_callback_twice(self) -> None:
        cb = callback()
        st = state()
        target = target_message()
        campaign = SimpleNamespace(
            campaign_id="123",
            name="Test campaign",
            state="ON",
        )

        with (
            patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ui.control, "_actor", new=AsyncMock(return_value=object())),
            patch.object(ui, "list_yandex_direct_campaigns", return_value=[campaign]),
            patch.object(ui, "_message", return_value=target),
            patch.object(ui.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            await ui.choose_yandex_campaign(cb, st)

        cb.answer.assert_awaited_once_with("Загружаю кампании…")
        st.update_data.assert_awaited_once_with(
            connection_id="connection-id",
            publication_mode="existing",
            yandex_campaigns=[{"id": "123", "name": "Test campaign"}],
        )
        st.set_state.assert_awaited_once_with(ui.AdConnectionState.selecting_campaign)
        self.assertIn("В какой существующей кампании", target.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
