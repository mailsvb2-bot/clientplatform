from __future__ import annotations

import unittest

from aiogram import Dispatcher
from aiohttp import web

from clientplatform.runtime.bot_gateway import (
    BotGatewayRuntimeConfig,
    ManagedBotGatewayRuntime,
    managed_bot_telegram_webhook,
)
from clientplatform.runtime.secrets import EnvironmentCredentialProvider


class ManagedBotGatewayHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.runtime = ManagedBotGatewayRuntime(
            dispatcher=Dispatcher(),
            config=BotGatewayRuntimeConfig(
                enabled=True,
                path_prefix="/clientplatform/managed-bots",
                batch_size=10,
                interval_seconds=0.5,
                tick_timeout_seconds=30.0,
                lock_ttl_seconds=300,
                max_attempts=5,
                per_minute_limit=120,
                queue_limit=1000,
                max_payload_bytes=262_144,
            ),
            credential_provider=EnvironmentCredentialProvider({}),
        )

    async def test_register_route_exposes_health_state_but_no_telegram_post(self) -> None:
        app = web.Application()
        self.runtime.register_route(app)
        self.assertIs(app["clientplatform_bot_gateway_runtime"], self.runtime)
        paths = [getattr(route.resource, "canonical", "") for route in app.router.routes()]
        self.assertNotIn(
            "/clientplatform/managed-bots/telegram/{external_bot_id}",
            paths,
        )

    async def test_runtime_rejects_direct_telegram_webhook(self) -> None:
        with self.assertRaises(web.HTTPNotFound):
            await self.runtime.handle_webhook(object())

    async def test_compatibility_handler_also_rejects_webhook(self) -> None:
        with self.assertRaises(web.HTTPNotFound):
            await managed_bot_telegram_webhook(object())

    def test_health_identifies_polling_transport(self) -> None:
        snapshot = self.runtime.health_snapshot()
        self.assertEqual(snapshot["transport"], "polling")
        self.assertEqual(snapshot["active_pollers"], 0)


if __name__ == "__main__":
    unittest.main()
