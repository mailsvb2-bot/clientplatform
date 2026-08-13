from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from clientplatform.domain.ad_publication_assets import AdPublicationAssetError
from handlers import clientplatform_goal_first_autopilot as goal


class TelegramBotStub:
    def __init__(self, *, file_size: int = 4, file_path: str = "media/file.jpg", payload: bytes = b"data") -> None:
        self.file_size = file_size
        self.file_path = file_path
        self.payload = payload
        self.get_file = AsyncMock(side_effect=self._get_file)
        self.download_file = AsyncMock(side_effect=self._download_file)

    async def _get_file(self, file_id: str):
        return SimpleNamespace(file_size=self.file_size, file_path=self.file_path)

    async def _download_file(self, file_path: str, *, destination, timeout: int):
        destination.write(self.payload)


def message(bot: TelegramBotStub):
    return SimpleNamespace(bot=bot)


class GoalFirstMediaDownloadSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_reported_oversize_before_provider_io(self):
        bot = TelegramBotStub()
        with self.assertRaises(AdPublicationAssetError):
            await goal._download_telegram_file(
                message(bot),
                file_id="file-1",
                reported_size=goal._MAX_TELEGRAM_MEDIA_BYTES + 1,
            )
        bot.get_file.assert_not_awaited()

    async def test_rejects_remote_oversize_and_missing_path(self):
        oversized = TelegramBotStub(file_size=goal._MAX_TELEGRAM_MEDIA_BYTES + 1)
        with self.assertRaises(AdPublicationAssetError):
            await goal._download_telegram_file(
                message(oversized), file_id="file-1", reported_size=0
            )
        oversized.download_file.assert_not_awaited()

        missing_path = TelegramBotStub(file_size=4, file_path="")
        with self.assertRaises(AdPublicationAssetError):
            await goal._download_telegram_file(
                message(missing_path), file_id="file-1", reported_size=4
            )
        missing_path.download_file.assert_not_awaited()

    async def test_rejects_empty_or_changed_download(self):
        empty = TelegramBotStub(file_size=0, payload=b"")
        with self.assertRaises(AdPublicationAssetError):
            await goal._download_telegram_file(
                message(empty), file_id="file-1", reported_size=0
            )

        changed = TelegramBotStub(file_size=4, payload=b"different")
        with self.assertRaises(AdPublicationAssetError):
            await goal._download_telegram_file(
                message(changed), file_id="file-1", reported_size=4
            )

    async def test_accepts_consistent_download_and_provider_size_change(self):
        exact = TelegramBotStub(file_size=4, payload=b"data")
        self.assertEqual(
            await goal._download_telegram_file(
                message(exact), file_id="file-1", reported_size=4
            ),
            b"data",
        )
        exact.download_file.assert_awaited_once()

        provider_remeasured = TelegramBotStub(file_size=8, payload=b"12345678")
        self.assertEqual(
            await goal._download_telegram_file(
                message(provider_remeasured), file_id="file-2", reported_size=4
            ),
            b"12345678",
        )


if __name__ == "__main__":
    unittest.main()
