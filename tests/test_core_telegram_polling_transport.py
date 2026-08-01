from __future__ import annotations

import os
import socket
import unittest
from unittest.mock import AsyncMock, patch

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
    async def test_default_connector_forces_ipv4_and_fresh_tcp_connections(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            session = PollingAiohttpSession()
            options = session.connector_options
            family = telegram_ip_family()

        self.assertEqual(family, socket.AF_INET)
        self.assertEqual(options["family"], socket.AF_INET)
        self.assertEqual(options["ttl_dns_cache"], 60)
        self.assertTrue(options["force_close"])
        await session.close()

    async def test_connector_policy_remains_explicitly_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_IP_FAMILY": "auto",
                "TELEGRAM_FORCE_CLOSE": "0",
                "TELEGRAM_DNS_TTL_SEC": "7",
            },
            clear=True,
        ):
            options = telegram_connector_options()

        self.assertEqual(options["family"], socket.AF_UNSPEC)
        self.assertEqual(options["ttl_dns_cache"], 7)
        self.assertFalse(options["force_close"])

    async def test_invalid_family_fails_closed_to_ipv4(self) -> None:
        with patch.dict(
            os.environ,
            {"TELEGRAM_IP_FAMILY": "broken"},
            clear=True,
        ):
            self.assertEqual(telegram_ip_family(), socket.AF_INET)

    async def test_transport_generation_prevents_duplicate_concurrent_resets(self) -> None:
        session = PollingAiohttpSession()

        first = await session.reset_transport(observed_generation=0)
        duplicate = await session.reset_transport(observed_generation=0)

        self.assertTrue(first)
        self.assertFalse(duplicate)
        self.assertEqual(session.transport_generation, 1)
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
        await bot.session.close()


if __name__ == "__main__":
    unittest.main()
