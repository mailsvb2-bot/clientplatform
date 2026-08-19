from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.sales import ContactBasis, SalesInvariantViolation, SalesLeadStage
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_customers,
    clientplatform_sales,
    clientplatform_tenancy,
)


class _StageRaceConnection:
    """Inject one competing stage write immediately before the repository CAS."""

    def __init__(self, conn: sqlite3.Connection, *, lead_id: str, business_id: str):
        self._conn = conn
        self._lead_id = lead_id
        self._business_id = business_id
        self.injected = False

    def execute(self, sql: str, params=()):
        normalized = " ".join(str(sql).split())
        if (
            not self.injected
            and normalized.startswith("UPDATE clientplatform_sales_leads")
            and "AND stage=?" in normalized
        ):
            self._conn.execute(
                """
                UPDATE clientplatform_sales_leads
                SET stage='won', closure_reason='competing payment',
                    last_signal_at='2026-08-19T13:00:00+00:00',
                    updated_at='2026-08-19T13:00:00+00:00'
                WHERE id=? AND business_id=? AND stage='new'
                """,
                (self._lead_id, self._business_id),
            )
            self.injected = True
        return self._conn.execute(sql, params)


class ClientPlatformSalesStageConcurrencyTests(unittest.TestCase):
    def test_stale_new_to_lost_cannot_overwrite_competing_new_to_won(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            clientplatform_tenancy.ensure(conn)
            clientplatform_customers.ensure(conn)
            clientplatform_activity.ensure(conn)
            clientplatform_sales.ensure(conn)

            tenancy = TenancyRepository(conn)
            access = tenancy.create_business(owner_user_id=101, name="Практика")
            owner = tenancy.resolve_context(
                user_id=101,
                business_id=access.business.id,
            )
            customer = CustomerRepository(conn).create_customer(
                actor=owner,
                display_name="Анна",
            )
            base_repo = SalesRepository(conn)
            lead = base_repo.create_or_refresh_lead(
                actor=owner,
                opportunity_key="web:stage-race",
                customer_id=customer.id,
                source_kind="website",
                source_ref="landing-main",
                contact_basis=ContactBasis.INBOUND,
                now="2026-08-19T12:00:00+00:00",
            )

            racing_conn = _StageRaceConnection(
                conn,
                lead_id=lead.id,
                business_id=owner.business_id,
            )
            racing_repo = SalesRepository(racing_conn)
            with self.assertRaisesRegex(
                SalesInvariantViolation,
                "changed concurrently",
            ):
                racing_repo.set_stage(
                    actor=owner,
                    lead_id=lead.id,
                    stage=SalesLeadStage.LOST,
                    reason="stale owner decision",
                    now="2026-08-19T13:00:01+00:00",
                )

            self.assertTrue(racing_conn.injected)
            current = base_repo.get_lead(actor=owner, lead_id=lead.id)
            self.assertIs(current.stage, SalesLeadStage.WON)
            self.assertEqual(current.closure_reason, "competing payment")
            stage_events = [
                event
                for event in base_repo.list_events(actor=owner, lead_id=lead.id)
                if event["event_type"] == "stage_changed"
            ]
            self.assertEqual(stage_events, [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
