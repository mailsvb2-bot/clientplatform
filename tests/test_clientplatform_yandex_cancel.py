from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import clientplatform_yandex_screen_code as screen_code


class FakeState:
    def __init__(self) -> None:
        self.cleared = False

    async def clear(self) -> None:
        self.cleared = True


class YandexScreenCodeCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_clears_fsm_and_returns_to_ad_workspace(self) -> None:
        callback = SimpleNamespace(
            data="cpa:yandex-cancel:business-token",
            answer=AsyncMock(),
        )
        outbound = SimpleNamespace(answer=AsyncMock())
        state = FakeState()
        with (
            patch.object(screen_code, "_message", return_value=outbound),
            patch.object(
                screen_code.control,
                "_keyboard",
                side_effect=lambda rows: rows,
            ),
        ):
            await screen_code.cancel_yandex_direct_screen_code(callback, state)

        self.assertTrue(state.cleared)
        callback.answer.assert_awaited_once_with("Подключение отменено")
        self.assertIn("отменено", outbound.answer.await_args.args[0])
        self.assertEqual(
            outbound.answer.await_args.kwargs["reply_markup"],
            [[("Вернуться к рекламным кабинетам", "cpa:home:business-token")]],
        )


if __name__ == "__main__":
    unittest.main()
