from __future__ import annotations

import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from clientplatform.domain.bot_gateway import (
    ClaimedIngressEvent,
    IngressEvent,
    IngressEventStatus,
    ManagedBotRoute,
)
from clientplatform.runtime.bot_gateway import BotGatewayRuntimeConfig
from clientplatform.runtime.partner_aware_bot_gateway import ManagedBotGatewayRuntime


class PartnerAwareGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_partner_reply_is_processed_before_customer_link_or_dispatcher(self) -> None:
        business_id = str(uuid4())
        connection_id = str(uuid4())
        managed_bot_id = str(uuid4())
        payload = {
            "update_id": 90001,
            "message": {
                "message_id": 1,
                "date": 1,
                "chat": {"id": 700003, "type": "private"},
                "from": {
                    "id": 700003,
                    "is_bot": False,
                    "first_name": "Partner",
                },
                "text": "Да, интересно",
            },
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        event = IngressEvent(
            id=str(uuid4()),
            business_id=business_id,
            managed_bot_id=managed_bot_id,
            provider_update_id="90001",
            payload_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            payload_json=encoded,
            status=IngressEventStatus.PROCESSING,
            attempts=0,
            available_at="2026-08-10T00:00:00+00:00",
            created_at="2026-08-10T00:00:00+00:00",
            updated_at="2026-08-10T00:00:00+00:00",
            locked_at="2026-08-10T00:00:00+00:00",
            lock_token="lease",
        )
        route = ManagedBotRoute(
            managed_bot_id=managed_bot_id,
            business_id=business_id,
            connection_id=connection_id,
            external_bot_id="123456",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_PARTNER_TEST",
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_PARTNER_WEBHOOK"
            ),
        )
        feed_webhook_update = AsyncMock()
        dispatcher = SimpleNamespace(
            workflow_data={},
            feed_webhook_update=feed_webhook_update,
        )
        config = BotGatewayRuntimeConfig(
            enabled=True,
            path_prefix="/clientplatform/managed-bots",
            batch_size=10,
            interval_seconds=1.0,
            tick_timeout_seconds=30.0,
            lock_ttl_seconds=300,
            max_attempts=5,
            per_minute_limit=120,
            queue_limit=1000,
            max_payload_bytes=262144,
        )
        runtime = ManagedBotGatewayRuntime(dispatcher=dispatcher, config=config)
        with (
            patch(
                "clientplatform.runtime.partner_aware_bot_gateway.record_partner_reply_if_expected",
                return_value=str(uuid4()),
            ) as record_reply,
            patch(
                "clientplatform.runtime.partner_aware_bot_gateway.ensure_telegram_customer_link"
            ) as ensure_customer,
            patch(
                "clientplatform.runtime.partner_aware_bot_gateway.mark_ingress_event_processed"
            ) as mark_processed,
        ):
            await runtime._process_item(
                ClaimedIngressEvent(event=event, route=route)
            )
        record_reply.assert_called_once()
        ensure_customer.assert_not_called()
        feed_webhook_update.assert_not_awaited()
        mark_processed.assert_called_once()
        self.assertEqual(runtime._processed, 1)

    async def test_command_stays_on_normal_customer_dispatch_path(self) -> None:
        business_id = str(uuid4())
        connection_id = str(uuid4())
        managed_bot_id = str(uuid4())
        payload = {
            "update_id": 90002,
            "message": {
                "message_id": 2,
                "date": 1,
                "chat": {"id": 700004, "type": "private"},
                "from": {
                    "id": 700004,
                    "is_bot": False,
                    "first_name": "Partner",
                },
                "text": "/start",
            },
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        event = IngressEvent(
            id=str(uuid4()),
            business_id=business_id,
            managed_bot_id=managed_bot_id,
            provider_update_id="90002",
            payload_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            payload_json=encoded,
            status=IngressEventStatus.PROCESSING,
            attempts=0,
            available_at="2026-08-10T00:00:00+00:00",
            created_at="2026-08-10T00:00:00+00:00",
            updated_at="2026-08-10T00:00:00+00:00",
            locked_at="2026-08-10T00:00:00+00:00",
            lock_token="lease",
        )
        route = ManagedBotRoute(
            managed_bot_id=managed_bot_id,
            business_id=business_id,
            connection_id=connection_id,
            external_bot_id="123456",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_PARTNER_TEST",
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_PARTNER_WEBHOOK"
            ),
        )
        feed_webhook_update = AsyncMock()
        dispatcher = SimpleNamespace(
            workflow_data={},
            feed_webhook_update=feed_webhook_update,
        )
        runtime = ManagedBotGatewayRuntime(
            dispatcher=dispatcher,
            config=BotGatewayRuntimeConfig(
                enabled=True,
                path_prefix="/clientplatform/managed-bots",
                batch_size=10,
                interval_seconds=1.0,
                tick_timeout_seconds=30.0,
                lock_ttl_seconds=300,
                max_attempts=5,
                per_minute_limit=120,
                queue_limit=1000,
                max_payload_bytes=262144,
            ),
        )
        fake_bot = Mock()
        with (
            patch(
                "clientplatform.runtime.partner_aware_bot_gateway.record_partner_reply_if_expected"
            ) as record_reply,
            patch(
                "clientplatform.runtime.partner_aware_bot_gateway.ensure_telegram_customer_link"
            ) as ensure_customer,
            patch(
                "clientplatform.runtime.partner_aware_bot_gateway.mark_ingress_event_processed"
            ) as mark_processed,
            patch.object(runtime, "_bot_for", AsyncMock(return_value=fake_bot)),
        ):
            await runtime._process_item(
                ClaimedIngressEvent(event=event, route=route)
            )
        record_reply.assert_not_called()
        ensure_customer.assert_called_once()
        feed_webhook_update.assert_awaited_once()
        mark_processed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
