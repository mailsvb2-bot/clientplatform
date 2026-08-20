from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from clientplatform.domain.outcomes import BusinessOutcomeEvent, OutcomeMoney, OutcomeSource, OutcomeType
from clientplatform.domain.retention import ReactivationAction, RetentionCohort
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from clientplatform.infrastructure.retention_repository import RetentionRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import create_or_update_tables


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


class ClientPlatformRetentionU010Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        create_or_update_tables(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        access = self.tenancy.create_business(owner_user_id=1001, name="Retention Practice")
        self.owner = self.tenancy.resolve_context(user_id=1001, business_id=access.business.id)
        self.customers = CustomerRepository(self.conn)
        self.outcomes = OutcomeRepository(self.conn)
        self.retention = RetentionRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _customer(self, name: str, *, last_contact_at: datetime) -> str:
        customer = self.customers.create_customer(
            actor=self.owner,
            display_name=name,
            now=last_contact_at.isoformat(),
        )
        self.conn.execute(
            "UPDATE customers SET first_contact_at=?,last_contact_at=? WHERE id=? AND business_id=?",
            (last_contact_at.isoformat(), last_contact_at.isoformat(), customer.id, self.owner.business_id),
        )
        return customer.id

    def _append_outcome(
        self,
        customer_id: str,
        *,
        outcome_type: OutcomeType,
        occurred_at: datetime,
        amount_minor: int | None = None,
        suffix: str,
    ) -> None:
        money = None
        if amount_minor is not None:
            money = OutcomeMoney(amount_minor=amount_minor, currency="RUB")
        event_id = str(uuid4())
        self.outcomes.append(
            BusinessOutcomeEvent(
                id=event_id,
                business_id=self.owner.business_id,
                outcome_type=outcome_type,
                occurred_at=occurred_at,
                source=OutcomeSource(source_type="test_payment", source_id=f"src:{suffix}"),
                customer_id=customer_id,
                subject_ref=None,
                money=money,
                idempotency_key=f"u010:{suffix}",
                metadata={},
                metadata_version=1,
                created_at=occurred_at,
            )
        )

    def test_one_time_and_inactive_cohorts_are_deterministic(self) -> None:
        one_time = self._customer("One", last_contact_at=NOW - timedelta(days=40))
        self._append_outcome(
            one_time,
            outcome_type=OutcomeType.ORDER_PAID,
            occurred_at=NOW - timedelta(days=40),
            amount_minor=500_00,
            suffix="one",
        )
        inactive = self._customer("Inactive", last_contact_at=NOW - timedelta(days=100))
        self._append_outcome(
            inactive,
            outcome_type=OutcomeType.ORDER_PAID,
            occurred_at=NOW - timedelta(days=140),
            amount_minor=700_00,
            suffix="inactive-1",
        )
        self._append_outcome(
            inactive,
            outcome_type=OutcomeType.ORDER_PAID,
            occurred_at=NOW - timedelta(days=100),
            amount_minor=900_00,
            suffix="inactive-2",
        )

        rows = self.retention.list_candidates(actor=self.owner, now=NOW)
        by_id = {row.customer_id: row for row in rows}
        self.assertEqual(by_id[one_time].cohort, RetentionCohort.ONE_TIME_CUSTOMER)
        self.assertEqual(by_id[one_time].suggested_action, ReactivationAction.REVIEW_REPEAT_OFFER)
        self.assertEqual(by_id[one_time].inactive_days, 40)
        self.assertEqual(by_id[inactive].cohort, RetentionCohort.INACTIVE_CUSTOMER)
        self.assertEqual(
            by_id[inactive].suggested_action,
            ReactivationAction.REVIEW_REACTIVATION_OFFER,
        )
        self.assertEqual(by_id[inactive].paid_orders, 2)

    def test_recent_customer_and_recent_booking_are_not_reactivation_candidates(self) -> None:
        recent = self._customer("Recent", last_contact_at=NOW - timedelta(days=10))
        self._append_outcome(
            recent,
            outcome_type=OutcomeType.ORDER_PAID,
            occurred_at=NOW - timedelta(days=10),
            amount_minor=400_00,
            suffix="recent",
        )
        booked = self._customer("Booked", last_contact_at=NOW - timedelta(days=100))
        self._append_outcome(
            booked,
            outcome_type=OutcomeType.ORDER_PAID,
            occurred_at=NOW - timedelta(days=100),
            amount_minor=600_00,
            suffix="booked-paid",
        )
        self._append_outcome(
            booked,
            outcome_type=OutcomeType.BOOKING_CREATED,
            occurred_at=NOW - timedelta(days=5),
            suffix="booked-new",
        )

        ids = {row.customer_id for row in self.retention.list_candidates(actor=self.owner, now=NOW)}
        self.assertNotIn(recent, ids)
        self.assertNotIn(booked, ids)

    def test_recent_reactivation_outcome_prevents_repeat_reactivation_suggestion(self) -> None:
        customer_id = self._customer("Returned", last_contact_at=NOW - timedelta(days=120))
        self._append_outcome(
            customer_id,
            outcome_type=OutcomeType.ORDER_PAID,
            occurred_at=NOW - timedelta(days=120),
            amount_minor=1000_00,
            suffix="returned-paid",
        )
        self._append_outcome(
            customer_id,
            outcome_type=OutcomeType.CUSTOMER_REACTIVATED,
            occurred_at=NOW - timedelta(days=3),
            suffix="returned-reactivated",
        )
        ids = {row.customer_id for row in self.retention.list_candidates(actor=self.owner, now=NOW)}
        self.assertNotIn(customer_id, ids)

    def test_tenant_isolation_and_stable_oldest_first_order(self) -> None:
        first = self._customer("Older", last_contact_at=NOW - timedelta(days=120))
        second = self._customer("Newer", last_contact_at=NOW - timedelta(days=100))
        for customer_id, days, suffix in ((first, 120, "old"), (second, 100, "new")):
            self._append_outcome(
                customer_id,
                outcome_type=OutcomeType.ORDER_PAID,
                occurred_at=NOW - timedelta(days=days),
                amount_minor=500_00,
                suffix=suffix,
            )
        other = self.tenancy.create_business(owner_user_id=2002, name="Other")
        other_owner = self.tenancy.resolve_context(user_id=2002, business_id=other.business.id)
        other_customer = CustomerRepository(self.conn).create_customer(
            actor=other_owner,
            display_name="Foreign",
            now=(NOW - timedelta(days=200)).isoformat(),
        )
        foreign_id = str(other_customer.id)
        self.conn.execute(
            "UPDATE customers SET last_contact_at=? WHERE id=? AND business_id=?",
            ((NOW - timedelta(days=200)).isoformat(), foreign_id, other_owner.business_id),
        )
        OutcomeRepository(self.conn).append(
            BusinessOutcomeEvent(
                id=str(uuid4()),
                business_id=other_owner.business_id,
                outcome_type=OutcomeType.ORDER_PAID,
                occurred_at=NOW - timedelta(days=200),
                source=OutcomeSource(source_type="test_payment", source_id="foreign"),
                customer_id=foreign_id,
                subject_ref=None,
                money=OutcomeMoney(amount_minor=500_00, currency="RUB"),
                idempotency_key="u010:foreign",
                metadata={},
                metadata_version=1,
                created_at=NOW - timedelta(days=200),
            )
        )

        rows = self.retention.list_candidates(actor=self.owner, now=NOW, limit=1)
        self.assertEqual([row.customer_id for row in rows], [first])
        all_ids = {row.customer_id for row in self.retention.list_candidates(actor=self.owner, now=NOW)}
        self.assertNotIn(foreign_id, all_ids)

    def test_invalid_limit_and_naive_now_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "limit"):
            self.retention.list_candidates(actor=self.owner, now=NOW, limit=0)
        customer_id = self._customer("Naive", last_contact_at=NOW - timedelta(days=40))
        self._append_outcome(
            customer_id,
            outcome_type=OutcomeType.ORDER_PAID,
            occurred_at=NOW - timedelta(days=40),
            amount_minor=100_00,
            suffix="naive",
        )
        with self.assertRaisesRegex(ValueError, "now must be timezone-aware"):
            self.retention.list_candidates(actor=self.owner, now=NOW.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
