from __future__ import annotations

import json
import sqlite3
import unittest
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock, patch

from aiogram import Dispatcher

from clientplatform.application.control import _managed_telegram_connection
from clientplatform.domain.bot_gateway import (
    ClaimedIngressEvent,
    IngressEvent,
    IngressEventStatus,
    ManagedBotRoute,
)
from clientplatform.domain.connections import ConnectionType
from clientplatform.infrastructure import ConnectionRepository, TenancyRepository
from clientplatform.runtime.bot_gateway import (
    ManagedBotGatewayRuntime,
    bot_gateway_runtime_config,
)
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from services.db.schema import (
    clientplatform_bot_gateway,
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_tenancy,
)


class _FakeSession:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class _FakeUpdate:
    def model_dump(self, **_kwargs):
        return {
            "update_id": 41,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": 5001, "type": "private"},
                "from": {"id": 5001, "is_bot": False, "first_name": "Иван"},
                "text": "/start",
            },
        }


class _FakeBot:
    instances: list["_FakeBot"] = []
    runtime: ManagedBotGatewayRuntime | None = None

    def __init__(self, *, token: str) -> None:
        self.token = token
        self.session = _FakeSession()
        self.delete_calls: list[bool] = []
        self.get_updates_calls: list[dict[str, object]] = []
        self.instances.append(self)

    async def delete_webhook(self, *, drop_pending_updates: bool):
        self.delete_calls.append(drop_pending_updates)
        return True

    async def get_me(self):
        return SimpleNamespace(id=700001, username="practice_helper_bot")

    async def get_updates(self, **kwargs):
        self.get_updates_calls.append(dict(kwargs))
        assert self.runtime is not None
        self.runtime._running = False
        return [_FakeUpdate()]


class ClientPlatformManagedBotGatewayRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        clientplatform_bot_gateway.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.owner = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.connections = ConnectionRepository(self.conn)
        connection = self.connections.create_connection(
            actor=self.owner,
            platform="telegram",
            connection_type="telegram_managed_bot",
            external_account_id="700001",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_PRACTICE",
        )
        connection = self.connections.activate_connection(
            actor=self.owner,
            connection_id=connection.id,
        )
        self.managed_bot = self.connections.register_managed_bot(
            actor=self.owner,
            connection_id=connection.id,
            external_bot_id="700001",
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_PRACTICE"
            ),
            username="practice_helper_bot",
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def route(self) -> ManagedBotRoute:
        return ManagedBotRoute(
            managed_bot_id=self.managed_bot.id,
            business_id=self.owner.business_id,
            connection_id=self.managed_bot.connection_id,
            external_bot_id="700001",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_PRACTICE",
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_PRACTICE"
            ),
            username="practice_helper_bot",
        )

    async def test_initial_delivery_prefers_managed_bot_connection(self) -> None:
        selected = _managed_telegram_connection(
            actor=self.owner,
            repository=self.connections,
            conn=self.conn,
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.connection_type, ConnectionType.TELEGRAM_MANAGED_BOT)

    async def test_runtime_configuration_is_bounded_and_polling_only(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "CLIENTPLATFORM_BOT_GATEWAY_ENABLED": "1",
                "CLIENTPLATFORM_BOT_GATEWAY_BATCH_SIZE": "10000",
                "CLIENTPLATFORM_BOT_GATEWAY_MAX_PAYLOAD_BYTES": "99999999",
                "CLIENTPLATFORM_BOT_GATEWAY_POLL_TIMEOUT_SEC": "999",
            },
            clear=True,
        ):
            config = bot_gateway_runtime_config()
        self.assertTrue(config.enabled)
        self.assertEqual(config.transport, "polling")
        self.assertEqual(config.batch_size, 10)
        self.assertEqual(config.max_payload_bytes, 262_144)
        self.assertEqual(config.poll_timeout_seconds, 20)

    async def test_poller_removes_webhook_and_admits_start_update(self) -> None:
        runtime = ManagedBotGatewayRuntime(
            dispatcher=Dispatcher(),
            credential_provider=EnvironmentCredentialProvider(
                {"CLIENTPLATFORM_SECRET_TELEGRAM_PRACTICE": "telegram-token"}
            ),
        )
        runtime._running = True
        route = self.route()
        runtime._routes[route.managed_bot_id] = route
        _FakeBot.instances.clear()
        _FakeBot.runtime = runtime
        admitted = SimpleNamespace(duplicate=False)
        with (
            patch("clientplatform.runtime.bot_gateway.Bot", _FakeBot),
            patch(
                "clientplatform.runtime.bot_gateway.admit_telegram_update",
                return_value=admitted,
            ) as admit,
        ):
            await runtime._poll_route(route)
        bot = _FakeBot.instances[-1]
        self.assertEqual(bot.delete_calls, [False])
        self.assertEqual(len(bot.get_updates_calls), 1)
        self.assertEqual(bot.get_updates_calls[0]["offset"], None)
        admit.assert_called_once()
        self.assertEqual(admit.call_args.kwargs["provider_update_id"], 41)
        self.assertEqual(admit.call_args.kwargs["payload"]["message"]["text"], "/start")
        self.assertEqual(runtime.health_snapshot()["admitted"], 1)
        await runtime._stop_poller(route.managed_bot_id)

    async def test_staff_bridge_bypasses_customer_and_sales_paths(self) -> None:
        route = self.route()
        payload = _FakeUpdate().model_dump()
        payload["message"]["text"] = "/start bridge_staff-token"
        item = ClaimedIngressEvent(
            event=IngressEvent(
                id=str(uuid4()),
                business_id=route.business_id,
                managed_bot_id=route.managed_bot_id,
                provider_update_id="41",
                payload_sha256="0" * 64,
                payload_json=json.dumps(payload),
                status=IngressEventStatus.PROCESSING,
                attempts=1,
                available_at="2026-08-25T00:00:00+00:00",
                created_at="2026-08-25T00:00:00+00:00",
                updated_at="2026-08-25T00:00:00+00:00",
            ),
            route=route,
        )
        runtime = ManagedBotGatewayRuntime(dispatcher=Dispatcher())
        runtime._bot_for = AsyncMock(return_value=SimpleNamespace())  # type: ignore[method-assign]
        runtime._dispatcher.feed_webhook_update = AsyncMock()  # type: ignore[method-assign]
        with (
            patch("clientplatform.runtime.bot_gateway.consume_telegram_staff_bridge", return_value=101) as staff,
            patch("clientplatform.runtime.bot_gateway.ensure_telegram_customer_link") as ensure_customer,
            patch("clientplatform.runtime.bot_gateway.consume_telegram_customer_channel_link") as customer_bridge,
            patch("clientplatform.runtime.bot_gateway.mark_ingress_event_processed") as mark,
            patch("clientplatform.runtime.bot_gateway.Update.model_validate", return_value=SimpleNamespace()),
        ):
            await runtime._process_item(item)
        staff.assert_called_once_with(
            route=route, token="staff-token", telegram_user_id=5001,
            username=None, display_name="Иван",
        )
        ensure_customer.assert_not_called()
        customer_bridge.assert_not_called()
        mark.assert_called_once_with(item)

    async def test_promotion_start_captures_canonical_touch_before_dispatch(self) -> None:
        route = self.route()
        payload = _FakeUpdate().model_dump()
        payload["message"]["text"] = "/start cpa_sourceToken123"
        item = ClaimedIngressEvent(
            event=IngressEvent(
                id=str(uuid4()),
                business_id=route.business_id,
                managed_bot_id=route.managed_bot_id,
                provider_update_id="42",
                payload_sha256="1" * 64,
                payload_json=json.dumps(payload),
                status=IngressEventStatus.PROCESSING,
                attempts=1,
                available_at="2026-08-25T00:00:00+00:00",
                created_at="2026-08-25T00:00:00+00:00",
                updated_at="2026-08-25T00:00:00+00:00",
            ),
            route=route,
        )
        customer_id = str(uuid4())
        runtime = ManagedBotGatewayRuntime(dispatcher=Dispatcher())
        runtime._bot_for = AsyncMock(return_value=SimpleNamespace())  # type: ignore[method-assign]
        runtime._dispatcher.feed_webhook_update = AsyncMock()  # type: ignore[method-assign]
        with (
            patch(
                "clientplatform.runtime.bot_gateway.ensure_telegram_customer_link",
                return_value=SimpleNamespace(
                    business_id=route.business_id,
                    customer_id=customer_id,
                ),
            ),
            patch("clientplatform.runtime.bot_gateway.open_channel_promotion") as opened,
            patch("clientplatform.runtime.bot_gateway.mark_ingress_event_processed") as mark,
            patch(
                "clientplatform.runtime.bot_gateway.Update.model_validate",
                return_value=SimpleNamespace(),
            ),
        ):
            await runtime._process_item(item)

        opened.assert_called_once_with(
            source_token="sourceToken123",
            business_id=route.business_id,
            customer_id=customer_id,
        )
        mark.assert_called_once_with(item)
        runtime._dispatcher.feed_webhook_update.assert_awaited_once()

    async def test_promotion_start_parser_is_bounded_and_fail_closed(self) -> None:
        from clientplatform.runtime import bot_gateway as gateway

        self.assertIsNone(gateway._telegram_promotion_token(None))
        self.assertIsNone(gateway._telegram_promotion_token("обычный текст"))
        self.assertIsNone(gateway._telegram_promotion_token("/start ordinary"))
        self.assertIsNone(gateway._telegram_promotion_token("/start cpa_bad!"))
        self.assertEqual(
            gateway._telegram_promotion_token(" start   cpa_sourceToken123 "),
            "sourceToken123",
        )

    async def test_disabled_runtime_has_no_background_owner(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            config = bot_gateway_runtime_config()
        runtime = ManagedBotGatewayRuntime(
            dispatcher=Dispatcher(),
            config=config,
            credential_provider=EnvironmentCredentialProvider({}),
        )
        self.assertFalse(runtime.start())
        self.assertFalse(runtime.health_snapshot()["enabled"])


if __name__ == "__main__":
    unittest.main()
