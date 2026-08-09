from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.sales import (
    ContactBasis,
    SalesActionKind,
    SalesActionPlan,
)
from clientplatform.domain.sales_handoff import evaluate_handoff
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


class _RecordingConnection:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, sql: str, params=()):
        self.statements.append(" ".join(str(sql).split()))
        return self._conn.execute(sql, params)


class ClientPlatformSalesReviewRegressionTests(unittest.TestCase):
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
            user_id=101,
            business_id=access.business.id,
        )
        customer = CustomerRepository(self.conn).create_customer(
            actor=self.owner,
            display_name="Клиент",
        )
        self.lead = SalesRepository(self.conn).create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="telegram:review-regressions",
            customer_id=customer.id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
            now="2026-08-09T10:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_replan_locks_lead_before_dismissing_active_plan(self) -> None:
        recording = _RecordingConnection(self.conn)
        repository = SalesRepository(recording)
        first = SalesActionPlan(
            lead_id=self.lead.id,
            action_kind=SalesActionKind.RESPOND,
            rationale="first",
            requires_approval=True,
        )
        second = SalesActionPlan(
            lead_id=self.lead.id,
            action_kind=SalesActionKind.ASK_QUALIFICATION,
            rationale="second",
            requires_approval=True,
        )

        repository.save_plan(
            actor=self.owner,
            plan=first,
            now="2026-08-09T10:01:00+00:00",
        )
        recording.statements.clear()
        second_id = repository.save_plan(
            actor=self.owner,
            plan=second,
            now="2026-08-09T10:02:00+00:00",
        )

        lead_lock = next(
            index
            for index, statement in enumerate(recording.statements)
            if "UPDATE clientplatform_sales_leads SET updated_at=updated_at" in statement
        )
        dismiss = next(
            index
            for index, statement in enumerate(recording.statements)
            if "UPDATE clientplatform_sales_action_plans SET status='dismissed'" in statement
        )
        self.assertLess(lead_lock, dismiss)

        active = self.conn.execute(
            """
            SELECT id, status
            FROM clientplatform_sales_action_plans
            WHERE business_id=? AND lead_id=? AND status IN ('planned','approved')
            """,
            (self.owner.business_id, self.lead.id),
        ).fetchall()
        self.assertEqual([(row["id"], row["status"]) for row in active], [(second_id, "planned")])

    def test_omitted_handoff_context_preserves_snapshot_but_explicit_empty_clears(self) -> None:
        signal = evaluate_handoff(model_confidence=0.4)
        assert signal is not None
        repository = SalesHandoffRepository(self.conn)
        first = repository.open(
            actor=self.owner,
            lead_id=self.lead.id,
            signal=signal,
            context={"last_customer_message": "Сохранить"},
            now="2026-08-09T11:00:00+00:00",
        )
        repeated = repository.open(
            actor=self.owner,
            lead_id=self.lead.id,
            signal=signal,
            context=None,
            now="2026-08-09T11:01:00+00:00",
        )
        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(
            repeated["context"],
            {"last_customer_message": "Сохранить"},
        )
        self.assertEqual(repeated["updated_at"], "2026-08-09T11:01:00+00:00")

        cleared = repository.open(
            actor=self.owner,
            lead_id=self.lead.id,
            signal=signal,
            context={},
            now="2026-08-09T11:02:00+00:00",
        )
        self.assertEqual(cleared["context"], {})


if __name__ == "__main__":
    unittest.main()
