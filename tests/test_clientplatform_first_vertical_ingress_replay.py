from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clientplatform.domain.connections import ConnectionPlatform, ConnectionType
from clientplatform.infrastructure import ConnectionRepository, TenancyRepository
from clientplatform.infrastructure.safe_bot_gateway_repository import (
    BotGatewayRepository,
)
from services.db.schema import (
    clientplatform_bot_gateway,
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_tenancy,
)


class ClientPlatformFirstVerticalIngressReplayE2E(unittest.TestCase):
    def _open(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        clientplatform_tenancy.ensure(conn)
        clientplatform_customers.ensure(conn)
        clientplatform_connections.ensure(conn)
        clientplatform_bot_gateway.ensure(conn)

    def _managed_route(
        self,
        conn: sqlite3.Connection,
        *,
        owner: Any,
        external_bot_id: str,
        suffix: str,
    ) -> Any:
        connections = ConnectionRepository(conn)
        connection = connections.create_connection(
            actor=owner,
            platform=ConnectionPlatform.TELEGRAM,
            connection_type=ConnectionType.TELEGRAM_MANAGED_BOT,
            external_account_id=external_bot_id,
            credential_reference=(
                f"secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_{suffix}"
            ),
            permissions=("send_message", "send_media"),
            now="2026-07-30T13:00:00+00:00",
        )
        connection = connections.activate_connection(
            actor=owner,
            connection_id=connection.id,
            now="2026-07-30T13:00:00+00:00",
        )
        managed = connections.register_managed_bot(
            actor=owner,
            connection_id=connection.id,
            external_bot_id=external_bot_id,
            webhook_secret_reference=(
                f"secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_{suffix}"
            ),
            username=f"clientplatform_{suffix.lower()}_bot",
            display_name=f"ClientPlatform {suffix}",
            now="2026-07-30T13:00:00+00:00",
        )
        return BotGatewayRepository(conn).resolve_telegram_route(
            external_bot_id=managed.external_bot_id
        )

    @staticmethod
    def _payload(update_id: int) -> dict[str, Any]:
        return {
            "update_id": update_id,
            "message": {
                "message_id": 77,
                "date": 1_775_000_000,
                "chat": {
                    "id": 5001,
                    "type": "private",
                    "first_name": "Анна",
                    "last_name": "Клиент",
                    "username": "client",
                },
                "from": {
                    "id": 5001,
                    "is_bot": False,
                    "first_name": "Анна",
                    "last_name": "Клиент",
                    "username": "client",
                    "language_code": "ru",
                },
                "text": "/start",
            },
        }

    def test_provider_update_replay_is_single_event_across_restart_and_tenants(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="clientplatform-ingress-replay-") as raw:
            database = Path(raw) / "gateway.sqlite3"
            conn = self._open(database)
            self._ensure_schema(conn)

            tenancy = TenancyRepository(conn)
            access_a = tenancy.create_business(
                owner_user_id=101,
                name="Практика Марии",
            )
            access_b = tenancy.create_business(
                owner_user_id=202,
                name="Школа Нины",
            )
            owner_a = tenancy.resolve_context(
                user_id=101,
                business_id=access_a.business.id,
            )
            owner_b = tenancy.resolve_context(
                user_id=202,
                business_id=access_b.business.id,
            )
            route_a = self._managed_route(
                conn,
                owner=owner_a,
                external_bot_id="710001",
                suffix="MARIA_REPLAY",
            )
            route_b = self._managed_route(
                conn,
                owner=owner_b,
                external_bot_id="710002",
                suffix="NINA_REPLAY",
            )

            gateway = BotGatewayRepository(conn)
            update_id = 9_100_001
            payload = self._payload(update_id)
            admitted_at = datetime(2026, 7, 30, 13, 1, tzinfo=timezone.utc)
            first = gateway.admit_telegram_update(
                route=route_a,
                provider_update_id=update_id,
                payload=payload,
                now=admitted_at,
            )
            replay_before_processing = gateway.admit_telegram_update(
                route=route_a,
                provider_update_id=update_id,
                payload=payload,
                now=admitted_at,
            )
            self.assertFalse(first.duplicate)
            self.assertTrue(replay_before_processing.duplicate)
            self.assertEqual(first.event.id, replay_before_processing.event.id)

            claimed_a = gateway.claim_due(limit=10, now=admitted_at)
            self.assertEqual(len(claimed_a), 1)
            self.assertEqual(claimed_a[0].event.id, first.event.id)
            customer_a = gateway.ensure_telegram_customer_link(
                route=claimed_a[0].route,
                telegram_user_id=5001,
                username="client",
                display_name="Анна Клиент",
                now=admitted_at,
            )
            processed_a = gateway.mark_processed(
                claimed_a[0],
                now=datetime(2026, 7, 30, 13, 1, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(processed_a.status.value, "processed")
            self.assertIsNone(processed_a.payload_json)
            conn.commit()

            conn.close()
            conn = self._open(database)
            gateway = BotGatewayRepository(conn)
            route_a = gateway.resolve_telegram_route(external_bot_id="710001")
            replay_after_restart = gateway.admit_telegram_update(
                route=route_a,
                provider_update_id=update_id,
                payload=payload,
                now=datetime(2026, 7, 30, 13, 2, tzinfo=timezone.utc),
            )
            self.assertTrue(replay_after_restart.duplicate)
            self.assertEqual(replay_after_restart.event.id, first.event.id)
            self.assertEqual(replay_after_restart.event.status.value, "processed")
            self.assertEqual(gateway.claim_due(limit=10), [])
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM bot_gateway_ingress_events "
                    "WHERE managed_bot_id=? AND provider_update_id=?",
                    (route_a.managed_bot_id, str(update_id)),
                ).fetchone()[0],
                1,
            )

            route_b = gateway.resolve_telegram_route(external_bot_id="710002")
            second_tenant = gateway.admit_telegram_update(
                route=route_b,
                provider_update_id=update_id,
                payload=payload,
                now=datetime(2026, 7, 30, 13, 3, tzinfo=timezone.utc),
            )
            self.assertFalse(second_tenant.duplicate)
            self.assertNotEqual(second_tenant.event.id, first.event.id)
            self.assertNotEqual(second_tenant.event.business_id, first.event.business_id)

            claimed_b = gateway.claim_due(
                limit=10,
                now=datetime(2026, 7, 30, 13, 3, tzinfo=timezone.utc),
            )
            self.assertEqual(len(claimed_b), 1)
            self.assertEqual(claimed_b[0].event.id, second_tenant.event.id)
            customer_b = gateway.ensure_telegram_customer_link(
                route=claimed_b[0].route,
                telegram_user_id=5001,
                username="client",
                display_name="Анна Клиент",
                now=datetime(2026, 7, 30, 13, 3, tzinfo=timezone.utc),
            )
            gateway.mark_processed(
                claimed_b[0],
                now=datetime(2026, 7, 30, 13, 3, 1, tzinfo=timezone.utc),
            )
            conn.commit()

            self.assertNotEqual(customer_a.customer_id, customer_b.customer_id)
            self.assertNotEqual(customer_a.business_id, customer_b.business_id)
            rows = conn.execute(
                """
                SELECT business_id,provider_update_id,status,payload_json
                FROM bot_gateway_ingress_events
                ORDER BY business_id
                """
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["provider_update_id"] for row in rows}, {str(update_id)})
            self.assertEqual({row["status"] for row in rows}, {"processed"})
            self.assertTrue(all(row["payload_json"] is None for row in rows))
            conn.close()


if __name__ == "__main__":
    unittest.main()
