from __future__ import annotations

import unittest

from a1.transport.telegram_http import (
    AiohttpTelegramBotClient,
    TelegramBotApiError,
)


class A1TelegramMediaReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_storage_reference_is_not_sent_as_public_media(self) -> None:
        calls = 0

        async def post_json(_url, _payload, _timeout_seconds):
            nonlocal calls
            calls += 1
            return 200, {"ok": True, "result": {"message_id": 1}}

        client = AiohttpTelegramBotClient(post_json=post_json)
        with self.assertRaises(TelegramBotApiError) as raised:
            await client.send_audio(
                token="1:TOKEN",
                chat_id="42",
                audio="s3://private-bucket/audio.mp3",
            )
        self.assertEqual(
            raised.exception.code,
            "telegram_media_reference_unresolved",
        )
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
