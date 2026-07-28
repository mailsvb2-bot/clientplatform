from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from clientplatform.application.dispatch_worker import DispatchBatchResult
from clientplatform.runtime.dispatch_runtime import (
    DispatchRuntime,
    DispatchRuntimeConfig,
    dispatch_runtime_config,
    run_configured_dispatch_tick,
)
from clientplatform.runtime.scheduler import ClientPlatformDispatchScheduler
from clientplatform.runtime.secrets import EnvironmentCredentialProvider, SecretReferenceError
from clientplatform.transport import AdapterRegistry
from clientplatform.transport.telegram_http import (
    AiohttpTelegramBotClient,
    TelegramBotApiError,
)


def _config(
    *,
    enabled: bool,
    interval_seconds: float = 0.01,
    tick_timeout_seconds: float = 0.05,
) -> DispatchRuntimeConfig:
    return DispatchRuntimeConfig(
        enabled=enabled,
        interval_seconds=interval_seconds,
        tick_timeout_seconds=tick_timeout_seconds,
        batch_size=3,
        max_attempts=4,
        lock_ttl_seconds=60,
        http_timeout_seconds=2.0,
    )


def _runtime(*, enabled: bool, tick_timeout_seconds: float = 0.05) -> DispatchRuntime:
    return DispatchRuntime(
        config=_config(
            enabled=enabled,
            tick_timeout_seconds=tick_timeout_seconds,
        ),
        credential_provider=EnvironmentCredentialProvider(
            {"CLIENTPLATFORM_SECRET_TELEGRAM_MAIN": "123456:TEST_TOKEN"}
        ),
        adapters=AdapterRegistry([]),
    )


