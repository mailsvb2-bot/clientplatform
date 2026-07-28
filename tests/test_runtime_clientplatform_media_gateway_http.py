from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from clientplatform.domain.connections import (
    ClaimedDispatch,
    ConnectionPlatform,
    Dispatch,
    DispatchStatus,
)
from clientplatform.domain.programs import ContentKind
from clientplatform.runtime.media_gateway import (
    FilesystemMediaObjectStore,
    MediaGatewayConfig,
    start_media_gateway_runtime,
    stop_media_gateway_runtime,
)
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from clientplatform.transport.media import HmacMediaGatewayResolver, media_gateway_signature
from clientplatform.transport.telegram import TelegramDispatchAdapter
from clientplatform.transport.telegram_http import AiohttpTelegramBotClient


NOW = "2026-07-28T00:00:00+00:00"


def _claimed() -> ClaimedDispatch:
    return ClaimedDispatch(
        dispatch=Dispatch(
            id="11111111-1111-4111-8111-111111111111",
            business_id="22222222-2222-4222-8222-222222222222",
            platform=ConnectionPlatform.TELEGRAM,
            logical_delivery_id="33333333-3333-4333-8333-333333333333",
            connection_id="44444444-4444-4444-8444-444444444444",
            customer_identity_id="55555555-5555-4555-8555-555555555555",
            payload_kind=ContentKind.AUDIO,
            payload_ref="s3://private-bucket/program/audio.mp3",
            idempotency_key="delivery:media-gateway-e2e",
            status=DispatchStatus.SENDING,
            attempts=0,
            available_at=NOW,
            created_at=NOW,
            updated_at=NOW,
            locked_at=NOW,
            lock_token="lease-token",
        ),
        external_subject="700001",
        credential_reference="secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_MAIN",
    )


class ClientPlatformMediaGatewayHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        target = self.root / "private-bucket" / "program" / "audio.mp3"
        target.parent.mkdir(parents=True)
        self.media_content = b"clientplatform-AUDIO-CONTENT"
        target.write_bytes(self.media_content)
        self.secret = "media-gateway-test-secret"
        self.provider = EnvironmentCredentialProvider(
            {
                "CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY": self.secret,
                "CLIENTPLATFORM_SECRET_TELEGRAM_MAIN": "telegram-test-token",
            }
        )
        self.config = MediaGatewayConfig(
            enabled=True,
            host="127.0.0.1",
            port=0,
            public_base_url="https://media.example.test/clientplatform",
            storage_mode="filesystem",
            allowed_buckets=frozenset({"private-bucket"}),
            filesystem_root=str(self.root),
            s3_endpoint="",
            s3_region="",
            s3_access_key_reference="secret://env/CLIENTPLATFORM_SECRET_S3_ACCESS_KEY",
            s3_secret_key_reference="secret://env/CLIENTPLATFORM_SECRET_S3_SECRET_KEY",
            s3_session_token_reference="",
            signing_secret_reference="secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY",
            max_object_bytes=1_000_000,
            upstream_timeout_seconds=10.0,
            chunk_size=4,
        )
        store = FilesystemMediaObjectStore(
            root=str(self.root),
            max_object_bytes=self.config.max_object_bytes,
        )
        self.runtime = await start_media_gateway_runtime(
            self.config,
            store=store,
            credential_provider=self.provider,
        )
        assert self.runtime is not None
        server = self.runtime.site._server
        assert server is not None and server.sockets
        self.port = int(server.sockets[0].getsockname()[1])
        self.resolver = HmacMediaGatewayResolver(
            base_url=self.config.public_base_url,
            credential_provider=self.provider,
            signing_secret_reference=self.config.signing_secret_reference,
            ttl_seconds=120,
        )

    async def asyncTearDown(self) -> None:
        await stop_media_gateway_runtime()
        self.temp.cleanup()

    def _local_url(self, signed_url: str) -> str:
        parsed = urlsplit(signed_url)
        return urlunsplit(
            (
                "http",
                f"127.0.0.1:{self.port}",
                parsed.path,
                parsed.query,
                "",
            )
        )

    async def test_signed_gateway_streams_full_head_and_range_responses(self) -> None:
        signed = await self.resolver.resolve(
            "s3://private-bucket/program/audio.mp3",
            ContentKind.AUDIO,
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(self._local_url(signed)) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Accept-Ranges"], "bytes")
                self.assertEqual(response.headers["Cache-Control"], "private, no-store")
                self.assertEqual(await response.read(), self.media_content)
            async with session.head(self._local_url(signed)) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    int(response.headers["Content-Length"]), len(self.media_content)
                )
                self.assertEqual(await response.read(), b"")
            async with session.get(
                self._local_url(signed),
                headers={"Range": "bytes=3-7"},
            ) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(
                    response.headers["Content-Range"],
                    f"bytes 3-7/{len(self.media_content)}",
                )
                self.assertEqual(await response.read(), self.media_content[3:8])

    async def test_bad_expired_and_disallowed_requests_fail_closed(self) -> None:
        signed = await self.resolver.resolve(
            "s3://private-bucket/program/audio.mp3",
            ContentKind.AUDIO,
        )
        parsed = urlsplit(self._local_url(signed))
        tampered = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path + ".other", parsed.query, "")
        )
        expired_value = int(time.time()) - 1
        expired_path = "/clientplatform/media/private-bucket/program/audio.mp3"
        expired_signature = media_gateway_signature(
            secret=self.secret,
            path=expired_path,
            expires=expired_value,
        )
        expired = (
            f"http://127.0.0.1:{self.port}{expired_path}"
            f"?expires={expired_value}&sig={expired_signature}"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(tampered) as response:
                self.assertEqual(response.status, 403)
            async with session.get(expired) as response:
                self.assertEqual(response.status, 403)
            async with session.get(self._local_url(signed) + "&extra=1") as response:
                self.assertEqual(response.status, 403)

    async def test_telegram_adapter_resolves_gateway_and_fake_provider_fetches_media(self) -> None:
        fetched: list[bytes] = []
        provider_payloads: list[dict[str, str]] = []

        async def post_json(url, payload, timeout_seconds):
            del timeout_seconds
            self.assertIn("/bottelegram-test-token/sendAudio", url)
            provider_payloads.append(dict(payload))
            async with aiohttp.ClientSession() as session:
                async with session.get(self._local_url(payload["audio"])) as response:
                    self.assertEqual(response.status, 200)
                    fetched.append(await response.read())
            return 200, {"ok": True, "result": {"message_id": 9001}}

        client = AiohttpTelegramBotClient(post_json=post_json)
        adapter = TelegramDispatchAdapter(client, media_resolver=self.resolver)
        message_id = await adapter.send(_claimed(), "telegram-test-token")

        self.assertEqual(message_id, "9001")
        self.assertEqual(fetched, [self.media_content])
        self.assertEqual(provider_payloads[0]["chat_id"], "700001")
        self.assertTrue(provider_payloads[0]["audio"].startswith("https://media.example.test/clientplatform/media/"))
        self.assertNotIn(self.secret, repr(provider_payloads))
        self.assertNotIn("s3://", repr(provider_payloads))


if __name__ == "__main__":
    unittest.main()
