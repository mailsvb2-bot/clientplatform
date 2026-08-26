from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from clientplatform.application.dispatch_worker import _effective_max_attempts
from runtime.messenger_max_sender import MaxProviderRateLimitError
from clientplatform.domain.connections import (
    ClaimedDispatch,
    ConnectionPlatform,
    Dispatch,
    DispatchStatus,
)
from clientplatform.domain.programs import ContentKind
from clientplatform.runtime import lifecycle
from clientplatform.runtime.dispatch_runtime import DispatchRuntimeConfig
from clientplatform.runtime.scheduler import DispatchSchedulerHealth
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from clientplatform.transport.media import (
    HmacMediaGatewayResolver,
    MediaReferenceError,
    SafeMediaReferenceResolver,
)
from clientplatform.transport.telegram import TelegramDispatchAdapter
from clientplatform.transport.telegram_http import TelegramBotApiError


NOW = "2026-07-28T00:00:00+00:00"


def _claimed(*, kind: ContentKind, payload_ref: str) -> ClaimedDispatch:
    return ClaimedDispatch(
        dispatch=Dispatch(
            id="11111111-1111-4111-8111-111111111111",
            business_id="22222222-2222-4222-8222-222222222222",
            platform=ConnectionPlatform.TELEGRAM,
            logical_delivery_id="33333333-3333-4333-8333-333333333333",
            connection_id="44444444-4444-4444-8444-444444444444",
            customer_identity_id="55555555-5555-4555-8555-555555555555",
            payload_kind=kind,
            payload_ref=payload_ref,
            idempotency_key="delivery:test",
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


class SignedMediaResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_s3_reference_becomes_deterministic_short_lived_https_url(self) -> None:
        secret = "media-signing-secret"
        resolver = HmacMediaGatewayResolver(
            base_url="https://media.example.test/clientplatform",
            credential_provider=EnvironmentCredentialProvider(
                {"CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY": secret}
            ),
            signing_secret_reference="secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY",
            ttl_seconds=300,
            clock=lambda: 1000.0,
        )
        first = await resolver.resolve(
            "s3://private-bucket/programs/сон/audio 01.mp3",
            ContentKind.AUDIO,
        )
        second = await resolver.resolve(
            "s3://private-bucket/programs/сон/audio 01.mp3",
            ContentKind.AUDIO,
        )
        self.assertEqual(first, second)
        parsed = urlsplit(first)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "media.example.test")
        self.assertIn("/clientplatform/media/private-bucket/programs/", parsed.path)
        query = parse_qs(parsed.query)
        self.assertEqual(query["expires"], ["1300"])
        self.assertEqual(len(query["sig"][0]), 43)
        self.assertNotIn(secret, first)
        self.assertNotIn("s3://", first)

    async def test_bot_local_id_fails_but_https_passes_without_secret_resolution(
        self,
    ) -> None:
        class ExplodingProvider:
            def resolve(self, _reference: str) -> str:
                raise AssertionError("public HTTPS must not resolve a secret")

        resolver = HmacMediaGatewayResolver(
            base_url="https://media.example.test",
            credential_provider=ExplodingProvider(),
            signing_secret_reference="secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY",
            ttl_seconds=120,
        )
        with self.assertRaisesRegex(
            MediaReferenceError,
            "media_bot_local_reference_not_portable",
        ):
            await resolver.resolve("telegram-file-id", ContentKind.VIDEO)
        self.assertEqual(
            await resolver.resolve(
                "https://cdn.example.test/video.mp4",
                ContentKind.VIDEO,
            ),
            "https://cdn.example.test/video.mp4",
        )

    async def test_unsafe_or_unresolved_references_fail_closed(self) -> None:
        safe = SafeMediaReferenceResolver()
        for reference in (
            "http://cdn.example.test/file.mp3",
            "s3://private-bucket/file.mp3",
            "ftp://files.example.test/file.mp3",
            "https://user:password@example.test/file.mp3",
        ):
            with self.subTest(reference=reference):
                with self.assertRaises(MediaReferenceError):
                    await safe.resolve(reference, ContentKind.AUDIO)

        resolver = HmacMediaGatewayResolver(
            base_url="https://media.example.test",
            credential_provider=EnvironmentCredentialProvider(
                {"CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY": "secret"}
            ),
            signing_secret_reference="secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY",
            ttl_seconds=120,
        )
        for reference in (
            "s3://Bad_Bucket/file.mp3",
            "s3://private-bucket/../file.mp3",
            "s3://private-bucket/folder//file.mp3",
            "s3://private-bucket/file.mp3?download=1",
        ):
            with self.subTest(reference=reference):
                with self.assertRaises(MediaReferenceError):
                    await resolver.resolve(reference, ContentKind.AUDIO)

    def test_gateway_configuration_is_bounded(self) -> None:
        provider = EnvironmentCredentialProvider(
            {"CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY": "secret"}
        )
        for base_url in (
            "http://media.example.test",
            "https://user:password@media.example.test",
            "https://media.example.test?unsafe=1",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    HmacMediaGatewayResolver(
                        base_url=base_url,
                        credential_provider=provider,
                        signing_secret_reference="secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY",
                    )
        for ttl in (59, 901):
            with self.subTest(ttl=ttl):
                with self.assertRaises(ValueError):
                    HmacMediaGatewayResolver(
                        base_url="https://media.example.test",
                        credential_provider=provider,
                        signing_secret_reference="secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY",
                        ttl_seconds=ttl,
                    )


class TelegramResolvedMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_sends_only_resolved_url(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.audio = ""

            async def send_audio(self, *, token, chat_id, audio):
                del token, chat_id
                self.audio = audio
                return "9001"

            async def send_video(self, **_kwargs):
                raise AssertionError

            async def send_document(self, **_kwargs):
                raise AssertionError

            async def send_photo(self, **_kwargs):
                raise AssertionError

            async def send_message(self, **_kwargs):
                raise AssertionError

        client = Client()
        resolver = HmacMediaGatewayResolver(
            base_url="https://media.example.test",
            credential_provider=EnvironmentCredentialProvider(
                {"CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY": "secret"}
            ),
            signing_secret_reference="secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY",
            ttl_seconds=60,
            clock=lambda: 1000.0,
        )
        adapter = TelegramDispatchAdapter(client, media_resolver=resolver)
        message_id = await adapter.send(
            _claimed(kind=ContentKind.AUDIO, payload_ref="s3://bucket-a/audio.mp3"),
            "telegram-token",
        )
        self.assertEqual(message_id, "9001")
        self.assertTrue(client.audio.startswith("https://media.example.test/media/"))
        self.assertNotIn("s3://", client.audio)


class TerminalRetryPolicyTests(unittest.TestCase):
    def test_terminal_transport_and_media_errors_skip_retry_budget(self) -> None:
        self.assertEqual(
            _effective_max_attempts(
                TelegramBotApiError("telegram_api_401", retryable=False),
                8,
            ),
            1,
        )
        self.assertEqual(_effective_max_attempts(MediaReferenceError("bad_media"), 8), 1)
        self.assertEqual(_effective_max_attempts(OSError("temporary"), 8), 8)
        self.assertEqual(
            _effective_max_attempts(
                MaxProviderRateLimitError("rate limited"),
                8,
                non_replay_boundary_crossed=True,
            ),
            8,
        )


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        lifecycle._dispatch_scheduler = None

    async def asyncTearDown(self) -> None:
        lifecycle._dispatch_scheduler = None

    async def test_lifecycle_is_explicit_single_owner_and_observable(self) -> None:
        class FakeScheduler:
            instances: list["FakeScheduler"] = []

            def __init__(self, runtime) -> None:
                self.runtime = runtime
                self.running = False
                self.stopped = False
                self.__class__.instances.append(self)

            def start(self) -> bool:
                self.running = True
                return True

            async def stop(self) -> None:
                self.running = False
                self.stopped = True

            def health_snapshot(self) -> DispatchSchedulerHealth:
                return DispatchSchedulerHealth(
                    enabled=True,
                    running=self.running,
                    iterations=2,
                    claimed=3,
                    sent=2,
                    retried=1,
                    dead=0,
                    errors=0,
                    last_error="",
                    last_tick_age_seconds=1,
                )

        runtime = SimpleNamespace(
            config=DispatchRuntimeConfig(
                enabled=True,
                interval_seconds=5.0,
                tick_timeout_seconds=120.0,
                batch_size=10,
                max_attempts=8,
                lock_ttl_seconds=900,
                http_timeout_seconds=20.0,
            )
        )
        with patch.object(lifecycle, "ClientPlatformDispatchScheduler", FakeScheduler):
            self.assertTrue(await lifecycle.start_clientplatform_runtime(runtime))
            self.assertFalse(await lifecycle.start_clientplatform_runtime(runtime))
            snapshot = lifecycle.clientplatform_runtime_health_snapshot()
            self.assertTrue(snapshot["clientplatform_runtime_composed"])
            self.assertTrue(snapshot["clientplatform_dispatch_running"])
            self.assertEqual(snapshot["clientplatform_dispatch_sent"], 2)
            await lifecycle.stop_clientplatform_runtime()
        self.assertTrue(FakeScheduler.instances[0].stopped)
        self.assertFalse(
            lifecycle.clientplatform_runtime_health_snapshot()[
                "clientplatform_runtime_composed"
            ]
        )


if __name__ == "__main__":
    unittest.main()
