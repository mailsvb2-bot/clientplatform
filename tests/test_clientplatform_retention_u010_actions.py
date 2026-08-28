from __future__ import annotations

import sqlite3
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application.retention import (
    RetentionCandidateUnavailable,
    list_reactivation_opportunities,
    prepare_reactivation_sales_lead,
    record_reactivation_result,
)
from clientplatform.domain.outcomes import BusinessOutcomeEvent, OutcomeMoney, OutcomeSource, OutcomeType
from clientplatform.domain.retention import RetentionCohort
from clientplatform.domain.sales import (
    ContactBasis,
    SalesError,
    SalesInvariantViolation,
    SalesLeadStage,
)
from clientplatform.infrastructure import ConnectionRepository, DispatchOutboxRepository
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from clientplatform.infrastructure.retention_repository import RetentionRepository
from clientplatform.infrastructure.sales_followup_repository import SalesFollowupRepository
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

    def _opportunities(self, actor=None):
        with patch(
            "clientplatform.application.retention.get_db_ro",
            side_effect=lambda: nullcontext(self.conn),
        ):
            return list_reactivation_opportunities(
                actor=actor or self.owner,
                now=NOW,
                limit=10,
            )

    def _record(
        self,
        lead_id: str,
        *,
        amount_minor: int = 2500_00,
        currency: str = "RUB",
        now: datetime | None = None,
    ):
        with patch(
            "clientplatform.application.retention.get_db",
            side_effect=lambda: nullcontext(self.conn),
        ):
            return record_reactivation_result(
                actor=self.owner,
                lead_id=lead_id,
                amount_minor=amount_minor,
                currency=currency,
                now=now or NOW + timedelta(minutes=5),
            )

    def test_reactivation_opportunity_projection_is_read_only_routable_and_tenant_scoped(self) -> None:
        customer_id = self._candidate()
        self._route(customer_id, platform="vk")
        other = self.tenancy.create_business(owner_user_id=4102, name="Other Projection")
        other_owner = self.tenancy.resolve_context(user_id=4102, business_id=other.business.id)
        before_leads = self.conn.execute("SELECT COUNT(*) FROM clientplatform_sales_leads").fetchone()[0]
        before_outbox = self.conn.execute("SELECT COUNT(*) FROM provider_dispatch_outbox").fetchone()[0]

        rows = self._opportunities()
        foreign_rows = self._opportunities(other_owner)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].candidate.customer_id, customer_id)
        self.assertEqual(rows[0].route_platform, "vk")
        self.assertEqual(foreign_rows, [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM clientplatform_sales_leads").fetchone()[0],
            before_leads,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM provider_dispatch_outbox").fetchone()[0],
            before_outbox,
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

    def test_reactivation_lead_cannot_be_marked_won_without_canonical_result(self) -> None:
        customer_id = self._candidate()
        prepared = self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)

        with self.assertRaisesRegex(
            SalesInvariantViolation,
            "requires canonical payment and reactivation outcome",
        ):
            SalesRepository(self.conn).set_stage(
                actor=self.owner,
                lead_id=prepared.lead.id,
                stage=SalesLeadStage.WON,
                reason="manual_win",
                now=(NOW + timedelta(minutes=2)).isoformat(),
            )

        unchanged = SalesRepository(self.conn).get_lead(
            actor=self.owner,
            lead_id=prepared.lead.id,
        )
        self.assertNotEqual(unchanged.stage, SalesLeadStage.WON)

    def test_record_result_materializes_repeat_payment_reactivation_and_won_stage(self) -> None:
        customer_id = self._candidate()
        self._route(customer_id)
        prepared = self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)

        recorded = self._record(prepared.lead.id)

        self.assertEqual(recorded.lead.stage, SalesLeadStage.WON)
        self.assertEqual(recorded.lead.closure_reason, "customer_reactivated")
        self.assertIsNone(recorded.lead.next_action)
        self.assertEqual(recorded.payment_outcome.outcome_type, OutcomeType.ORDER_PAID)
        self.assertEqual(recorded.payment_outcome.customer_id, customer_id)
        self.assertEqual(recorded.payment_outcome.source_type, "sales_reactivation")
        self.assertEqual(recorded.payment_outcome.source_id, prepared.lead.id)
        self.assertEqual(recorded.payment_outcome.money, OutcomeMoney(2500_00, "RUB"))
        self.assertEqual(
            recorded.reactivation_outcome.outcome_type,
            OutcomeType.CUSTOMER_REACTIVATED,
        )
        self.assertEqual(recorded.reactivation_outcome.source_type, "outcome_event")
        self.assertEqual(
            recorded.reactivation_outcome.source_id,
            recorded.payment_outcome.id,
        )
        self.assertEqual(
            recorded.reactivation_outcome.money,
            OutcomeMoney(2500_00, "RUB"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM clientplatform_sales_events "
                "WHERE lead_id=? AND event_type='reactivation_outcome_recorded'",
                (prepared.lead.id,),
            ).fetchone()[0],
            1,
        )
        self.assertIsNone(
            RetentionRepository(self.conn).get_candidate(
                actor=self.owner,
                customer_id=customer_id,
                now=NOW + timedelta(minutes=6),
            )
        )

    def test_record_result_is_idempotent_and_conflicting_money_fails_closed(self) -> None:
        customer_id = self._candidate()
        prepared = self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)

        first = self._record(prepared.lead.id, amount_minor=1999_50)
        replay = self._record(
            prepared.lead.id,
            amount_minor=1999_50,
            now=NOW + timedelta(minutes=9),
        )

        self.assertEqual(replay.payment_outcome.id, first.payment_outcome.id)
        self.assertEqual(replay.reactivation_outcome.id, first.reactivation_outcome.id)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM business_outcome_events "
                "WHERE business_id=? AND idempotency_key IN (?,?)",
                (
                    self.owner.business_id,
                    f"reactivation-order-paid:{prepared.lead.id}",
                    f"customer-reactivated:{prepared.lead.id}",
                ),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM clientplatform_sales_events "
                "WHERE lead_id=? AND event_type='reactivation_outcome_recorded'",
                (prepared.lead.id,),
            ).fetchone()[0],
            1,
        )
        with self.assertRaisesRegex(SalesInvariantViolation, "conflicts"):
            self._record(prepared.lead.id, amount_minor=2000_00)

    def test_result_requires_canonical_reactivation_lead_and_active_tenant(self) -> None:
        customer_id = self._candidate()
        sales = SalesRepository(self.conn)
        ordinary = sales.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key=f"ordinary:{customer_id}",
            customer_id=customer_id,
            source_kind="manual",
            source_ref="owner-entry",
            contact_basis=ContactBasis.EXISTING_CUSTOMER,
            now=NOW.isoformat(),
        )
        with self.assertRaisesRegex(SalesInvariantViolation, "not a canonical reactivation"):
            self._record(ordinary.id)

        prepared = self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)
        other = self.tenancy.create_business(owner_user_id=4102, name="Other Results")
        other_owner = self.tenancy.resolve_context(user_id=4102, business_id=other.business.id)
        with patch(
            "clientplatform.application.retention.get_db",
            side_effect=lambda: nullcontext(self.conn),
        ):
            with self.assertRaises(SalesError):
                record_reactivation_result(
                    actor=other_owner,
                    lead_id=prepared.lead.id,
                    amount_minor=1000_00,
                    currency="RUB",
                    now=NOW + timedelta(minutes=5),
                )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM business_outcome_events "
                "WHERE idempotency_key LIKE 'reactivation-order-paid:%' "
                "OR idempotency_key LIKE 'customer-reactivated:%'"
            ).fetchone()[0],
            0,
        )

    def test_lost_cycle_must_be_reopened_before_recording_return(self) -> None:
        customer_id = self._candidate()
        prepared = self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)
        SalesRepository(self.conn).set_stage(
            actor=self.owner,
            lead_id=prepared.lead.id,
            stage=SalesLeadStage.LOST,
            reason="no_response",
            now=(NOW + timedelta(minutes=1)).isoformat(),
        )

        with self.assertRaisesRegex(SalesInvariantViolation, "must be reopened"):
            self._record(prepared.lead.id)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM business_outcome_events "
                "WHERE idempotency_key LIKE 'reactivation-order-paid:%' "
                "OR idempotency_key LIKE 'customer-reactivated:%'"
            ).fetchone()[0],
            0,
        )

    def test_confirmed_reactivation_payment_stops_scheduled_followup_and_outbox(self) -> None:
        ActivityRepository(self.conn).upsert_profile(
            actor=self.owner,
            activity_description="Сервисная компания",
            timezone_name="Europe/Moscow",
            now=NOW.isoformat(),
        )
        customer_id = self._candidate()
        self._route(customer_id)
        prepared = self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)
        followups = SalesFollowupRepository(self.conn)
        followup = followups.schedule(
            actor=self.owner,
            lead_id=prepared.lead.id,
            message_text="Анна, готовы помочь с повторным заказом.",
            scheduled_at=NOW + timedelta(hours=1),
            request_key="u010-before-return",
            now=NOW,
        )
        dispatch = DispatchOutboxRepository(self.conn).materialize_sales_followup(
            actor=self.owner,
            followup_id=followup.id,
            now=NOW,
        )
        self.assertEqual(
            followups.get(actor=self.owner, followup_id=followup.id).status.value,
            "queued",
        )

        self._record(prepared.lead.id, now=NOW + timedelta(minutes=5))

        stopped = followups.get(actor=self.owner, followup_id=followup.id)
        self.assertEqual(stopped.status.value, "stopped")
        self.assertEqual(stopped.stop_reason, "lead_closed")
        row = self.conn.execute(
            "SELECT status,last_error FROM provider_dispatch_outbox WHERE id=?",
            (dispatch.id,),
        ).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["last_error"], "sales_followup_lead_closed")

    def test_result_rejects_invalid_money_before_any_write(self) -> None:
        customer_id = self._candidate()
        prepared = self._prepare(customer_id, RetentionCohort.INACTIVE_CUSTOMER)
        with self.assertRaisesRegex(ValueError, "positive"):
            self._record(prepared.lead.id, amount_minor=0)
        with self.assertRaisesRegex(ValueError, "currency"):
            self._record(prepared.lead.id, currency="RUBLE")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM business_outcome_events "
                "WHERE idempotency_key LIKE 'reactivation-order-paid:%' "
                "OR idempotency_key LIKE 'customer-reactivated:%'"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
