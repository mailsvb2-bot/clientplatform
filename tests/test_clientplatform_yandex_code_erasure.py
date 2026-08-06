from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers import clientplatform_yandex_screen_code as screen_code


class YandexConfirmationCodeErasureTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_delete_method_is_tolerated(self) -> None:
        await screen_code._erase_confirmation_code_message(SimpleNamespace())

    async def test_transport_delete_failure_is_tolerated(self) -> None:
        message = SimpleNamespace(delete=AsyncMock(side_effect=OSError("unavailable")))
        await screen_code._erase_confirmation_code_message(message)
        message.delete.assert_awaited_once()

    async def test_code_message_is_deleted_when_telegram_allows_it(self) -> None:
        message = SimpleNamespace(delete=AsyncMock())
        await screen_code._erase_confirmation_code_message(message)
        message.delete.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
