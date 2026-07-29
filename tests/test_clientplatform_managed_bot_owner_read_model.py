from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.connections import ConnectionNotFound, ManagedBotStatus
from clientplatform.infrastructure.managed_bot_owner_repository import (
    ManagedBotOwnerRepository,
)
from clientplatform.infrastructure.safe_bot_gateway_repository import (
    BotGatewayRepository,
)
from clientplatform.infrastructure.safe_connection_repository import (
    ConnectionRepository,
)
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_bot_gateway,
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_tenancy,
)


class ClientPlatformManagedBotOwnerReadModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        clientplatform_bot_gateway.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        first = tenancy.create_business(owner_user_id=101, name="Практика")
        second = tenancy.create_business(owner_user_id=202, name="Другая практика")
        self.owner = tenancy.resolve_context(
            user_id=101,
            business_id=first.business.id,
        )
        self.other_owner = tenancy.resolve_context(
            user_id=202,
            business_id=second.business.id,
        )
        connections = ConnectionRepository(self.conn)
        connection = connections.create_connection(
            actor=self.owner,
            platform="telegram",
            connection_type="telegram_managed_bot",
            external_account_id="700001",
            credential_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_OWNER_READ"
            ),
        )
        connections.activate_connection(
            actor=self.owner,
            connection_id=connection.id,
        )
        self.bot = connections.register_managed_bot(
            actor=self.owner,
            connection_id=connection.id,
            external_bot_id="700001",
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_OWNER_READ"
            ),
            username="practice_helper_bot",
            display_name="Помощник практики",
        )
        self.gateway = BotGatewayRepository(self.conn)
        self.route = self.gateway.resolve_telegram_route(external_bot_id="700001")
        self.repository = ManagedBotOwnerRepository(self.conn)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    @staticmethod
    def _payload(update_id: int) -> dict[str, object]:
        return {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 1_700_000_000,
                "chat": {"id": 5001, "type": "private"},
                "from": {"id": 5001, "is_bot": False, "first_name": "Иван"},
                "text": "/start",
            },
        }

    def _admit(self, update_id: int) -> str:
        admitted = self.gateway.admit_telegram_update(
            route=self.route,
            provider_update_id=update_id,
            payload=self._payload(update_id),
        )
        return admitted.event.id

    def test_snapshot_is_tenant_scoped_and_contains_only_safe_fields(self) -> None:
        event_ids = [self._admit(number) for number in range(1, 6)]
        timestamp = "2026-07-29T10:00:00+00:00"
        statuses = ["pending", "processing", "retry", "processed", "dead"]
        for event_id, status in zip(event_ids, statuses, strict=True):
            self.conn.execute(
                """
                UPDATE bot_gateway_ingress_events
                SET status=?, updated_at=?,
                    processed_at=CASE WHEN ?='processed' THEN ? ELSE NULL END,
                    dead_at=CASE WHEN ?='dead' THEN ? ELSE NULL END
                WHERE id=?
                """,
                (
                    status,
                    timestamp,
                    status,
                    timestamp,
                    status,
                    timestamp,
                    event_id,
                ),
            )
        snapshot = self.repository.snapshot(
            actor=self.owner,
            managed_bot_id=self.bot.id,
        )
        self.assertEqual(snapshot.bot_status, ManagedBotStatus.ACTIVE)
        self.assertEqual(snapshot.in_flight_events, 3)
        self.assertEqual(snapshot.pending_events, 1)
        self.assertEqual(snapshot.processing_events, 1)
        self.assertEqual(snapshot.retry_events, 1)
        self.assertEqual(snapshot.processed_events, 1)
        self.assertEqual(snapshot.dead_events, 1)
        self.assertEqual(snapshot.last_processed_at, timestamp)
        self.assertEqual(snapshot.last_dead_at, timestamp)
        self.assertNotIn("secret://", repr(snapshot))
        self.assertFalse(hasattr(snapshot, "credential_reference"))
        self.assertFalse(hasattr(snapshot, "webhook_secret_reference"))

        with self.assertRaises(ConnectionNotFound):
            self.repository.snapshot(
                actor=self.other_owner,
                managed_bot_id=self.bot.id,
            )

    def test_webhook_material_is_internal_and_tenant_scoped(self) -> None:
        material = self.repository.webhook_material(
            actor=self.owner,
            managed_bot_id=self.bot.id,
        )
        self.assertEqual(material.external_bot_id, "700001")
        self.assertTrue(material.credential_reference.startswith("secret://"))
        self.assertTrue(material.webhook_secret_reference.startswith("secret://"))
        with self.assertRaises(ConnectionNotFound):
            self.repository.webhook_material(
                actor=self.other_owner,
                managed_bot_id=self.bot.id,
            )


if __name__ == "__main__":
    unittest.main()
