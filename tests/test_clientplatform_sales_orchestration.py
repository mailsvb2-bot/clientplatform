from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

from clientplatform.application import sales_orchestration
from clientplatform.domain.customers import CustomerPlatform
from clientplatform.domain.sales import ContactBasis, SalesActionKind, SalesInvariantViolation
from clientplatform.domain.sales_state_machine import SalesConversationEvent
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.commercial_ladder_repository import CommercialLadderRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.sales_action_repository import SalesActionRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_customers,
    clientplatform_offer_ladders,
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
    clientplatform_offer_ladders.ensure(conn)
    return conn


class ClientPlatformSalesOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _database()
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.owner = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        customers = CustomerRepository(self.conn)
        self.customer = customers.create_customer(actor=self.owner, display_name="Анна")
        customers.attach_identity(
            actor=self.owner,
            customer_id=self.customer.id,
            platform=CustomerPlatform.TELEGRAM,
            external_subject="202",
            username="anna",
            display_name="Анна",
        )
        self.sales = SalesRepository(self.conn)
        self.lead = self.sales.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="public-storefront:telegram:202",
            customer_id=self.customer.id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
            source_ref="public_storefront",
        )
        ladder = CommercialLadderRepository(self.conn)
        self.ladder_id = ladder.create_ladder(actor=self.owner, name="Основной путь")
        self.step = ladder.add_step(
            actor=self.owner,
            ladder_id=self.ladder_id,
            position=0,
            kind="diagnostic",
            title="Первая консультация",
            min_evidence_score=0.25,
            requires_human_approval=True,
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    @contextmanager
    def _use_db(self) -> Iterator[sqlite3.Connection]:
        yield self.conn
        self.conn.commit()

    def test_signal_closes_into_plan_candidate_and_replay_is_idempotent(self) -> None:
        first = sales_orchestration.orchestrate_sales_signal_in_transaction(
            conn=self.conn,
            actor=self.owner,
            lead_id=self.lead.id,
            event=SalesConversationEvent.INBOUND_RECEIVED,
            dedupe_key="storefront:202",
            model_confidence=0.96,
            unanswered_inbound=True,
            metadata={"channel": "telegram"},
        )
        replay = sales_orchestration.orchestrate_sales_signal_in_transaction(
            conn=self.conn,
            actor=self.owner,
            lead_id=self.lead.id,
            event=SalesConversationEvent.INBOUND_RECEIVED,
            dedupe_key="storefront:202",
            model_confidence=0.96,
            unanswered_inbound=True,
            metadata={"channel": "telegram"},
        )

        self.assertTrue(first.signal_applied)
        self.assertEqual(first.transition.current.value, "engaged")
        self.assertIsNotNone(first.plan)
        assert first.plan is not None
        self.assertEqual(first.plan.action_kind, SalesActionKind.RESPOND)
        self.assertTrue(first.plan.requires_approval)
        self.assertIsNotNone(first.plan_id)
        self.assertIsNone(first.handoff)
        self.assertIsNotNone(first.commercial_candidate)
        assert first.commercial_candidate is not None
        self.assertEqual(first.commercial_candidate.step_id, self.step.id)
        self.assertEqual(first.commercial_candidate.title, "Первая консультация")

        self.assertFalse(replay.signal_applied)
        self.assertIsNone(replay.plan)
        self.assertIsNone(replay.plan_id)
        self.assertIsNone(replay.handoff)
        self.assertIsNone(replay.commercial_candidate)

        plans = self.conn.execute(
            "SELECT id, action_kind, requires_approval, status FROM clientplatform_sales_action_plans"
        ).fetchall()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["action_kind"], "respond")
        self.assertEqual(plans[0]["requires_approval"], 1)
        self.assertEqual(plans[0]["status"], "planned")
        candidate_events = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM clientplatform_sales_events
            WHERE event_type='commercial_candidate_selected'
            """
        ).fetchone()[0]
        self.assertEqual(candidate_events, 1)

    def test_outbound_is_fail_closed_until_explicit_owner_approval(self) -> None:
        result = sales_orchestration.orchestrate_sales_signal_in_transaction(
            conn=self.conn,
            actor=self.owner,
            lead_id=self.lead.id,
            event=SalesConversationEvent.INBOUND_RECEIVED,
            dedupe_key="approval:202",
            model_confidence=0.99,
            unanswered_inbound=True,
        )
        assert result.plan_id is not None
        actions = SalesActionRepository(self.conn)

        with self.assertRaisesRegex(SalesInvariantViolation, "explicitly approved"):
            actions.authorize_outbound(actor=self.owner, plan_id=result.plan_id)

        with patch.object(sales_orchestration, "get_db", self._use_db):
            authorization = sales_orchestration.approve_and_authorize_sales_outbound(
                actor=self.owner,
                plan_id=result.plan_id,
            )

        approved = actions.get(actor=self.owner, plan_id=result.plan_id)
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(authorization["dispatch_allowed"])
        self.assertEqual(authorization["platform"], "telegram")
        self.assertEqual(authorization["external_subject"], "202")

        events = self.conn.execute(
            """
            SELECT event_type, COUNT(*) AS count
            FROM clientplatform_sales_events
            WHERE event_type IN ('sales_action_approved','sales_outbound_authorized')
            GROUP BY event_type
            """
        ).fetchall()
        self.assertEqual(
            {row["event_type"]: row["count"] for row in events},
            {"sales_action_approved": 1, "sales_outbound_authorized": 1},
        )

    def test_low_confidence_creates_persisted_handoff_and_no_candidate(self) -> None:
        result = sales_orchestration.orchestrate_sales_signal_in_transaction(
            conn=self.conn,
            actor=self.owner,
            lead_id=self.lead.id,
            event=SalesConversationEvent.INBOUND_RECEIVED,
            dedupe_key="low-confidence:202",
            model_confidence=0.40,
            unanswered_inbound=True,
            metadata={"message": "нужна проверка"},
        )

        self.assertTrue(result.signal_applied)
        self.assertIsNotNone(result.plan)
        assert result.plan is not None
        self.assertEqual(result.plan.action_kind, SalesActionKind.HUMAN_HANDOFF)
        self.assertFalse(result.plan.requires_approval)
        self.assertIsNotNone(result.handoff)
        assert result.handoff is not None
        self.assertEqual(result.handoff["reason"], "low_confidence")
        self.assertIsNone(result.commercial_candidate)

        handoffs = self.conn.execute(
            "SELECT COUNT(*) FROM clientplatform_sales_handoffs WHERE status='open'"
        ).fetchone()[0]
        self.assertEqual(handoffs, 1)


if __name__ == "__main__":
    unittest.main()
