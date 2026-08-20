from __future__ import annotations

import sqlite3
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application.retention import (
    RetentionCandidateUnavailable,
    prepare_reactivation_sales_lead,
)
from clientplatform.domain.outcomes import BusinessOutcomeEvent, OutcomeMoney, OutcomeSource, OutcomeType
from clientplatform.domain.retention import RetentionCohort
from clientplatform.domain.sales import ContactBasis, SalesLeadStage
from clientplatform.infrastructure import ConnectionRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import create_or_update_tables


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


class ClientPlatformRetentionU010ActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        create_or_update_tables(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        access = self.tenancy.create_business(owner_user_id=3101, name="Retention Actions")
        self.owner = self.tenancy.resolve_context(user_id=3101, business_id=access.business.id)
        self.customers = CustomerRepository(self.conn)
        self.outcomes = OutcomeRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _candidate(self, *, name: str = "Анна", inactive_days: int = 100) -> str:
        occurred_at = NOW - timedelta(days=inactive_days)
        customer = self.customers.create_customer(
            actor=self.owner,
            display_name=name,
            now=occurred_at.isoformat(),
        )
        self.conn.execute(
            "UPDATE customers SET first_contact_at=?,last_contact_at=? WHERE id=? AND business_id=?",
            (occurred_at.isoformat(), occurred_at.isoformat(), customer.id, self.owner.business_id),
        )
        self.outcomes.append(
            BusinessOutcomeEvent(
                id=str(uuid4()),
                business_id=self.owner.business_id,
                outcome_type=OutcomeType.ORDER_PAID,
                occurred_at=occurred_at,
                source=OutcomeSource(source_type="test_payment", source_id=f"payment:{customer.id}"),
                customer_id=customer.id,
                subject_ref=None,
                money=OutcomeMoney(amount_minor=1000_00, currency="RUB"),
                idempotency_key=f"u010-action-payment:{customer.id}",
                metadata={},
                metadata_version=1,
                created_at=occurred_at,
            )
        )
        return customer.id

    def _route(self, customer_id: str, *, platform: str = "telegram") -> None:
        self.customers.attach_identity(
            actor=self.owner,
            customer_id=customer_id,
            platform=platform,
            external_subject="73101001",
            now=(NOW - timedelta(days=100)).isoformat(),
        )
        connections = ConnectionRepository(self.conn)
        connection_types = {
            "telegram": "telegram_shared_bot",
            "vk": "vk_community",
            "max": "max_shared_bot",
        }
        connection = connections.create_connection(
            actor=self.owner,
            platform=platform,
            connection_type=connection_types[platform],
            external_account_id=f"u010-{platform}",
            credential_reference=f"secret://clientplatform/u010/{platform}",
            permissions=("send_messages",),
        )
        connections.activate_connection(actor=self.owner, connection_id=connection.id)

    def _prepare(self, customer_id: str, cohort: RetentionCohort):
        with patch(
            "clientplatform.application.retention.get_db",
            side_effect=lambda: nullcontext(self.conn),
        ):
            return prepare_reactivation_sales_lead(
                actor=self.owner,
                customer_id=customer_id,
                expected_cohort=cohort,
                now=NOW,
            )

    def test_owner_approval_materializes_canonical_sales_lead_without_sending(self) -> None:
        customer_id = self._candidate()
        self._route(customer_id)

        prepared = self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)

        self.assertEqual(prepared.route_platform, "telegram")
        self.assertEqual(prepared.lead.customer_id, customer_id)
        self.assertEqual(prepared.lead.source_kind, "telegram")
        self.assertEqual(prepared.lead.source_ref, "reactivation:inactive_customer")
        self.assertEqual(prepared.lead.contact_basis, ContactBasis.EXISTING_CUSTOMER)
        self.assertEqual(prepared.lead.stage, SalesLeadStage.NEW)
        self.assertIn("возврата клиента", prepared.lead.next_action or "")
        event = self.conn.execute(
            "SELECT payload_json FROM clientplatform_sales_events WHERE lead_id=? AND event_type='reactivation_review_approved'",
            (prepared.lead.id,),
        ).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM clientplatform_sales_followups").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM provider_dispatch_outbox").fetchone()[0],
            0,
        )

    def test_repeated_approval_is_idempotent_for_same_evidence_cycle(self) -> None:
        customer_id = self._candidate()
        self._route(customer_id)
        first = self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)
        second = self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)
        self.assertEqual(second.lead.id, first.lead.id)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM clientplatform_sales_events WHERE lead_id=? AND event_type='reactivation_review_approved'",
                (first.lead.id,),
            ).fetchone()[0],
            1,
        )

    def test_suppressed_route_falls_back_to_manual_owner_work(self) -> None:
        customer_id = self._candidate()
        self._route(customer_id)
        self.conn.execute(
            """
            INSERT INTO clientplatform_sales_contact_suppressions(
                business_id,customer_id,platform,reason,updated_by_member_id,created_at,updated_at
            ) VALUES(?,?,?,'opt_out',?,?,?)
            """,
            (
                self.owner.business_id,
                customer_id,
                "telegram",
                self.owner.membership_id,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        prepared = self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)
        self.assertIsNone(prepared.route_platform)
        self.assertEqual(prepared.lead.source_kind, "manual")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM provider_dispatch_outbox").fetchone()[0],
            0,
        )

    def test_stale_or_wrong_cohort_approval_fails_closed(self) -> None:
        customer_id = self._candidate(inactive_days=40)
        with self.assertRaisesRegex(RetentionCandidateUnavailable, "refresh"):
            self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM clientplatform_sales_leads").fetchone()[0],
            0,
        )

    def test_cross_tenant_customer_cannot_be_materialized(self) -> None:
        customer_id = self._candidate()
        other = self.tenancy.create_business(owner_user_id=4101, name="Other")
        other_owner = self.tenancy.resolve_context(user_id=4101, business_id=other.business.id)
        with patch(
            "clientplatform.application.retention.get_db",
            side_effect=lambda: nullcontext(self.conn),
        ):
            with self.assertRaisesRegex(RetentionCandidateUnavailable, "refresh"):
                prepare_reactivation_sales_lead(
                    actor=other_owner,
                    customer_id=customer_id,
                    expected_cohort=RetentionCohort.INACTIVE_CUSTOMER,
                    now=NOW,
                )

    def test_explicit_repeat_approval_reopens_lost_reactivation_cycle(self) -> None:
        customer_id = self._candidate()
        self._route(customer_id)
        first = self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)
        sales = SalesRepository(self.conn)
        lost = sales.set_stage(
            actor=self.owner,
            lead_id=first.lead.id,
            stage=SalesLeadStage.LOST,
            reason="no_response",
            now=(NOW + timedelta(minutes=1)).isoformat(),
        )
        self.assertEqual(lost.stage, SalesLeadStage.LOST)
        reopened = self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)
        self.assertEqual(reopened.lead.id, first.lead.id)
        self.assertEqual(reopened.lead.stage, SalesLeadStage.NEW)
        self.assertIsNotNone(reopened.lead.next_action)


if __name__ == "__main__":
    unittest.main()
