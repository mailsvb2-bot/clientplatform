from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from clientplatform.application.control import _managed_telegram_connection
from clientplatform.domain.bot_gateway import (
    BotGatewayAdmissionRejected,
    BotGatewayReplayConflict,
    IngressEventStatus,
)
from clientplatform.domain.connections import ConnectionPlatform, ConnectionType
from clientplatform.infrastructure import ConnectionRepository, TenancyRepository
from clientplatform.infrastructure.bot_gateway_repository import BotGatewayRepository
from services.db.schema import (
    clientplatform_bot_gateway,
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_tenancy,
)


class ClientPlatformBotGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        clientplatform_bot_gateway.ensure(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        self.connections = ConnectionRepository(self.conn)
        self.gateway = BotGatewayRepository(self.conn)
        self.owner, self.route = self._create_business_route(
            owner_user_id=101,
            name="Практика",
            external_bot_id="700001",
            secret_suffix="PRACTICE",
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _create_business_route(
        self,
        *,
        owner_user_id: int,
        name: str,
        external_bot_id: str,
        secret_suffix: str,
    ):
        access = self.tenancy.create_business(
            owner_user_id=owner_user_id,
            name=name,
        )
        owner = self.tenancy.resolve_context(
            user_id=owner_user_id,
            business_id=access.business.id,
        )
        connection = self.connections.create_connection(
            actor=owner,
            platform=ConnectionPlatform.TELEGRAM,
            connection_type=ConnectionType.TELEGRAM_MANAGED_BOT,
            external_account_id=external_bot_id,
            credential_reference=(
                f"secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_{secret_suffix}"
            ),
            permissions=("send_message", "send_media"),
        )
        connection = self.connections.activate_connection(
            actor=owner,
            connection_id=connection.id,
        )
        managed = self.connections.register_managed_bot(
            actor=owner,
            connection_id=connection.id,
            external_bot_id=external_bot_id,
            webhook_secret_reference=(
                f"secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_{secret_suffix}"
            ),
            username=f"bot_{secret_suffix.lower()}",
            display_name=name,
        )
        route = self.gateway.resolve_telegram_route(
            external_bot_id=managed.external_bot_id
        )
        return owner, route

    def _payload(self, update_id: int, *, text: str = "/start") -> dict[str, object]:
        return {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 1_700_000_000,
                "chat": {"id": 5001, "type": "private"},
                "from": {
                    "id": 5001,
                    "is_bot": False,
                    "first_name": "Иван",
                    "username": "ivan",
                },
                "text": text,
            },
        }

    def test_route_is_global_and_secret_reference_only(self) -> None:
        self.assertEqual(self.route.external_bot_id, "700001")
        self.assertEqual(self.route.business_id, self.owner.business_id)
        self.assertTrue(self.route.credential_reference.startswith("secret://env/"))
        self.assertTrue(self.route.webhook_secret_reference.startswith("secret://env/"))

        other = self.tenancy.create_business(owner_user_id=202, name="Школа")
        owner = self.tenancy.resolve_context(
            user_id=202,
            business_id=other.business.id,
        )
        connection = self.connections.create_connection(
            actor=owner,
            platform="telegram",
            connection_type="telegram_managed_bot",
            external_account_id="700001",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_SCHOOL",
        )
        self.connections.activate_connection(actor=owner, connection_id=connection.id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connections.register_managed_bot(
                actor=owner,
                connection_id=connection.id,
                external_bot_id="700001",
                webhook_secret_reference=(
                    "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_SCHOOL"
                ),
            )

    def test_one_active_managed_telegram_bot_per_business(self) -> None:
        connection = self.connections.create_connection(
            actor=self.owner,
            platform="telegram",
            connection_type="telegram_managed_bot",
            external_account_id="700002",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_SECOND",
        )
        self.connections.activate_connection(
            actor=self.owner,
            connection_id=connection.id,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connections.register_managed_bot(
                actor=self.owner,
                connection_id=connection.id,
                external_bot_id="700002",
                webhook_secret_reference=(
                    "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_SECOND"
                ),
            )

    def test_replay_is_idempotent_and_conflicting_payload_fails_closed(self) -> None:
        first = self.gateway.admit_telegram_update(
            route=self.route,
            provider_update_id=1,
            payload=self._payload(1),
        )
        duplicate = self.gateway.admit_telegram_update(
            route=self.route,
            provider_update_id=1,
            payload=self._payload(1),
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(first.event.id, duplicate.event.id)

        with self.assertRaises(BotGatewayReplayConflict):
            self.gateway.admit_telegram_update(
                route=self.route,
                provider_update_id=1,
                payload=self._payload(1, text="другой payload"),
            )

    def test_rate_and_queue_limits_are_per_bot(self) -> None:
        now = datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc)
        self.gateway.admit_telegram_update(
            route=self.route,
            provider_update_id=1,
            payload=self._payload(1),
            per_minute_limit=1,
            now=now,
        )
        with self.assertRaises(BotGatewayAdmissionRejected):
            self.gateway.admit_telegram_update(
                route=self.route,
                provider_update_id=2,
                payload=self._payload(2),
                per_minute_limit=1,
                now=now,
            )

        _, second_route = self._create_business_route(
            owner_user_id=303,
            name="Студия",
            external_bot_id="700003",
            secret_suffix="STUDIO",
        )
        admitted = self.gateway.admit_telegram_update(
            route=second_route,
            provider_update_id=2,
            payload=self._payload(2),
            per_minute_limit=1,
            queue_limit=1,
            now=now,
        )
        self.assertFalse(admitted.duplicate)
        with self.assertRaises(BotGatewayAdmissionRejected):
            self.gateway.admit_telegram_update(
                route=second_route,
                provider_update_id=3,
                payload=self._payload(3),
                per_minute_limit=10,
                queue_limit=1,
                now=now,
            )

    def test_claim_process_clears_payload_and_dead_event_does_not_block_fleet(self) -> None:
        self.gateway.admit_telegram_update(
            route=self.route,
            provider_update_id=10,
            payload=self._payload(10),
        )
        _, second_route = self._create_business_route(
            owner_user_id=404,
            name="Кабинет",
            external_bot_id="700004",
            secret_suffix="CABINET",
        )
        self.gateway.admit_telegram_update(
            route=second_route,
            provider_update_id=11,
            payload=self._payload(11),
        )
        claimed = self.gateway.claim_due(limit=2)
        self.assertEqual(len(claimed), 2)

        first = self.gateway.reschedule(
            claimed[0],
            error_code="handler_failed",
            max_attempts=1,
        )
        second = self.gateway.mark_processed(claimed[1])
        self.assertEqual(first.status, IngressEventStatus.DEAD)
        self.assertIsNone(first.payload_json)
        self.assertEqual(second.status, IngressEventStatus.PROCESSED)
        self.assertIsNone(second.payload_json)
        snapshot = self.gateway.health_snapshot()
        self.assertEqual(snapshot["dead"], 1)
        self.assertEqual(snapshot["processed"], 1)
        self.assertEqual(snapshot["active_bots"], 2)

    def test_managed_bot_start_creates_one_business_scoped_customer(self) -> None:
        first = self.gateway.ensure_telegram_customer_link(
            route=self.route,
            telegram_user_id=5001,
            username="ivan",
            display_name="Иван Иванов",
        )
        second = self.gateway.ensure_telegram_customer_link(
            route=self.route,
            telegram_user_id=5001,
            username="ivan",
            display_name="Иван Иванов",
        )
        self.assertEqual(first, second)
        self.assertEqual(first.business_id, self.owner.business_id)
        count = self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM customer_identities
            WHERE business_id=? AND platform='telegram' AND external_subject='5001'
            """,
            (self.owner.business_id,),
        ).fetchone()
        self.assertEqual(int(count["c"]), 1)

    def test_initial_delivery_prefers_managed_bot_connection(self) -> None:
        selected = _managed_telegram_connection(
            actor=self.owner,
            repository=self.connections,
            conn=self.conn,
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.id, self.route.connection_id)
        self.assertEqual(selected.connection_type, ConnectionType.TELEGRAM_MANAGED_BOT)


if __name__ == "__main__":
    unittest.main()
