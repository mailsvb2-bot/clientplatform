from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.sales import (
    ContactBasis,
    SalesActionKind,
    SalesInvariantViolation,
    SalesLeadStage,
    plan_sales_action,
)
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_customers,
    clientplatform_sales,
    clientplatform_tenancy,
)


class ClientPlatformSalesAgentTests(unittest.TestCase):
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
        self.customer = CustomerRepository(self.conn).create_customer(
            actor=self.owner, display_name="Клиент"
        )
        self.repo = SalesRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_no_contact_basis_never_proposes_cold_outbound(self) -> None:
        lead = self.repo.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="web:lead-1",
            customer_id=self.customer.id,
            source_kind="website",
            contact_basis=ContactBasis.NONE,
        )
        plan = plan_sales_action(lead, model_confidence=0.99)
        self.assertEqual(plan.action_kind, SalesActionKind.NOOP)
        self.assertEqual(plan.rationale, "no_contact_basis")

    def test_inbound_and_low_confidence_paths_are_explicit(self) -> None:
        lead = self.repo.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="tg:lead-2",
            customer_id=self.customer.id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
        )
        response = plan_sales_action(
            lead, model_confidence=0.93, unanswered_inbound=True
        )
        self.assertEqual(response.action_kind, SalesActionKind.RESPOND)
        self.assertTrue(response.requires_approval)

        handoff = plan_sales_action(lead, model_confidence=0.41)
        self.assertEqual(handoff.action_kind, SalesActionKind.HUMAN_HANDOFF)
        self.assertFalse(handoff.requires_approval)


    def test_inbound_basis_does_not_mask_qualified_stage(self) -> None:
        lead = self.repo.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="tg:qualified-inbound",
            customer_id=self.customer.id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
        )
        lead = self.repo.set_stage(
            actor=self.owner, lead_id=lead.id, stage=SalesLeadStage.QUALIFIED
        )
        plan = plan_sales_action(lead, model_confidence=0.95)
        self.assertEqual(plan.action_kind, SalesActionKind.PRESENT_OFFER)

    def test_opportunity_key_cannot_be_rebound_to_another_customer(self) -> None:
        self.repo.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="web:stable-opportunity",
            customer_id=self.customer.id,
            source_kind="website",
            contact_basis=ContactBasis.INBOUND,
        )
        other_customer = CustomerRepository(self.conn).create_customer(
            actor=self.owner, display_name="Другой клиент"
        )
        with self.assertRaisesRegex(
            SalesInvariantViolation, "another customer"
        ):
            self.repo.create_or_refresh_lead(
                actor=self.owner,
                opportunity_key="web:stable-opportunity",
                customer_id=other_customer.id,
                source_kind="website",
                contact_basis=ContactBasis.INBOUND,
            )

    def test_sales_action_boolean_inputs_fail_closed(self) -> None:
        lead = self.repo.create_or_refresh_lead(
            actor=self.owner, opportunity_key="tg:bool-boundary",
            customer_id=self.customer.id, source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
        )
        with self.assertRaisesRegex(ValueError, "unanswered_inbound"):
            plan_sales_action(lead, model_confidence=0.9, unanswered_inbound="false")

    def test_model_confidence_must_be_finite(self) -> None:
        lead = self.repo.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="web:finite-confidence",
            customer_id=self.customer.id,
            source_kind="website",
            contact_basis=ContactBasis.INBOUND,
        )
        with self.assertRaisesRegex(ValueError, "must be finite"):
            plan_sales_action(lead, model_confidence=float("nan"))

    def test_archived_customer_cannot_receive_new_sales_opportunity(self) -> None:
        self.conn.execute(
            "UPDATE customers SET status='archived' WHERE id=? AND business_id=?",
            (self.customer.id, self.owner.business_id),
        )
        with self.assertRaisesRegex(SalesInvariantViolation, "active customer"):
            self.repo.create_or_refresh_lead(
                actor=self.owner,
                opportunity_key="web:archived-customer",
                customer_id=self.customer.id,
                source_kind="website",
                contact_basis=ContactBasis.INBOUND,
            )

    def test_sales_event_payload_is_bounded_and_json_only(self) -> None:
        lead = self.repo.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="web:event-boundary",
            customer_id=self.customer.id,
            source_kind="website",
            contact_basis=ContactBasis.INBOUND,
        )
        with self.assertRaisesRegex(ValueError, "JSON serializable"):
            self.repo.record_event(
                actor=self.owner,
                lead_id=lead.id,
                event_type="signal",
                dedupe_key="signal:non-json",
                payload={"bad": object()},
            )
        with self.assertRaisesRegex(ValueError, "too large"):
            self.repo.record_event(
                actor=self.owner,
                lead_id=lead.id,
                event_type="signal",
                dedupe_key="signal:too-large",
                payload={"text": "x" * (33 * 1024)},
            )

    def test_cross_tenant_lead_is_not_readable(self) -> None:
        lead = self.repo.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="tg:lead-3",
            customer_id=self.customer.id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
        )
        tenancy = TenancyRepository(self.conn)
        other = tenancy.create_business(owner_user_id=202, name="Другой")
        other_owner = tenancy.resolve_context(
            user_id=202, business_id=other.business.id
        )
        with self.assertRaisesRegex(RuntimeError, "active business"):
            self.repo.get_lead(actor=other_owner, lead_id=lead.id)

    def test_action_plan_normalizes_kind_and_rejects_non_boolean_approval(self) -> None:
        from clientplatform.domain.sales import SalesActionPlan

        lead = self.repo.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="tg:plan-boundary",
            customer_id=self.customer.id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
        )
        plan = SalesActionPlan(
            lead_id=lead.id,
            action_kind="respond",
            rationale="reply",
            requires_approval=True,
        )
        self.assertEqual(plan.action_kind, SalesActionKind.RESPOND)
        with self.assertRaisesRegex(ValueError, "requires_approval"):
            SalesActionPlan(
                lead_id=lead.id,
                action_kind=SalesActionKind.RESPOND,
                rationale="reply",
                requires_approval=1,
            )

    def test_event_payload_rejects_non_finite_json_numbers(self) -> None:
        lead = self.repo.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="web:nonfinite-json",
            customer_id=self.customer.id,
            source_kind="website",
            contact_basis=ContactBasis.INBOUND,
        )
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaisesRegex(ValueError, "JSON serializable"):
                self.repo.record_event(
                    actor=self.owner,
                    lead_id=lead.id,
                    event_type="provider_evidence",
                    dedupe_key=f"nonfinite:{value!r}",
                    payload={"score": value},
                )




if __name__ == "__main__":
    unittest.main()
