from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.application.external_products import (
    ingest_external_product_webhook,
    parse_external_product_event,
    verify_and_activate_external_product_connector,
    verify_external_product_signature,
)
from clientplatform.runtime.external_product_http import external_product_event_webhook
from clientplatform.domain.attribution import AcquisitionSource
from clientplatform.domain.external_products import (
    ExternalProductAcquisition,
    ExternalProductEvent,
    ExternalProductEventType,
    ExternalProductInvariantViolation,
    ExternalProductSignatureError,
)
from clientplatform.domain.outcomes import OutcomeMoney
from clientplatform.infrastructure.attribution_repository import AttributionRepository
from clientplatform.infrastructure.external_product_repository import ExternalProductRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_attribution,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_external_products,
    clientplatform_outcomes,
    clientplatform_promotions,
    clientplatform_revenue_attribution,
    clientplatform_tenancy,
)


class _CredentialProvider:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def resolve(self, reference: str) -> str:
        if reference != "secret://env/CLIENTPLATFORM_SECRET_EXTERNAL_TEST":
            raise AssertionError("unexpected secret reference")
        return self.secret


@contextmanager
def _db_context(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except (sqlite3.Error, ValueError, RuntimeError):
        conn.rollback()
        raise


class ExternalProductFixture:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_bookings.ensure(self.conn)
        clientplatform_outcomes.ensure(self.conn)
        clientplatform_promotions.ensure(self.conn)
        clientplatform_attribution.ensure(self.conn)
        clientplatform_revenue_attribution.ensure(self.conn)
        clientplatform_external_products.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=9191, name="External product")
        self.actor = tenancy.resolve_context(
            user_id=9191,
            business_id=access.business.id,
        )
        self.repo = ExternalProductRepository(self.conn)
        pending = self.repo.create_connector(
            actor=self.actor,
            product_key="external_test",
            display_name="External Test",
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_EXTERNAL_TEST"
            ),
            now=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
        )
        self.connector = self.repo.activate_connector(
            actor=self.actor,
            connector_id=pending.id,
            now=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class ClientPlatformExternalProductConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = ExternalProductFixture()
        self.now = datetime(2026, 8, 28, 16, 5, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.fx.close()

    def _lead_event(self) -> ExternalProductEvent:
        return ExternalProductEvent(
            external_event_id="lead-42",
            event_type=ExternalProductEventType.LEAD_CREATED,
            occurred_at=self.now,
            customer_ref="raw-external-user-42",
            acquisition=ExternalProductAcquisition(
                source=AcquisitionSource.PARTNER,
                source_key="corpclub26:wellbeing",
            ),
            metadata={"channel": "telegram"},
        )

    def test_connector_activation_requires_resolvable_32_byte_secret(self) -> None:
        pending = self.fx.repo.create_connector(
            actor=self.fx.actor,
            product_key="second_external",
            display_name="Second External",
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_EXTERNAL_TEST"
            ),
            now=self.now,
        )
        self.fx.conn.commit()
        with patch(
            "clientplatform.application.external_products.get_db",
            side_effect=lambda: _db_context(self.fx.conn),
        ):
            with self.assertRaisesRegex(
                ExternalProductInvariantViolation, "at least 32 bytes"
            ):
                verify_and_activate_external_product_connector(
                    actor=self.fx.actor,
                    connector_id=pending.id,
                    credential_provider=_CredentialProvider("short"),
                )
            active = verify_and_activate_external_product_connector(
                actor=self.fx.actor,
                connector_id=pending.id,
                credential_provider=_CredentialProvider("z" * 48),
            )
        self.assertEqual(active.status.value, "active")

    def test_signed_webhook_rejects_tamper_and_expired_replay(self) -> None:
        secret = "s" * 48
        body = b'{"version":1}'
        timestamp = str(int(self.now.timestamp()))
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        verify_external_product_signature(
            secret=secret,
            timestamp_header=timestamp,
            signature_header=signature,
            body=body,
            now=self.now,
        )
        with self.assertRaises(ExternalProductSignatureError):
            verify_external_product_signature(
                secret=secret,
                timestamp_header=timestamp,
                signature_header=signature,
                body=body + b" ",
                now=self.now,
            )
        with self.assertRaisesRegex(
            ExternalProductSignatureError,
            "timestamp_expired",
        ):
            verify_external_product_signature(
                secret=secret,
                timestamp_header=timestamp,
                signature_header=signature,
                body=body,
                now=self.now + timedelta(minutes=6),
            )

    def test_event_parser_is_strict_about_tenant_and_money_fields(self) -> None:
        payload = {
            "version": 1,
            "event_id": "evt-1",
            "type": "lead_created",
            "occurred_at": self.now.isoformat(),
            "customer_ref": "u-1",
            "business_id": self.fx.actor.business_id,
        }
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            parse_external_product_event(json.dumps(payload).encode("utf-8"))
        payload.pop("business_id")
        payload["amount_minor"] = 100
        with self.assertRaisesRegex(ValueError, "amount_minor and currency"):
            parse_external_product_event(json.dumps(payload).encode("utf-8"))

    def test_reserved_metadata_cannot_override_canonical_fields(self) -> None:
        payload = {
            "version": 1,
            "event_id": "evt-reserved",
            "type": "lead_created",
            "occurred_at": self.now.isoformat(),
            "customer_ref": "u-reserved",
            "metadata": {"external_product_key": "spoofed"},
        }
        with self.assertRaisesRegex(ValueError, "reserved key"):
            parse_external_product_event(json.dumps(payload).encode("utf-8"))

    def test_anonymous_evidence_does_not_create_fake_customer(self) -> None:
        event = ExternalProductEvent(
            external_event_id="choice:anon-1",
            event_type=ExternalProductEventType.EVIDENCE,
            occurred_at=datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc),
            customer_ref=None,
            metadata={"messenger": "max"},
        )
        receipt = self.fx.repo.ingest_event(
            connector=self.fx.connector,
            event=event,
            payload_fingerprint=hashlib.sha256(b"choice:anon-1").hexdigest(),
            received_at=datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc),
        )
        self.assertIsNone(receipt.customer_id)
        self.assertIsNone(receipt.customer_fingerprint)
        self.assertIsNone(receipt.outcome_event_id)
        count = self.fx.conn.execute(
            "SELECT COUNT(*) FROM customers WHERE business_id=?",
            (self.fx.actor.business_id,),
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_customer_linked_event_requires_customer_ref(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires customer_ref"):
            ExternalProductEvent(
                external_event_id="lead:no-customer",
                event_type=ExternalProductEventType.LEAD_CREATED,
                occurred_at=datetime(2026, 8, 28, 15, 1, tzinfo=timezone.utc),
                customer_ref=None,
            )

    def test_lead_creates_pseudonymous_customer_outcome_and_first_touch(self) -> None:
        event = self._lead_event()
        receipt = self.fx.repo.ingest_event(
            connector=self.fx.connector,
            event=event,
            payload_fingerprint="a" * 64,
            received_at=self.now,
        )
        self.assertIsNotNone(receipt.outcome_event_id)
        self.assertNotIn(event.customer_ref, receipt.customer_fingerprint)
        identity = self.fx.conn.execute(
            """
            SELECT external_subject FROM customer_identities
            WHERE business_id=? AND customer_id=? AND platform='internal'
            """,
            (self.fx.actor.business_id, receipt.customer_id),
        ).fetchone()
        self.assertIsNotNone(identity)
        self.assertNotIn(event.customer_ref, identity["external_subject"])
        outcome = self.fx.conn.execute(
            "SELECT outcome_type,customer_id FROM business_outcome_events WHERE id=?",
            (receipt.outcome_event_id,),
        ).fetchone()
        self.assertEqual(outcome["outcome_type"], "lead_created")
        self.assertEqual(outcome["customer_id"], receipt.customer_id)
        trace = AttributionRepository(self.fx.conn).get_customer_trace(
            business_id=self.fx.actor.business_id,
            customer_id=receipt.customer_id,
        )
        self.assertIsNotNone(trace)
        self.assertEqual(trace.identity.source, AcquisitionSource.PARTNER)
        self.assertEqual(trace.identity.source_ref_type, "external_product")
        self.assertIsNone(trace.identity.promotion_campaign_id)

    def test_event_id_is_idempotent_and_conflicting_reuse_is_rejected(self) -> None:
        event = self._lead_event()
        first = self.fx.repo.ingest_event(
            connector=self.fx.connector,
            event=event,
            payload_fingerprint="b" * 64,
            received_at=self.now,
        )
        second = self.fx.repo.ingest_event(
            connector=self.fx.connector,
            event=event,
            payload_fingerprint="b" * 64,
            received_at=self.now + timedelta(seconds=1),
        )
        self.assertEqual(first.id, second.id)
        count = self.fx.conn.execute(
            "SELECT COUNT(*) FROM business_outcome_events WHERE business_id=?",
            (self.fx.actor.business_id,),
        ).fetchone()[0]
        self.assertEqual(count, 1)
        with self.assertRaisesRegex(
            ExternalProductInvariantViolation,
            "different payload",
        ):
            self.fx.repo.ingest_event(
                connector=self.fx.connector,
                event=event,
                payload_fingerprint="c" * 64,
                received_at=self.now,
            )

    def test_payment_and_refund_inherit_external_first_touch(self) -> None:
        lead = self.fx.repo.ingest_event(
            connector=self.fx.connector,
            event=self._lead_event(),
            payload_fingerprint="d" * 64,
            received_at=self.now,
        )
        paid_event = ExternalProductEvent(
            external_event_id="pay-42",
            event_type=ExternalProductEventType.ORDER_PAID,
            occurred_at=self.now + timedelta(minutes=1),
            customer_ref="raw-external-user-42",
            subject_ref="order-42",
            money=OutcomeMoney(amount_minor=49000, currency="RUB"),
            metadata={"provider": "external-test"},
        )
        paid = self.fx.repo.ingest_event(
            connector=self.fx.connector,
            event=paid_event,
            payload_fingerprint="e" * 64,
            received_at=self.now + timedelta(minutes=1),
        )
        revenue = self.fx.conn.execute(
            """
            SELECT source,amount_minor,currency FROM revenue_attributions
            WHERE business_id=? AND outcome_event_id=?
            """,
            (self.fx.actor.business_id, paid.outcome_event_id),
        ).fetchone()
        self.assertEqual(revenue["source"], "partner")
        self.assertEqual(revenue["amount_minor"], 49000)
        self.assertEqual(revenue["currency"], "RUB")

        refund_event = ExternalProductEvent(
            external_event_id="refund-42",
            event_type=ExternalProductEventType.REFUND_RECORDED,
            occurred_at=self.now + timedelta(minutes=2),
            customer_ref="raw-external-user-42",
            related_event_id="pay-42",
            money=OutcomeMoney(amount_minor=10000, currency="RUB"),
        )
        refund = self.fx.repo.ingest_event(
            connector=self.fx.connector,
            event=refund_event,
            payload_fingerprint="f" * 64,
            received_at=self.now + timedelta(minutes=2),
        )
        refund_revenue = self.fx.conn.execute(
            """
            SELECT source,amount_minor,currency FROM revenue_attributions
            WHERE business_id=? AND outcome_event_id=?
            """,
            (self.fx.actor.business_id, refund.outcome_event_id),
        ).fetchone()
        self.assertEqual(refund_revenue["source"], "partner")
        self.assertEqual(refund_revenue["amount_minor"], -10000)
        self.assertEqual(lead.customer_id, paid.customer_id)
        self.assertEqual(paid.customer_id, refund.customer_id)

    def test_refund_rejects_customer_mismatch_with_referenced_payment(self) -> None:
        paid = ExternalProductEvent(
            external_event_id="pay-customer-a",
            event_type=ExternalProductEventType.ORDER_PAID,
            occurred_at=self.now,
            customer_ref="customer-a",
            money=OutcomeMoney(amount_minor=1200, currency="USD"),
        )
        self.fx.repo.ingest_event(
            connector=self.fx.connector,
            event=paid,
            payload_fingerprint="2" * 64,
            received_at=self.now,
        )
        refund = ExternalProductEvent(
            external_event_id="refund-customer-b",
            event_type=ExternalProductEventType.REFUND_RECORDED,
            occurred_at=self.now + timedelta(minutes=1),
            customer_ref="customer-b",
            related_event_id="pay-customer-a",
            money=OutcomeMoney(amount_minor=100, currency="USD"),
        )
        with self.assertRaisesRegex(
            ExternalProductInvariantViolation,
            "refund customer must match",
        ):
            self.fx.repo.ingest_event(
                connector=self.fx.connector,
                event=refund,
                payload_fingerprint="3" * 64,
                received_at=self.now + timedelta(minutes=1),
            )

    def test_refund_rejects_currency_mismatch_with_referenced_payment(self) -> None:
        paid = ExternalProductEvent(
            external_event_id="pay-usd",
            event_type=ExternalProductEventType.ORDER_PAID,
            occurred_at=self.now,
            customer_ref="customer-a",
            money=OutcomeMoney(amount_minor=1200, currency="USD"),
        )
        self.fx.repo.ingest_event(
            connector=self.fx.connector,
            event=paid,
            payload_fingerprint="4" * 64,
            received_at=self.now,
        )
        refund = ExternalProductEvent(
            external_event_id="refund-eur",
            event_type=ExternalProductEventType.REFUND_RECORDED,
            occurred_at=self.now + timedelta(minutes=1),
            customer_ref="customer-a",
            related_event_id="pay-usd",
            money=OutcomeMoney(amount_minor=100, currency="EUR"),
        )
        with self.assertRaisesRegex(
            ExternalProductInvariantViolation,
            "refund currency must match",
        ):
            self.fx.repo.ingest_event(
                connector=self.fx.connector,
                event=refund,
                payload_fingerprint="5" * 64,
                received_at=self.now + timedelta(minutes=1),
            )

    def test_external_acquisition_source_key_limit_matches_attribution_boundary(self) -> None:
        acquisition = ExternalProductAcquisition(
            source=AcquisitionSource.PARTNER,
            source_key="x" * 200,
        )
        self.assertEqual(len(acquisition.source_key), 200)
        with self.assertRaisesRegex(ValueError, "1..200"):
            ExternalProductAcquisition(
                source=AcquisitionSource.PARTNER,
                source_key="x" * 201,
            )

    def test_refund_must_reference_accepted_payment_event(self) -> None:
        event = ExternalProductEvent(
            external_event_id="refund-orphan",
            event_type=ExternalProductEventType.REFUND_RECORDED,
            occurred_at=self.now,
            customer_ref="u-orphan",
            related_event_id="missing-payment",
            money=OutcomeMoney(amount_minor=100, currency="RUB"),
        )
        with self.assertRaisesRegex(
            ExternalProductInvariantViolation,
            "must reference an accepted order_paid",
        ):
            self.fx.repo.ingest_event(
                connector=self.fx.connector,
                event=event,
                payload_fingerprint="1" * 64,
                received_at=self.now,
            )

    def test_application_ingress_uses_connector_route_not_payload_business_id(self) -> None:
        secret = "x" * 48
        payload = {
            "version": 1,
            "event_id": "ingress-42",
            "type": "evidence",
            "occurred_at": self.now.isoformat(),
            "customer_ref": "ingress-user",
            "metadata": {"messenger": "vk"},
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        timestamp = str(int(self.now.timestamp()))
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        with patch(
            "clientplatform.application.external_products.get_db",
            side_effect=lambda: _db_context(self.fx.conn),
        ):
            receipt = ingest_external_product_webhook(
                connector_id=self.fx.connector.id,
                timestamp_header=timestamp,
                signature_header=signature,
                body=body,
                credential_provider=_CredentialProvider(secret),
                now=self.now,
            )
        self.assertEqual(receipt.event_type, ExternalProductEventType.EVIDENCE)
        self.assertIsNone(receipt.outcome_event_id)
        stored = self.fx.conn.execute(
            "SELECT COUNT(*) FROM external_product_event_receipts WHERE business_id=?",
            (self.fx.actor.business_id,),
        ).fetchone()[0]
        self.assertEqual(stored, 1)


class ClientPlatformExternalProductHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_ingestion_is_offloaded_from_event_loop(self) -> None:
        request = SimpleNamespace(
            headers={},
            match_info={"connector_id": "connector-id"},
            read=AsyncMock(return_value=b"{}"),
        )
        receipt = SimpleNamespace(
            id="receipt-id",
            external_event_id="event-id",
            outcome_event_id="outcome-id",
        )
        offload = AsyncMock(return_value=receipt)
        with patch(
            "clientplatform.runtime.external_product_http.asyncio.to_thread",
            new=offload,
        ):
            response = await external_product_event_webhook(request)
        self.assertEqual(response.status, 200)
        offload.assert_awaited_once()
        self.assertEqual(offload.await_args.args[0].__name__, "ingest_external_product_webhook")
        self.assertEqual(offload.await_args.kwargs["connector_id"], "connector-id")



if __name__ == "__main__":
    unittest.main()