class ClientPlatformSecretProviderTests(unittest.TestCase):
    def test_environment_reference_resolves_without_exposing_value(self) -> None:
        secret = "123456:VERY_PRIVATE_TOKEN"
        provider = EnvironmentCredentialProvider(
            {"CLIENTPLATFORM_SECRET_TELEGRAM_MAIN": secret}
        )
        self.assertEqual(
            provider.resolve("secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_MAIN"),
            secret,
        )

        for reference in (
            secret,
            "secret://env/HOME",
            "vault://clientplatform/telegram",
            "secret://env/CLIENTPLATFORM_SECRET_MISSING",
        ):
            with self.subTest(reference=reference):
                with self.assertRaises(SecretReferenceError) as raised:
                    provider.resolve(reference)
                self.assertNotIn(secret, str(raised.exception))

    def test_runtime_is_enabled_by_default_and_bounds_environment(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            config = dispatch_runtime_config()
        self.assertTrue(config.enabled)
        self.assertEqual(config.batch_size, 10)

        with patch.dict(
            "os.environ",
            {"CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED": "0"},
            clear=True,
        ):
            self.assertFalse(dispatch_runtime_config().enabled)

        with patch.dict(
            "os.environ",
            {
                "CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED": "yes",
                "CLIENTPLATFORM_DISPATCH_BATCH_SIZE": "10000",
                "CLIENTPLATFORM_DISPATCH_TICK_TIMEOUT_SEC": "1",
            },
            clear=True,
        ):
            config = dispatch_runtime_config()
        self.assertTrue(config.enabled)
        self.assertEqual(config.batch_size, 10)
        self.assertEqual(config.tick_timeout_seconds, 120.0)


class ClientPlatformTelegramHttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_api_methods_use_https_and_return_message_id(self) -> None:
        calls: list[tuple[str, dict[str, str], float]] = []

        async def post_json(
            url: str,
            payload: dict[str, str],
            timeout_seconds: float,
        ) -> tuple[int, object]:
            calls.append((url, dict(payload), timeout_seconds))
            return 200, {"ok": True, "result": {"message_id": 77}}

        client = AiohttpTelegramBotClient(
            timeout_seconds=3.0,
            post_json=post_json,
        )
        token = "123456:ABC_DEF"
        self.assertEqual(
            await client.send_message(token=token, chat_id="42", text="Привет"),
            "77",
        )
        self.assertEqual(
            await client.send_audio(
                token=token,
                chat_id="42",
                audio="https://cdn.example/audio.mp3",
            ),
            "77",
        )
        self.assertEqual(
            await client.send_photo(
                token=token,
                chat_id="42",
                photo="telegram-file-id",
            ),
            "77",
        )
        self.assertTrue(all(url.startswith("https://api.telegram.org/bot") for url, _, _ in calls))
        self.assertTrue(calls[0][0].endswith("/sendMessage"))
        self.assertEqual(calls[0][1], {"chat_id": "42", "text": "Привет"})
        self.assertTrue(calls[1][0].endswith("/sendAudio"))
        self.assertEqual(calls[1][1]["audio"], "https://cdn.example/audio.mp3")
        self.assertTrue(calls[2][0].endswith("/sendPhoto"))
        self.assertEqual(calls[0][2], 3.0)

    async def test_provider_failure_is_sanitized_and_classified(self) -> None:
        token = "123456:SECRET_TOKEN"

        async def rejected(
            _url: str,
            _payload: dict[str, str],
            _timeout_seconds: float,
        ) -> tuple[int, object]:
            return 401, {
                "ok": False,
                "error_code": 401,
                "description": f"invalid token {token}",
            }

        client = AiohttpTelegramBotClient(post_json=rejected)
        with self.assertRaises(TelegramBotApiError) as raised:
            await client.send_message(token=token, chat_id="42", text="test")
        self.assertEqual(raised.exception.code, "telegram_api_401")
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn(token, str(raised.exception))

    async def test_rate_limit_is_retryable(self) -> None:
        async def rate_limited(
            _url: str,
            _payload: dict[str, str],
            _timeout_seconds: float,
        ) -> tuple[int, object]:
            return 429, {"ok": False, "error_code": 429}

        client = AiohttpTelegramBotClient(post_json=rate_limited)
        with self.assertRaises(TelegramBotApiError) as raised:
            await client.send_message(token="1:TOKEN", chat_id="42", text="test")
        self.assertTrue(raised.exception.retryable)


class ClientPlatformDispatchRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_runtime_is_a_noop_without_database_or_network(self) -> None:
        result = await run_configured_dispatch_tick(_runtime(enabled=False))
        self.assertEqual(
            result,
            DispatchBatchResult(claimed=0, sent=0, retried=0, dead=0),
        )

    async def test_scheduler_has_one_owner_and_reports_progress(self) -> None:
        observed = asyncio.Event()
        calls = 0

        async def tick(_runtime_value: DispatchRuntime) -> DispatchBatchResult:
            nonlocal calls
            calls += 1
            observed.set()
            return DispatchBatchResult(claimed=2, sent=1, retried=1, dead=0)

        scheduler = ClientPlatformDispatchScheduler(
            _runtime(enabled=True),
            tick=tick,
            task_factory=lambda coro: asyncio.get_running_loop().create_task(coro),
        )
        self.assertTrue(scheduler.start())
        self.assertFalse(scheduler.start())
        await asyncio.wait_for(observed.wait(), timeout=1.0)
        snapshot = scheduler.health_snapshot()
        self.assertTrue(snapshot.enabled)
        self.assertTrue(snapshot.running)
        self.assertGreaterEqual(snapshot.iterations, 1)
        self.assertGreaterEqual(snapshot.claimed, 2)
        self.assertGreaterEqual(snapshot.sent, 1)
        self.assertGreaterEqual(snapshot.retried, 1)
        self.assertGreaterEqual(calls, 1)
        await scheduler.stop()
        self.assertFalse(scheduler.health_snapshot().running)

    async def test_scheduler_timeout_is_bounded_and_visible(self) -> None:
        scheduler: ClientPlatformDispatchScheduler

        async def blocked(_runtime_value: DispatchRuntime) -> DispatchBatchResult:
            await asyncio.Future()
            raise AssertionError("unreachable")

        async def stop_after_tick(_delay: float) -> None:
            scheduler._running = False

        scheduler = ClientPlatformDispatchScheduler(
            _runtime(enabled=True, tick_timeout_seconds=0.01),
            tick=blocked,
            sleep=stop_after_tick,
        )
        scheduler._running = True
        await scheduler._run()
        snapshot = scheduler.health_snapshot()
        self.assertEqual(snapshot.errors, 1)
        self.assertEqual(snapshot.last_error, "dispatch_tick_timeout")

    async def test_disabled_scheduler_does_not_create_task(self) -> None:
        created = 0

        def task_factory(coro):
            nonlocal created
            created += 1
            coro.close()
            raise AssertionError("disabled scheduler must not create a task")

        scheduler = ClientPlatformDispatchScheduler(
            _runtime(enabled=False),
            task_factory=task_factory,
        )
        self.assertFalse(scheduler.start())
        self.assertEqual(created, 0)
        self.assertFalse(scheduler.health_snapshot().running)


if __name__ == "__main__":
    unittest.main()
