from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from handlers import clientplatform_yandex_screen_code as screen_code


class FakeState:
    def __init__(self, data=None) -> None:
        self.data = dict(data or {})
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def clear(self) -> None:
        self.cleared = True
        self.data.clear()


async def immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


class YandexScreenCodeCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_consumes_oauth_session_and_returns_to_workspace(self) -> None:
        callback = SimpleNamespace(
            data="cpa:yandex-cancel:business-token",
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        outbound = SimpleNamespace(answer=AsyncMock())
        state = FakeState(
            {
                "business_token": "business-token",
                "oauth_state": "s" * 43,
                "oauth_user_id": 101,
            }
        )
        with (
            patch.object(screen_code.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(screen_code, "_message", return_value=outbound),
            patch.object(screen_code.control, "_token_uuid", return_value="business-id"),
            patch.object(
                screen_code.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(
                screen_code.control,
                "_keyboard",
                side_effect=lambda rows: rows,
            ),
            patch.object(
                screen_code,
                "cancel_yandex_direct_oauth",
                new=Mock(return_value=True),
            ) as cancel,
        ):
            await screen_code.cancel_yandex_direct_screen_code(callback, state)

        cancel.assert_called_once_with(actor="actor", state="s" * 43)
        self.assertTrue(state.cleared)
        callback.answer.assert_awaited_once_with("Подключение отменено")
        self.assertIn("OAuth-сессия закрыта", outbound.answer.await_args.args[0])
        self.assertEqual(
            outbound.answer.await_args.kwargs["reply_markup"],
            [[("Вернуться к рекламным кабинетам", "cpa:home:business-token")]],
        )

    async def test_cancel_failure_keeps_fsm_for_safe_retry(self) -> None:
        callback = SimpleNamespace(
            data="cpa:yandex-cancel:business-token",
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        state = FakeState(
            {
                "business_token": "business-token",
                "oauth_state": "s" * 43,
                "oauth_user_id": 101,
            }
        )
        with (
            patch.object(screen_code.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(screen_code.control, "_token_uuid", return_value="business-id"),
            patch.object(
                screen_code.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(
                screen_code,
                "cancel_yandex_direct_oauth",
                side_effect=RuntimeError("database unavailable"),
            ),
        ):
            await screen_code.cancel_yandex_direct_screen_code(callback, state)

        self.assertFalse(state.cleared)
        callback.answer.assert_awaited_once_with(
            "Не удалось отменить подключение",
            show_alert=True,
        )

    async def test_cancel_rejects_mismatched_business_before_storage_call(self) -> None:
        callback = SimpleNamespace(
            data="cpa:yandex-cancel:other-business",
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        state = FakeState(
            {
                "business_token": "business-token",
                "oauth_state": "s" * 43,
                "oauth_user_id": 101,
            }
        )
        with patch.object(screen_code, "cancel_yandex_direct_oauth") as cancel:
            await screen_code.cancel_yandex_direct_screen_code(callback, state)

        cancel.assert_not_called()
        self.assertFalse(state.cleared)
        callback.answer.assert_awaited_once_with(
            "Не удалось отменить подключение",
            show_alert=True,
        )


if __name__ == "__main__":
    unittest.main()
