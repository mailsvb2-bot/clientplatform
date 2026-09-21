from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import services.db.core as db_core
from clientplatform.application.customer_timeline import get_customer_timeline
from clientplatform.application.customers import create_customer
from clientplatform.application.external_products import record_external_observation_feedback
from clientplatform.application.tenancy import create_business, resolve_tenant_context
from clientplatform.application.ucr_attendance_pilot import sync_ucr_attendance_pilot
from clientplatform.domain.external_products import (
    ExternalObservationFeedback,
    ExternalProductIngressMode,
    ExternalProductInvariantViolation,
    ExternalProductNotFound,
)
from clientplatform.infrastructure.external_product_repository import ExternalProductRepository
from clientplatform.runtime.ucr_gateway import (
    UcrGatewayConfig,
    UcrGatewayConfigurationError,
    UcrGatewayClient,
)
from services.db import get_db
from services.schema import init_db


_REQUEST = {
    "scope": {"tenantId": {"value": "tenant-a"}},
    "conferenceId": {"value": "conference-a"},
    "integrationId": {"value": "integration-a"},
    "externalUserId": "cGlsb3QtdXNlci0x",
}


class _Gateway:
    def __init__(self, *, seconds: int = 60, joins: int = 1, callback=None):
        self.seconds = seconds
        self.joins = joins
        self.callback = callback
        self.calls = 0

    async def invoke_universal_conference(self, *, method, request):
        self.calls += 1
        if self.callback is not None:
            self.callback()
        if self.joins == 0:
            attendance = {
                "externalUserId": _REQUEST["externalUserId"],
                "totalConnectedSeconds": "0",
                "currentConnectedSeconds": "0",
                "joinCount": 0,
                "reconnectCount": 0,
                "mediaReadyCount": 0,
                "connected": False,
            }
        else:
            attendance = {
                "externalUserId": _REQUEST["externalUserId"],
                "firstJoinAtUnixMs": "1789912800000",
                "lastLeaveAtUnixMs": str(1789912800000 + self.seconds * 1000),
                "totalConnectedSeconds": str(self.seconds),
                "currentConnectedSeconds": "0",
                "joinCount": self.joins,
                "reconnectCount": max(0, self.joins - 1),
                "mediaReadyCount": 1,
                "connected": False,
            }
        return {"ok": True, "result": {"attendance": attendance}}


class UcrAttendancePilotSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._old_db_path = db_core.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory(prefix="clientplatform-ucr-pilot-")
        db_core.DB_PATH = Path(self._tmpdir.name) / "pilot.db"
        init_db()
        access = create_business(owner_user_id=98001, name="UCR pilot")
        self.actor = resolve_tenant_context(
            user_id=98001,
            business_id=access.business.id,
        )
        self.customer = create_customer(actor=self.actor, display_name="Анна")
        self.customer_ref = "ucr-pilot-customer-1"
        with get_db() as conn:
            repository = ExternalProductRepository(conn)
            pending = repository.create_connector(
                actor=self.actor,
                product_key="ucr_attendance",
                display_name="UCR attendance pilot",
                webhook_secret_reference=(
                    "secret://env/CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN"
                ),
                ingress_mode=ExternalProductIngressMode.TRUSTED_PULL,
                now=datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc),
            )
            self.connector = repository.activate_connector(
                actor=self.actor,
                connector_id=pending.id,
                now=datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc),
            )
            repository.bind_customer_ref(
                actor=self.actor,
                connector_id=self.connector.id,
                customer_id=self.customer.id,
                customer_ref=self.customer_ref,
                now=datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc),
            )

    def tearDown(self) -> None:
        db_core.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def _sync(self, gateway, *, received_second: int = 10):
        return await sync_ucr_attendance_pilot(
            actor=self.actor,
            connector_id=self.connector.id,
            customer_id=self.customer.id,
            customer_ref=self.customer_ref,
            canonical_request=_REQUEST,
            observation_key="ucr:conference-a:attendance",
            gateway=gateway,
            received_at=datetime(
                2026, 9, 21, 12, 0, received_second, tzinfo=timezone.utc
            ),
        )

    async def test_verified_attendance_persists_only_canonical_evidence_and_feedback(self) -> None:
        result = await self._sync(_Gateway(seconds=2520))
        self.assertTrue(result.persisted)
        self.assertIsNotNone(result.receipt)
        assert result.receipt is not None
        self.assertEqual(result.receipt.customer_id, self.customer.id)
        self.assertEqual(result.receipt.observation_revision, 1)
        self.assertIsNone(result.receipt.outcome_event_id)

        with get_db() as conn:
            outcomes = conn.execute(
                "SELECT COUNT(*) FROM business_outcome_events WHERE business_id=?",
                (self.actor.business_id,),
            ).fetchone()[0]
            sales_events = conn.execute(
                "SELECT COUNT(*) FROM clientplatform_sales_events WHERE business_id=?",
                (self.actor.business_id,),
            ).fetchone()[0]
            raw = conn.execute(
                """
                SELECT metadata_json
                FROM external_product_event_receipts
                WHERE id=? AND business_id=?
                """,
                (result.receipt.id, self.actor.business_id),
            ).fetchone()["metadata_json"]
        self.assertEqual(outcomes, 0)
        self.assertEqual(sales_events, 0)
        self.assertNotIn(self.customer_ref, raw)
        self.assertNotIn(_REQUEST["externalUserId"], raw)

        timeline = get_customer_timeline(
            actor=self.actor,
            customer_id=self.customer.id,
            now=datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc),
        )
        evidence = [
            item
            for item in timeline.entries
            if item.kind == "external_observation:ucr.conference_attendance"
        ]
        self.assertEqual(len(evidence), 1)
        self.assertIn("42 мин", evidence[0].title)
        self.assertEqual(evidence[0].evidence_quality, "проверено источником")

        record_external_observation_feedback(
            actor=self.actor,
            customer_id=self.customer.id,
            receipt_id=result.receipt.id,
            feedback=ExternalObservationFeedback.USEFUL,
        )
        timeline = get_customer_timeline(
            actor=self.actor,
            customer_id=self.customer.id,
            now=datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc),
        )
        evidence = [
            item
            for item in timeline.entries
            if item.kind == "external_observation:ucr.conference_attendance"
        ]
        self.assertEqual(evidence[0].evidence_feedback, "useful")

    async def test_same_source_snapshot_is_idempotent_without_fake_revision(self) -> None:
        first = await self._sync(_Gateway(seconds=60), received_second=10)
        second = await self._sync(_Gateway(seconds=60), received_second=20)
        self.assertTrue(first.persisted)
        self.assertFalse(second.persisted)
        assert first.receipt is not None
        assert second.receipt is not None
        self.assertEqual(second.receipt.id, first.receipt.id)
        with get_db() as conn:
            count = conn.execute(
                """
                SELECT COUNT(*) FROM external_product_event_receipts
                WHERE business_id=? AND connector_id=?
                  AND observation_key='ucr:conference-a:attendance'
                """,
                (self.actor.business_id, self.connector.id),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    async def test_changed_snapshot_continues_exact_revision_chain(self) -> None:
        first = await self._sync(_Gateway(seconds=60), received_second=10)
        second = await self._sync(_Gateway(seconds=120, joins=2), received_second=20)
        assert first.receipt is not None
        assert second.receipt is not None
        self.assertEqual(first.receipt.observation_revision, 1)
        self.assertEqual(second.receipt.observation_revision, 2)
        self.assertEqual(
            second.receipt.observation_supersedes_external_event_id,
            first.receipt.external_event_id,
        )

    async def test_zero_attendance_does_not_invent_negative_observation(self) -> None:
        result = await self._sync(_Gateway(seconds=0, joins=0))
        self.assertFalse(result.persisted)
        self.assertIsNone(result.receipt)
        with get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM external_product_event_receipts WHERE business_id=?",
                (self.actor.business_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    async def test_disabled_gateway_fails_before_receipt(self) -> None:
        gateway = UcrGatewayClient(
            config=UcrGatewayConfig(
                enabled=False,
                base_url="",
                token_reference="secret://env/CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN",
            )
        )
        with self.assertRaises(UcrGatewayConfigurationError):
            await self._sync(gateway)
        with get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM external_product_event_receipts WHERE business_id=?",
                (self.actor.business_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    async def test_trusted_pull_connector_cannot_be_used_as_public_webhook_ingress(self) -> None:
        with get_db() as conn:
            repository = ExternalProductRepository(conn)
            with self.assertRaisesRegex(
                ExternalProductNotFound,
                "does not accept webhooks",
            ):
                repository.get_active_for_ingress(connector_id=self.connector.id)

    async def test_wrong_customer_fails_before_ucr_call(self) -> None:
        other = create_customer(actor=self.actor, display_name="Борис")
        gateway = _Gateway(seconds=60)
        with self.assertRaises(ExternalProductInvariantViolation):
            await sync_ucr_attendance_pilot(
                actor=self.actor,
                connector_id=self.connector.id,
                customer_id=other.id,
                customer_ref=self.customer_ref,
                canonical_request=_REQUEST,
                observation_key="ucr:conference-a:attendance",
                gateway=gateway,
            )
        self.assertEqual(gateway.calls, 0)

    async def test_disable_during_read_fails_closed_before_persistence(self) -> None:
        def disable_connector() -> None:
            with get_db() as conn:
                ExternalProductRepository(conn).disable_connector(
                    actor=self.actor,
                    connector_id=self.connector.id,
                    now=datetime(2026, 9, 21, 12, 0, 5, tzinfo=timezone.utc),
                )

        with self.assertRaises(ExternalProductNotFound):
            await self._sync(_Gateway(seconds=60, callback=disable_connector))
        with get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM external_product_event_receipts WHERE business_id=?",
                (self.actor.business_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
