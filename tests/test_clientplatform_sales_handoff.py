from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.sales import ContactBasis
from clientplatform.domain.sales_handoff import HandoffReason, evaluate_handoff
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.sales_handoff_repository import SalesHandoffRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_customers,
    clientplatform_sales,
    clientplatform_tenancy,
)


class ClientPlatformSalesHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_sales.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.owner = tenancy.resolve_context(
            user_id=101, business_id=access.business.id
        )
        customer = CustomerRepository(self.conn).create_customer(
            actor=self.owner, display_name="Клиент"
        )
        self.lead = SalesRepository(self.conn).create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="telegram:handoff-1",
            customer_id=customer.id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
        )
        self.handoffs = SalesHandoffRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_sensitive_context_has_precedence_and_preserves_context(self) -> None:
        signal = evaluate_handoff(
            model_confidence=0.99,
            explicit_human_request=True,
            sensitive_context=True,
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.reason, HandoffReason.SENSITIVE_CONTEXT)
        item = self.handoffs.open(
            actor=self.owner,
            lead_id=self.lead.id,
            signal=signal,
            context={"last_customer_message": "Нужен человек"},
        )
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["context"]["last_customer_message"], "Нужен человек")

    def test_open_is_idempotent_while_handoff_is_active(self) -> None:
        signal = evaluate_handoff(model_confidence=0.4)
        assert signal is not None
        first = self.handoffs.open(
            actor=self.owner, lead_id=self.lead.id, signal=signal
        )
        second = self.handoffs.open(
            actor=self.owner, lead_id=self.lead.id, signal=signal
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.handoffs.list_open(actor=self.owner)), 1)

    def test_repeated_claimed_handoff_refreshes_context_without_downgrade(self) -> None:
        urgent = evaluate_handoff(model_confidence=0.99, sensitive_context=True)
        normal = evaluate_handoff(model_confidence=0.4)
        assert urgent is not None and normal is not None
        first = self.handoffs.open(
            actor=self.owner,
            lead_id=self.lead.id,
            signal=urgent,
            context={"last_customer_message": "Первый снимок"},
            now="2026-08-08T10:00:00+00:00",
        )
        claimed = self.handoffs.claim(
            actor=self.owner,
            handoff_id=str(first["id"]),
            now="2026-08-08T10:01:00+00:00",
        )
        refreshed = self.handoffs.open(
            actor=self.owner,
            lead_id=self.lead.id,
            signal=normal,
            context={"last_customer_message": "Свежий снимок"},
            now="2026-08-08T10:02:00+00:00",
        )

        self.assertEqual(refreshed["id"], claimed["id"])
        self.assertEqual(refreshed["status"], "claimed")
        self.assertEqual(refreshed["reason"], urgent.reason.value)
        self.assertEqual(refreshed["severity"], urgent.severity.value)
        self.assertEqual(refreshed["summary"], urgent.summary)
        self.assertEqual(refreshed["context"]["last_customer_message"], "Свежий снимок")
        self.assertEqual(refreshed["updated_at"], "2026-08-08T10:02:00+00:00")
        self.assertEqual(len(self.handoffs.list_open(actor=self.owner)), 1)

    def test_claim_and_resolve_are_explicit(self) -> None:
        signal = evaluate_handoff(model_confidence=0.4)
        assert signal is not None
        item = self.handoffs.open(
            actor=self.owner, lead_id=self.lead.id, signal=signal
        )
        claimed = self.handoffs.claim(
            actor=self.owner, handoff_id=str(item["id"])
        )
        self.assertEqual(claimed["status"], "claimed")
        resolved = self.handoffs.resolve(
            actor=self.owner, handoff_id=str(item["id"])
        )
        self.assertEqual(resolved["status"], "resolved")

    def test_non_finite_confidence_and_negative_attempts_fail_closed(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaisesRegex(ValueError, "finite"):
                evaluate_handoff(model_confidence=value)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            evaluate_handoff(model_confidence=0.9, failed_attempts=-1)

    def test_more_severe_signal_upgrades_active_handoff(self) -> None:
        normal = evaluate_handoff(model_confidence=0.4)
        urgent = evaluate_handoff(model_confidence=0.99, sensitive_context=True)
        assert normal is not None and urgent is not None
        first = self.handoffs.open(
            actor=self.owner, lead_id=self.lead.id, signal=normal, context={"v": 1}
        )
        upgraded = self.handoffs.open(
            actor=self.owner, lead_id=self.lead.id, signal=urgent, context={"v": 2}
        )
        self.assertEqual(first["id"], upgraded["id"])
        self.assertEqual(upgraded["severity"], "urgent")
        self.assertEqual(upgraded["context"]["v"], 2)

    def test_claim_assigns_member_to_sales_lead(self) -> None:
        signal = evaluate_handoff(model_confidence=0.4)
        assert signal is not None
        item = self.handoffs.open(actor=self.owner, lead_id=self.lead.id, signal=signal)
        self.handoffs.claim(actor=self.owner, handoff_id=str(item["id"]))
        lead = SalesRepository(self.conn).get_lead(actor=self.owner, lead_id=self.lead.id)
        self.assertEqual(lead.assigned_member_id, self.owner.membership_id)

    def test_handoff_rejects_fractional_attempts_and_non_boolean_flags(self) -> None:
        with self.assertRaisesRegex(ValueError, "failed_attempts"):
            evaluate_handoff(model_confidence=0.9, failed_attempts=1.5)
        with self.assertRaisesRegex(ValueError, "explicit_human_request"):
            evaluate_handoff(model_confidence=0.9, explicit_human_request="false")

    def test_handoff_context_rejects_non_finite_json_numbers(self) -> None:
        signal = evaluate_handoff(model_confidence=0.1)
        self.assertIsNotNone(signal)
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaisesRegex(ValueError, "JSON serializable"):
                self.handoffs.open(
                    actor=self.owner,
                    lead_id=self.lead.id,
                    signal=signal,
                    context={"score": value},
                )


if __name__ == "__main__":
    unittest.main()
