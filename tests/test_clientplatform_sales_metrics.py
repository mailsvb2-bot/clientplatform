from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.sales import ContactBasis, SalesLeadStage
from clientplatform.domain.sales_handoff import evaluate_handoff
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.sales_handoff_repository import SalesHandoffRepository
from clientplatform.infrastructure.sales_metrics_repository import SalesMetricsRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_customers,
    clientplatform_sales,
    clientplatform_tenancy,
)


class ClientPlatformSalesMetricsTests(unittest.TestCase):
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
        customer_repo = CustomerRepository(self.conn)
        sales = SalesRepository(self.conn)
        self.leads = []
        for index, (source, stage) in enumerate(
            (
                ("telegram", SalesLeadStage.NEW),
                ("telegram", SalesLeadStage.QUALIFIED),
                ("website", SalesLeadStage.CHECKOUT),
                ("website", SalesLeadStage.WON),
            ),
            start=1,
        ):
            customer = customer_repo.create_customer(
                actor=self.owner, display_name=f"Клиент {index}"
            )
            lead = sales.create_or_refresh_lead(
                actor=self.owner,
                opportunity_key=f"{source}:{index}",
                customer_id=customer.id,
                source_kind=source,
                contact_basis=ContactBasis.INBOUND,
            )
            if stage != SalesLeadStage.NEW:
                lead = sales.set_stage(
                    actor=self.owner, lead_id=lead.id, stage=stage
                )
            self.leads.append(lead)

    def tearDown(self) -> None:
        self.conn.close()


    def test_handoff_queue_does_not_invent_funnel_progress(self) -> None:
        customer = CustomerRepository(self.conn).create_customer(
            actor=self.owner, display_name="Только handoff"
        )
        lead = SalesRepository(self.conn).create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="handoff-only:1",
            customer_id=customer.id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
        )
        signal = evaluate_handoff(model_confidence=0.2)
        assert signal is not None
        SalesHandoffRepository(self.conn).open(
            actor=self.owner, lead_id=lead.id, signal=signal
        )
        snapshot = SalesMetricsRepository(self.conn).snapshot(actor=self.owner)
        self.assertEqual(snapshot.total.discovered, 5)
        self.assertEqual(snapshot.total.open_handoffs, 1)

    def test_snapshot_uses_business_evidence_and_source_breakdown(self) -> None:
        signal = evaluate_handoff(model_confidence=0.2)
        assert signal is not None
        SalesHandoffRepository(self.conn).open(
            actor=self.owner,
            lead_id=self.leads[1].id,
            signal=signal,
        )
        snapshot = SalesMetricsRepository(self.conn).snapshot(actor=self.owner)
        self.assertEqual(snapshot.total.discovered, 4)
        self.assertEqual(snapshot.total.qualified, 3)
        self.assertEqual(snapshot.total.checkout, 2)
        self.assertEqual(snapshot.total.won, 1)
        self.assertEqual(snapshot.total.open_handoffs, 1)
        self.assertEqual(snapshot.by_source["website"].won, 1)
        self.assertEqual(snapshot.total.win_percent, 25.0)

    def test_won_is_not_double_counted_as_lost(self) -> None:
        lead = self.leads[-1]
        SalesRepository(self.conn).record_event(
            actor=self.owner,
            lead_id=lead.id,
            event_type="conversation_transition",
            dedupe_key="legacy-lost-before-payment",
            payload={"from": "qualified", "event": "declined", "to": "lost"},
        )
        snapshot = SalesMetricsRepository(self.conn).snapshot(actor=self.owner)
        self.assertEqual(snapshot.total.won, 1)
        self.assertEqual(snapshot.total.lost, 0)


    def test_funnel_counts_reject_fractional_and_boolean_values(self) -> None:
        from clientplatform.domain.sales_metrics import SalesFunnelCounts

        for value in (-0.5, 1.5, True):
            with self.assertRaisesRegex(ValueError, "integer"):
                SalesFunnelCounts(discovered=value)  # type: ignore[arg-type]



if __name__ == "__main__":
    unittest.main()
