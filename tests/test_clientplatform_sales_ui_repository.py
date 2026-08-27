from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.sales import ContactBasis, SalesLeadStage, plan_sales_action
from clientplatform.domain.sales_handoff import (
    HandoffReason,
    HandoffSeverity,
    HandoffSignal,
)
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.commercial_ladder_repository import (
    CommercialLadderRepository,
)
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.sales_handoff_repository import SalesHandoffRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.sales_ui_repository import SalesUiRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_attribution,
    clientplatform_customers,
    clientplatform_offer_ladders,
    clientplatform_sales,
    clientplatform_tenancy,
)


class ClientPlatformSalesUiRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_sales.ensure(self.conn)
        clientplatform_attribution.ensure(self.conn)
        clientplatform_offer_ladders.ensure(self.conn)

        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.owner = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.customer = CustomerRepository(self.conn).create_customer(
            actor=self.owner,
            display_name="Анна",
        )
        self.sales = SalesRepository(self.conn)
        self.ui = SalesUiRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_open_work_is_plain_read_model_with_latest_plan(self) -> None:
        lead = self.sales.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="tg:anna",
            customer_id=self.customer.id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
        )
        lead = self.sales.set_stage(
            actor=self.owner,
            lead_id=lead.id,
            stage=SalesLeadStage.QUALIFIED,
        )
        plan = plan_sales_action(lead, model_confidence=0.95)
        self.sales.save_plan(actor=self.owner, plan=plan)

        items = self.ui.list_open_work(actor=self.owner)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["customer_name"], "Анна")
        self.assertEqual(items[0]["stage"], "qualified")
        self.assertEqual(items[0]["next_action_kind"], "present_offer")
        self.assertIn("assigned_member_id", items[0])
        self.assertIn("next_action", items[0])
        self.assertIn("due_at", items[0])
        self.assertIn("attribution_source", items[0])

    def test_won_or_lost_work_is_not_in_active_queue(self) -> None:
        lead = self.sales.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="web:closed",
            customer_id=self.customer.id,
            source_kind="website",
            contact_basis=ContactBasis.INBOUND,
        )
        self.sales.set_stage(
            actor=self.owner,
            lead_id=lead.id,
            stage=SalesLeadStage.WON,
        )
        self.assertEqual(self.ui.list_open_work(actor=self.owner), [])
        closed = self.ui.list_recent_closed(actor=self.owner)
        self.assertEqual(closed[0]["stage"], "won")
        self.assertEqual(closed[0]["closure_reason"], "won")

    def test_direct_work_item_lookup_is_not_limited_to_first_50(self) -> None:
        target = self.sales.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="tg:older-than-window",
            customer_id=self.customer.id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
        )
        self.conn.execute(
            "UPDATE clientplatform_sales_leads SET updated_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", target.id),
        )
        for index in range(50):
            self.sales.create_or_refresh_lead(
                actor=self.owner,
                opportunity_key=f"tg:window:{index}",
                customer_id=self.customer.id,
                source_kind="telegram",
                contact_basis=ContactBasis.INBOUND,
            )

        listed_ids = {
            str(item["id"])
            for item in self.ui.list_open_work(actor=self.owner, limit=50)
        }
        self.assertNotIn(target.id, listed_ids)

        item = self.ui.get_work_item(actor=self.owner, lead_id=target.id)

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item["id"], target.id)
        self.assertEqual(item["customer_name"], "Анна")
        self.assertEqual(item["stage"], "new")

    def test_handoff_projection_contains_customer_but_not_context_payload(self) -> None:
        lead = self.sales.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="tg:human",
            customer_id=self.customer.id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
        )
        handoff = SalesHandoffRepository(self.conn).open(
            actor=self.owner,
            lead_id=lead.id,
            signal=HandoffSignal(
                HandoffReason.EXPLICIT_REQUEST,
                HandoffSeverity.HIGH,
                "Customer wants a human.",
            ),
            context={"private_message": "do not surface this in queue"},
        )

        items = self.ui.list_handoff_work(actor=self.owner)

        self.assertEqual(items[0]["id"], handoff["id"])
        self.assertEqual(items[0]["customer_name"], "Анна")
        self.assertEqual(items[0]["reason"], "explicit_request")
        self.assertNotIn("context_json", items[0])
        self.assertNotIn("private_message", repr(items[0]))

    def test_ladder_projection_lists_only_active_tenant_ladders_and_steps(self) -> None:
        ladders = CommercialLadderRepository(self.conn)
        ladder_id = ladders.create_ladder(actor=self.owner, name="Основной путь")
        ladders.add_step(
            actor=self.owner,
            ladder_id=ladder_id,
            position=0,
            kind="diagnostic",
            title="Первая встреча",
            requires_human_approval=True,
        )

        listed = self.ui.list_ladders(actor=self.owner)
        steps = self.ui.list_ladder_steps(actor=self.owner, ladder_id=ladder_id)

        self.assertEqual(
            [(item["name"], item["step_count"]) for item in listed],
            [("Основной путь", 1)],
        )
        self.assertEqual(steps[0]["title"], "Первая встреча")
        self.assertEqual(steps[0]["kind"], "diagnostic")
        self.assertEqual(steps[0]["requires_human_approval"], 1)

    def test_cross_tenant_read_models_are_empty_and_ladder_id_is_rejected(self) -> None:
        lead = self.sales.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="tg:tenant-a",
            customer_id=self.customer.id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
        )
        ladder_id = CommercialLadderRepository(self.conn).create_ladder(
            actor=self.owner,
            name="Tenant A",
        )
        self.assertTrue(lead.id)

        tenancy = TenancyRepository(self.conn)
        other_access = tenancy.create_business(owner_user_id=202, name="Другая практика")
        other_owner = tenancy.resolve_context(
            user_id=202,
            business_id=other_access.business.id,
        )

        self.assertEqual(self.ui.list_open_work(actor=other_owner), [])
        self.assertIsNone(self.ui.get_work_item(actor=other_owner, lead_id=lead.id))
        self.assertEqual(self.ui.list_recent_closed(actor=other_owner), [])
        self.assertEqual(self.ui.list_handoff_work(actor=other_owner), [])
        self.assertEqual(self.ui.list_ladders(actor=other_owner), [])
        with self.assertRaisesRegex(ValueError, "active business"):
            self.ui.list_ladder_steps(actor=other_owner, ladder_id=ladder_id)

    def test_limits_fail_closed(self) -> None:
        for value in (0, -1, True):
            with self.assertRaisesRegex(ValueError, "positive integer"):
                self.ui.list_open_work(actor=self.owner, limit=value)
            with self.assertRaisesRegex(ValueError, "positive integer"):
                self.ui.list_recent_closed(actor=self.owner, limit=value)


if __name__ == "__main__":
    unittest.main()
