from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.bot_gateway import ManagedBotRouteNotFound
from clientplatform.domain.connections import (
    ConnectionInvariantViolation,
    ConnectionNotFound,
    ManagedBotStatus,
)
from clientplatform.infrastructure import ConnectionRepository, TenancyRepository
from clientplatform.infrastructure.safe_bot_gateway_repository import BotGatewayRepository
from services.db.schema import (
    clientplatform_bot_gateway,
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_tenancy,
)


class ClientPlatformManagedBotLifecycleTests(unittest.TestCase):
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
        self.gateway = BotGatewayRepository(self.conn)
        self.first = self._register(
            external_bot_id="700001",
            suffix="FIRST",
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _register(self, *, external_bot_id: str, suffix: str):
        connection = self.connections.create_connection(
            actor=self.owner,
            platform="telegram",
            connection_type="telegram_managed_bot",
            external_account_id=external_bot_id,
            credential_reference=(
                f"secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_{suffix}"
            ),
        )
        connection = self.connections.activate_connection(
            actor=self.owner,
            connection_id=connection.id,
        )
        return self.connections.register_managed_bot(
            actor=self.owner,
            connection_id=connection.id,
            external_bot_id=external_bot_id,
            webhook_secret_reference=(
                f"secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_{suffix}"
            ),
        )

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

    def test_disable_allows_replacement_and_blocks_old_reactivation(self) -> None:
        disabled = self.connections.disable_managed_bot(
            actor=self.owner,
            managed_bot_id=self.first.id,
        )
        self.assertEqual(disabled.status, ManagedBotStatus.DISABLED)
        with self.assertRaises(ManagedBotRouteNotFound):
            self.gateway.resolve_telegram_route(
                external_bot_id=self.first.external_bot_id
            )

        replacement = self._register(external_bot_id="700002", suffix="SECOND")
        route = self.gateway.resolve_telegram_route(
            external_bot_id=replacement.external_bot_id
        )
        self.assertEqual(route.managed_bot_id, replacement.id)
        with self.assertRaises(ConnectionInvariantViolation):
            self.connections.activate_managed_bot(
                actor=self.owner,
                managed_bot_id=self.first.id,
            )

    def test_disable_terminates_queued_events_and_clears_payload(self) -> None:
        route = self.gateway.resolve_telegram_route(
            external_bot_id=self.first.external_bot_id
        )
        admitted = self.gateway.admit_telegram_update(
            route=route,
            provider_update_id=1,
            payload=self._payload(1),
        )
        self.assertIsNotNone(admitted.event.payload_json)
        self.connections.disable_managed_bot(
            actor=self.owner,
            managed_bot_id=self.first.id,
        )
        event = self.conn.execute(
            """
            SELECT status, payload_json, lock_token, last_error_code, dead_at
            FROM bot_gateway_ingress_events
            WHERE id=?
            """,
            (admitted.event.id,),
        ).fetchone()
        self.assertEqual(event["status"], "dead")
        self.assertIsNone(event["payload_json"])
        self.assertIsNone(event["lock_token"])
        self.assertEqual(event["last_error_code"], "managed_bot_disabled")
        self.assertIsNotNone(event["dead_at"])

    def test_disabled_bot_can_be_reactivated_when_no_replacement_exists(self) -> None:
        self.connections.disable_managed_bot(
            actor=self.owner,
            managed_bot_id=self.first.id,
        )
        active = self.connections.activate_managed_bot(
            actor=self.owner,
            managed_bot_id=self.first.id,
        )
        self.assertEqual(active.status, ManagedBotStatus.ACTIVE)
        route = self.gateway.resolve_telegram_route(
            external_bot_id=self.first.external_bot_id
        )
        self.assertEqual(route.managed_bot_id, self.first.id)

    def test_revoke_is_idempotent_and_permanent(self) -> None:
        revoked = self.connections.revoke_managed_bot(
            actor=self.owner,
            managed_bot_id=self.first.id,
        )
        repeated = self.connections.revoke_managed_bot(
            actor=self.owner,
            managed_bot_id=self.first.id,
        )
        self.assertEqual(revoked.status, ManagedBotStatus.REVOKED)
        self.assertEqual(repeated.status, ManagedBotStatus.REVOKED)
        with self.assertRaises(ConnectionNotFound):
            self.connections.activate_managed_bot(
                actor=self.owner,
                managed_bot_id=self.first.id,
            )
        connection = self.conn.execute(
            "SELECT status FROM connections WHERE id=?",
            (self.first.connection_id,),
        ).fetchone()
        self.assertEqual(connection["status"], "revoked")


if __name__ == "__main__":
    unittest.main()
