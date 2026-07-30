from __future__ import annotations

import unittest
from typing import Any

from clientplatform.domain.connections import (
    ClaimedDispatch,
    ConnectionPlatform,
    Dispatch,
    DispatchStatus,
)
from clientplatform.domain.program_media import (
    is_voice_media_reference,
    mark_voice_media_reference,
)
from clientplatform.domain.programs import ContentKind
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from clientplatform.transport.media import HmacMediaGatewayResolver
from clientplatform.transport.telegram import TelegramDispatchAdapter
from clientplatform.transport.telegram_http import AiohttpTelegramBotClient


VOICE_REFERENCE = (
    "s3://clientplatform-production/program-media/scope/audio/aa/voice-object.ogg"
)
AUDIO_REFERENCE = (
    "s3://clientplatform-production/program-media/scope/audio/aa/music-object.mp3"
)


def _claimed(reference: str) -> ClaimedDispatch:
    timestamp = "2026-07-30T00:00:00+00:00"
    return ClaimedDispatch(
        dispatch=Dispatch(
            id="11111111-1111-4111-8111-111111111111",
            business_id="22222222-2222-4222-8222-222222222222",
            platform=ConnectionPlatform.TELEGRAM,
            logical_delivery_id="33333333-3333-4333-8333-333333333333",
            connection_id="44444444-4444-4444-8444-444444444444",
            customer_identity_id="55555555-5555-4555-8555-555555555555",
            payload_kind=ContentKind.AUDIO,
            payload_ref=reference,
            idempotency_key="voice:test",
            status=DispatchStatus.SENDING,
            attempts=0,
            available_at=timestamp,
            created_at=timestamp,
            updated_at=timestamp,
            locked_at=timestamp,
            lock_token="lease-token",
        ),
        external_subject="700001",
        credential_reference="secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_BUSINESS",
    )


class VoiceReferenceTests(unittest.TestCase):
    def test_only_private_program_ogg_is_voice(self) -> None:
        self.assertTrue(is_voice_media_reference(VOICE_REFERENCE))
        self.assertEqual(mark_voice_media_reference(VOICE_REFERENCE), VOICE_REFERENCE)
        for reference in (
            AUDIO_REFERENCE,
            "s3://clientplatform-production/other/voice.ogg",
            "https://cdn.example/voice.ogg",
            f"{VOICE_REFERENCE}?download=1",
        ):
            with self.subTest(reference=reference):
                self.assertFalse(is_voice_media_reference(reference))


class VoiceDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_reference_resolves_and_calls_send_voice_only(self) -> None:
        calls: list[tuple[str, str]] = []

        class Client:
            async def send_voice(self, *, token: str, chat_id: str, voice: str) -> str:
                self_token = token
                del self_token, chat_id
                calls.append(("voice", voice))
                return "9001"

            async def send_audio(self, **_kwargs: Any) -> str:
                calls.append(("audio", ""))
                return "9002"

            async def send_video(self, **_kwargs: Any) -> str:
                raise AssertionError("unexpected sendVideo")

            async def send_document(self, **_kwargs: Any) -> str:
                raise AssertionError("unexpected sendDocument")

            async def send_photo(self, **_kwargs: Any) -> str:
                raise AssertionError("unexpected sendPhoto")

            async def send_message(self, **_kwargs: Any) -> str:
                raise AssertionError("unexpected sendMessage")

        resolver = HmacMediaGatewayResolver(
            base_url="https://client.example/clientplatform",
            credential_provider=EnvironmentCredentialProvider(
                {"CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY": "media-secret"}
            ),
            signing_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
            ),
            ttl_seconds=300,
            clock=lambda: 1000,
        )
        adapter = TelegramDispatchAdapter(Client(), media_resolver=resolver)

        self.assertEqual(
            await adapter.send(_claimed(VOICE_REFERENCE), "business-bot-token"),
            "9001",
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "voice")
        self.assertTrue(calls[0][1].startswith("https://client.example/clientplatform/media/"))

    async def test_mp3_audio_still_calls_send_audio(self) -> None:
        calls: list[str] = []

        class Client:
            async def send_voice(self, **_kwargs: Any) -> str:
                calls.append("voice")
                return "1"

            async def send_audio(self, **_kwargs: Any) -> str:
                calls.append("audio")
                return "2"

            async def send_video(self, **_kwargs: Any) -> str:
                raise AssertionError

            async def send_document(self, **_kwargs: Any) -> str:
                raise AssertionError

            async def send_photo(self, **_kwargs: Any) -> str:
                raise AssertionError

            async def send_message(self, **_kwargs: Any) -> str:
                raise AssertionError

        resolver = HmacMediaGatewayResolver(
            base_url="https://client.example/clientplatform",
            credential_provider=EnvironmentCredentialProvider(
                {"CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY": "media-secret"}
            ),
            signing_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
            ),
            clock=lambda: 1000,
        )
        adapter = TelegramDispatchAdapter(Client(), media_resolver=resolver)
        self.assertEqual(
            await adapter.send(_claimed(AUDIO_REFERENCE), "business-bot-token"),
            "2",
        )
        self.assertEqual(calls, ["audio"])

    async def test_http_client_uses_send_voice_multipart_field(self) -> None:
        calls: list[tuple[str, str]] = []

        async def post_multipart(
            url: str,
            field_name: str,
            _chat_id: str,
            _media_url: str,
            _timeout: float,
            _max_bytes: int,
        ) -> tuple[int, Any]:
            calls.append((url, field_name))
            return 200, {"ok": True, "result": {"message_id": 77}}

        async def post_json(*_args: Any) -> tuple[int, Any]:
            raise AssertionError("private voice must not use JSON URL-send")

        client = AiohttpTelegramBotClient(
            post_json=post_json,
            post_multipart=post_multipart,
            multipart_media_base_url="https://client.example/clientplatform",
        )
        signed = (
            "https://client.example/clientplatform/media/clientplatform-production/"
            "program-media/scope/audio/aa/voice-object.ogg?expires=1&sig=x"
        )
        self.assertEqual(
            await client.send_voice(
                token="business-bot-token",
                chat_id="42",
                voice=signed,
            ),
            "77",
        )
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].endswith("/sendVoice"))
        self.assertEqual(calls[0][1], "voice")


if __name__ == "__main__":
    unittest.main()
