from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from aiogram import Dispatcher
from aiohttp import web

from clientplatform.domain.bot_gateway import (
    AdmittedIngressEvent,
    IngressEvent,
    IngressEventStatus,
    ManagedBotRoute,
    ManagedBotRouteNotFound,
)
from clientplatform.runtime.bot_gateway import (
    BotGatewayRuntimeConfig,
    ManagedBotGatewayRuntime,
)
from clientplatform.runtime.secrets import EnvironmentCredentialProvider


class _Request:
    def __init__(self, *, secret: str, payload: object) -> None:
        self.match_info = {"external_bot_id": "700001"}
        self.headers = {"X-Telegram-Bot-Api-Secret-Token": secret}
        self._payload = payload

    async def json(self):
        return self._payload


class ManagedBotGatewayHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.route = ManagedBotRoute(
            managed_bot_id=str(uuid4()),
            business_id=str(uuid4()),
            connection_id=str(uuid4()),
            external_bot_id="700001",
            credential_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_PRACTICE"
            ),
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_PRACTICE"
            ),
        )
        self.event = IngressEvent(
            id=str(uuid4()),
            business_id=self.route.business_id,
            managed_bot_id=self.route.managed_bot_id,
            provider_update_id="1",
            payload_sha256="a" * 64,
            payload_json='{"update_id":1}',
            status=IngressEventStatus.PENDING,
            attempts=0,
            available_at="2026-07-29T07:00:00+00:00",
            created_at="2026-07-29T07:00:00+00:00",
            updated_at="2026-07-29T07:00:00+00:00",
        )
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
            credential_provider=EnvironmentCredentialProvider(
                {"CLIENTPLATFORM_SECRET_WEBHOOK_PRACTICE": "webhook-secret"}
            ),
        )

    @staticmethod
    def payload() -> dict[str, object]:
        return {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": 5001, "type": "private"},
                "from": {"id": 5001, "is_bot": False, "first_name": "Иван"},
                "text": "/start",
            },
        }

    async def test_unknown_route_and_wrong_secret_have_same_public_status(self) -> None:
        with patch(
            "clientplatform.runtime.bot_gateway.resolve_telegram_route",
            side_effect=ManagedBotRouteNotFound("missing"),
        ):
            with self.assertRaises(web.HTTPNotFound):
                await self.runtime.handle_webhook(
                    _Request(secret="wrong", payload=self.payload())
                )
        with patch(
            "clientplatform.runtime.bot_gateway.resolve_telegram_route",
            return_value=self.route,
        ):
            with self.assertRaises(web.HTTPNotFound):
                await self.runtime.handle_webhook(
                    _Request(secret="wrong", payload=self.payload())
                )

    async def test_valid_secret_returns_minimal_success_and_duplicate_state(self) -> None:
        admitted = AdmittedIngressEvent(event=self.event, duplicate=True)
        with (
            patch(
                "clientplatform.runtime.bot_gateway.resolve_telegram_route",
                return_value=self.route,
            ),
            patch(
                "clientplatform.runtime.bot_gateway.admit_telegram_update",
                return_value=admitted,
            ),
        ):
            response = await self.runtime.handle_webhook(
                _Request(secret="webhook-secret", payload=self.payload())
            )
        self.assertEqual(response.status, 200)
        body = json.loads(response.text)
        self.assertEqual(body, {"ok": True, "duplicate": True})
        self.assertNotIn("event_id", body)

    async def test_invalid_update_id_is_rejected_before_admission(self) -> None:
        payload = self.payload()
        payload["update_id"] = True
        with patch(
            "clientplatform.runtime.bot_gateway.resolve_telegram_route",
            return_value=self.route,
        ):
            with self.assertRaises(web.HTTPBadRequest):
                await self.runtime.handle_webhook(
                    _Request(secret="webhook-secret", payload=payload)
                )


if __name__ == "__main__":
    unittest.main()
