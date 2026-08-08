from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.sales import (
    ContactBasis,
    SalesActionKind,
    SalesActionPlan,
    SalesInvariantViolation,
    SalesLeadStage,
)
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.sales_action_repository import SalesActionRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_customers,
    clientplatform_sales,
    clientplatform_tenancy,
)


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    clientplatform_tenancy.ensure(conn)
    clientplatform_customers.ensure(conn)
    clientplatform_activity.ensure(conn)
    clientplatform_sales.ensure(conn)
    return conn


class ClientPlatformSalesActionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _database()
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.owner = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        customer = CustomerRepository(self.conn).create_customer(
            actor=self.owner,
            display_name="Анна",
        )
        self.sales = SalesRepository(self.conn)
        self.lead = self.sales.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="telegram:anna",
            customer_id=customer.id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
        )
        self.actions = SalesActionRepository(self.conn)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _plan(self, action: SalesActionKind) -> SalesActionPlan:
        return SalesActionPlan(
            lead_id=self.lead.id,
            action_kind=action,
            rationale=f"test:{action.value}",
            requires_approval=True,
        )

    def test_new_plan_supersedes_previous_planned_or_approved_plan(self) -> None:
        first_id = self.sales.save_plan(
            actor=self.owner,
            plan=self._plan(SalesActionKind.RESPOND),
            now="2026-08-08T18:00:00+00:00",
        )
        self.actions.approve(
            actor=self.owner,
            plan_id=first_id,
            now="2026-08-08T18:01:00+00:00",
        )

        second_id = self.sales.save_plan(
            actor=self.owner,
            plan=self._plan(SalesActionKind.ASK_QUALIFICATION),
            now="2026-08-08T18:02:00+00:00",
        )

        first = self.actions.get(actor=self.owner, plan_id=first_id)
        second = self.actions.get(actor=self.owner, plan_id=second_id)
        self.assertEqual(first["status"], "dismissed")
        self.assertEqual(second["status"], "planned")
        with self.assertRaisesRegex(SalesInvariantViolation, "explicitly approved"):
            self.actions.authorize_outbound(actor=self.owner, plan_id=first_id)
        with self.assertRaisesRegex(SalesInvariantViolation, "cannot be approved"):
            self.actions.approve(actor=self.owner, plan_id=first_id)

    def test_closed_lead_cannot_approve_outbound_plan(self) -> None:
        plan_id = self.sales.save_plan(
            actor=self.owner,
            plan=self._plan(SalesActionKind.RESPOND),
        )
        self.sales.set_stage(
            actor=self.owner,
            lead_id=self.lead.id,
            stage=SalesLeadStage.WON,
        )

        with self.assertRaisesRegex(SalesInvariantViolation, "closed sales lead"):
            self.actions.approve(actor=self.owner, plan_id=plan_id)

    def test_approval_rechecks_contact_basis_fail_closed(self) -> None:
        customer = CustomerRepository(self.conn).create_customer(
            actor=self.owner,
            display_name="Без согласия",
        )
        lead = self.sales.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="manual:no-contact",
            customer_id=customer.id,
            source_kind="manual",
            contact_basis=ContactBasis.NONE,
        )
        plan_id = self.sales.save_plan(
            actor=self.owner,
            plan=SalesActionPlan(
                lead_id=lead.id,
                action_kind=SalesActionKind.RESPOND,
                rationale="manual-test",
                requires_approval=True,
            ),
        )

        with self.assertRaisesRegex(SalesInvariantViolation, "contact basis"):
            self.actions.approve(actor=self.owner, plan_id=plan_id)


if __name__ == "__main__":
    unittest.main()
