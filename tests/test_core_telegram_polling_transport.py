from __future__ import annotations

import os
import socket
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe

from core.telegram_bot import (
    PollingAiohttpSession,
    ResilientBot,
    build_bot,
    telegram_connector_options,
    telegram_ip_family,
)


_TEST_TOKEN = str(123_456_789) + ":" + ("T" * 35)


class TelegramPollingTransportContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_connector_forces_ipv4_and_reuses_short_ui_sessions(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            session = PollingAiohttpSession()
            options = session.connector_options
            family = telegram_ip_family()

        self.assertEqual(family, socket.AF_INET)
        self.assertEqual(options["family"], socket.AF_INET)
        self.assertEqual(options["ttl_dns_cache"], 60)
        self.assertFalse(options["force_close"])
        self.assertEqual(options["keepalive_timeout"], 20.0)
        self.assertEqual(options["limit_per_host"], 20)
        self.assertTrue(options["enable_cleanup_closed"])
        self.assertEqual(session.proxy_mode, "direct")
        self.assertEqual(session.active_route, "system")
        self.assertEqual(session.route_count, 0)
        await session.close()

    async def test_connector_policy_remains_explicitly_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_IP_FAMILY": "auto",
                "TELEGRAM_FORCE_CLOSE": "1",
                "TELEGRAM_DNS_TTL_SEC": "7",
            },
            clear=True,
        ):
            options = telegram_connector_options()

        self.assertEqual(options["family"], socket.AF_UNSPEC)
        self.assertEqual(options["ttl_dns_cache"], 7)
        self.assertTrue(options["force_close"])
        self.assertNotIn("keepalive_timeout", options)

    async def test_invalid_family_fails_closed_to_ipv4(self) -> None:
        with patch.dict(
            os.environ,
            {"TELEGRAM_IP_FAMILY": "broken"},
            clear=True,
        ):
            self.assertEqual(telegram_ip_family(), socket.AF_INET)

    async def test_native_http_connect_proxy_does_not_require_socks_dependency(self) -> None:
        proxy_url = "http://relay.internal:3128"
        fake_client = MagicMock()
        fake_client.closed = False
        fake_client.close = AsyncMock()
        connector = object()

        session = PollingAiohttpSession(proxy=proxy_url)
        session._connector_type = MagicMock(return_value=connector)

        with patch("core.telegram_bot.ClientSession", return_value=fake_client) as factory:
            created = await session.create_session()

        self.assertIs(created, fake_client)
        self.assertEqual(session.proxy_mode, "http_connect")
        self.assertIsNone(session.proxy)
        self.assertEqual(factory.call_args.kwargs["proxy"], proxy_url)
        self.assertIs(factory.call_args.kwargs["connector"], connector)
        await session.close()

    async def test_http_connect_proxy_validation_rejects_ambiguous_urls(self) -> None:
        invalid_urls = (
            "http://relay.internal",
            "http://relay.internal:3128/path",
            "http://relay.internal:3128?secret=value",
            "http://relay.internal:3128#fragment",
        )

        for proxy_url in invalid_urls:
            with self.subTest(proxy_url=proxy_url):
                with self.assertRaisesRegex(
                    ValueError,
                    "invalid_telegram_http_proxy_url",
                ):
                    PollingAiohttpSession(proxy=proxy_url)

    async def test_build_bot_selects_http_connect_relay_without_exposing_url(self) -> None:
        synthetic_password = "relay-" + ("R" * 12)
        proxy_url = (
            "http://relay-user:"
            + synthetic_password
            + "@relay.internal:3128"
        )

        with patch.dict(
            os.environ,
            {"TELEGRAM_PROXY_URL": proxy_url},
            clear=True,
        ):
            bot = build_bot(_TEST_TOKEN)

        self.assertIsInstance(bot.session, PollingAiohttpSession)
        self.assertEqual(bot.session.proxy_mode, "http_connect")
        self.assertNotIn(synthetic_password, repr(bot.session.connector_options))
        await bot.session.close()

    async def test_build_bot_isolates_ui_and_polling_routes(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CLIENTPLATFORM_TELEGRAM_API_IPV4_POOL": (
                    "149.154.167.220,149.154.167.221"
                )
            },
            clear=True,
        ):
            bot = build_bot(_TEST_TOKEN)

        self.assertIsInstance(bot.session, PollingAiohttpSession)
        self.assertIsInstance(bot.polling_session, PollingAiohttpSession)
        self.assertIsNot(bot.session, bot.polling_session)
        self.assertEqual(bot.session.transport_role, "ui")
        self.assertEqual(bot.polling_session.transport_role, "polling")
        self.assertEqual(bot.session.active_route, "149.154.167.220")
        self.assertEqual(bot.polling_session.active_route, "149.154.167.221")
        self.assertEqual(bot.session.route_count, 2)
        self.assertEqual(bot.polling_session.route_count, 2)
        await bot.session.close()

    async def test_polling_reset_does_not_reset_or_rotate_ui_lane(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CLIENTPLATFORM_TELEGRAM_API_IPV4_POOL": (
                    "149.154.167.220,149.154.167.221"
                )
            },
            clear=True,
        ):
            bot = build_bot(_TEST_TOKEN)

        reset = await bot.polling_session.reset_transport(
            observed_generation=0,
        )

        self.assertTrue(reset)
        self.assertEqual(bot.polling_session.transport_generation, 1)
        self.assertEqual(bot.polling_session.active_route, "149.154.167.220")
        self.assertEqual(bot.session.transport_generation, 0)
        self.assertEqual(bot.session.active_route, "149.154.167.220")
        await bot.session.close()

    async def test_transport_generation_prevents_duplicate_concurrent_resets(self) -> None:
        session = PollingAiohttpSession(
            route_pool=("149.154.167.220", "149.154.167.221"),
        )

        first = await session.reset_transport(observed_generation=0)
        duplicate = await session.reset_transport(observed_generation=0)

        self.assertTrue(first)
        self.assertFalse(duplicate)
        self.assertEqual(session.transport_generation, 1)
        self.assertEqual(session.active_route, "149.154.167.221")
        await session.close()

    async def test_network_retry_resets_connector_before_retrying(self) -> None:
        failure = TelegramNetworkError(method=GetMe(), message="timeout")
        parent_call = AsyncMock(side_effect=[failure, {"ok": True}])
        sleep = AsyncMock()

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_NETWORK_RETRIES": "1",
                "TELEGRAM_NETWORK_RETRY_DELAY_SEC": "1.25",
                "CLIENTPLATFORM_TELEGRAM_API_IPV4_POOL": (
                    "149.154.167.220,149.154.167.221"
                ),
            },
            clear=True,
        ):
            bot = build_bot(_TEST_TOKEN)

        self.assertIsInstance(bot, ResilientBot)
        self.assertIsInstance(bot.session, PollingAiohttpSession)
        session = bot.session

        with (
            patch.object(Bot, "__call__", new=parent_call),
            patch("core.telegram_bot.asyncio.sleep", new=sleep),
        ):
            result = await bot(GetMe())

        self.assertEqual(result, {"ok": True})
        self.assertEqual(parent_call.await_count, 2)
        self.assertEqual(sleep.await_count, 1)
        self.assertEqual(sleep.await_args.args, (1.25,))
        self.assertEqual(session.transport_generation, 1)
        self.assertEqual(session.active_route, "149.154.167.221")
        self.assertEqual(bot.polling_session.transport_generation, 0)
        await session.close()

    async def test_retry_limit_still_surfaces_persistent_failure(self) -> None:
        failure = TelegramNetworkError(method=GetMe(), message="timeout")
        parent_call = AsyncMock(side_effect=[failure, failure])

        with patch.dict(
            os.environ,
            {"TELEGRAM_NETWORK_RETRIES": "1"},
            clear=True,
        ):
            bot = build_bot(_TEST_TOKEN)

        with (
            patch.object(Bot, "__call__", new=parent_call),
            patch("core.telegram_bot.asyncio.sleep", new=AsyncMock()),
            self.assertRaises(TelegramNetworkError),
        ):
            await bot(GetMe())

        self.assertEqual(parent_call.await_count, 2)
        self.assertEqual(bot.session.transport_generation, 2)
        self.assertEqual(bot.polling_session.transport_generation, 0)
        await bot.session.close()


if __name__ == "__main__":
    unittest.main()
