from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from aiogram import Dispatcher

from clientplatform.application.control import _managed_telegram_connection
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


class ClientPlatformManagedBotGatewayRuntimeTests(unittest.TestCase):
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
        self.connections.register_managed_bot(
            actor=self.owner,
            connection_id=connection.id,
            external_bot_id="700001",
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_PRACTICE"
            ),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_initial_delivery_prefers_managed_bot_connection(self) -> None:
        selected = _managed_telegram_connection(
            actor=self.owner,
            repository=self.connections,
            conn=self.conn,
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.connection_type, ConnectionType.TELEGRAM_MANAGED_BOT)
        self.assertEqual(
            selected.credential_reference,
            "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_PRACTICE",
        )

    def test_runtime_configuration_is_bounded_and_tokenless(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "CLIENTPLATFORM_BOT_GATEWAY_ENABLED": "1",
                "CLIENTPLATFORM_BOT_GATEWAY_PATH_PREFIX": "/gateway/token/leak",
                "CLIENTPLATFORM_BOT_GATEWAY_BATCH_SIZE": "10000",
                "CLIENTPLATFORM_BOT_GATEWAY_MAX_PAYLOAD_BYTES": "99999999",
            },
            clear=True,
        ):
            config = bot_gateway_runtime_config()
        self.assertTrue(config.enabled)
        self.assertEqual(config.path_prefix, "/clientplatform/managed-bots")
        self.assertEqual(config.batch_size, 10)
        self.assertEqual(config.max_payload_bytes, 262_144)
        self.assertNotIn("token", config.telegram_route_path)
        self.assertNotIn("secret", config.telegram_route_path)

    def test_disabled_runtime_has_no_background_owner(self) -> None:
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
