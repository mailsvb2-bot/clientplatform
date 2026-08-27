from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import services.db.core as db_core
from clientplatform.application.customers import create_customer
from clientplatform.application.growth_cockpit import get_growth_cockpit
from clientplatform.application.tenancy import create_business, resolve_tenant_context
from clientplatform.domain.revenue_attribution import RevenueAttributionModel, UnitEconomicsSnapshot
from clientplatform.domain.sales import ContactBasis
from clientplatform.infrastructure.sales_repository import SalesRepository
from services.db import get_db
from services.schema import init_db


class ClientPlatformOwnerActionQueueM4003Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_db_path = db_core.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory(prefix="clientplatform-m4003-")
        db_core.DB_PATH = Path(self._tmpdir.name) / "owner-actions.db"
        init_db()

        a = create_business(owner_user_id=991001, name="Tenant A")
        b = create_business(owner_user_id=991002, name="Tenant B")
        self.actor_a = resolve_tenant_context(user_id=991001, business_id=a.business.id)
        self.actor_b = resolve_tenant_context(user_id=991002, business_id=b.business.id)
        customer = create_customer(actor=self.actor_a, display_name="Анна A")
        with get_db() as conn:
            sales = SalesRepository(conn)
            lead = sales.create_or_refresh_lead(
                actor=self.actor_a,
                opportunity_key="m4003:tenant-a",
                customer_id=customer.id,
                source_kind="telegram",
                contact_basis=ContactBasis.INBOUND,
                now="2026-08-27T09:00:00+00:00",
            )
            sales.set_next_action(
                actor=self.actor_a,
                lead_id=lead.id,
                next_action="Позвонить клиенту A",
                due_at="2026-08-27T12:00:00+00:00",
                now="2026-08-27T09:01:00+00:00",
            )
        self.lead_id = lead.id

    def tearDown(self) -> None:
        db_core.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    @staticmethod
    def _economics(*, actor, occurred_from, occurred_to, verified_spend=None):
        if verified_spend is not None:
            raise AssertionError("owner action queue must not invent spend")
        return UnitEconomicsSnapshot(
            business_id=actor.business_id,
            model_version=RevenueAttributionModel.FIRST_TOUCH_V1,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            leads=0,
            qualified_leads=0,
            bookings=0,
            paid_customers=0,
            monetary_outcomes=0,
            attributed_monetary_outcomes=0,
            unattributed_monetary_outcomes=0,
            revenue_by_currency=(),
            spend=None,
            cpl_minor=None,
            cost_per_booking_minor=None,
            cac_minor=None,
            roas_basis_points=None,
            limitations=(),
            source_breakdown={},
        )

    def _snapshot(self, actor):
        with (
            patch(
                "clientplatform.application.growth_cockpit.get_business_profile",
                return_value=SimpleNamespace(timezone="UTC"),
            ),
            patch(
                "clientplatform.application.growth_cockpit.get_business_unit_economics",
                side_effect=self._economics,
            ),
        ):
            return get_growth_cockpit(
                actor=actor,
                period_days=7,
                advertising_loader=lambda **_kwargs: None,
            )

    def test_queue_reads_only_active_tenant_sales_facts(self) -> None:
        tenant_a = self._snapshot(self.actor_a)
        tenant_b = self._snapshot(self.actor_b)

        self.assertEqual(len(tenant_a.actions), 1)
        self.assertEqual(tenant_a.actions[0].source, "sales_lead")
        self.assertEqual(tenant_a.actions[0].source_id, self.lead_id)
        self.assertEqual(tenant_a.actions[0].action_key, f"sales_lead:{self.lead_id}")
        self.assertIn("Позвонить клиенту A", tenant_a.actions[0].reason)

        self.assertEqual(tenant_b.actions, ())
        self.assertEqual(tenant_b.next_action.action_key, "none")
        self.assertNotIn("Анна A", tenant_b.next_action.title)

    def test_projection_is_read_only_and_replay_stable(self) -> None:
        with get_db() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM clientplatform_sales_events WHERE business_id=?",
                (self.actor_a.business_id,),
            ).fetchone()[0]

        first = self._snapshot(self.actor_a)
        second = self._snapshot(self.actor_a)

        self.assertEqual(first.actions, second.actions)
        with get_db() as conn:
            after = conn.execute(
                "SELECT COUNT(*) FROM clientplatform_sales_events WHERE business_id=?",
                (self.actor_a.business_id,),
            ).fetchone()[0]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
