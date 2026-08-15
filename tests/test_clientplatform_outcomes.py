from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import bookings
from clientplatform.domain.outcomes import (
    BusinessOutcomeEvent,
    OutcomeIdempotencyConflict,
    OutcomeMoney,
    OutcomeSource,
    OutcomeType,
)
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from services.db.schema import clientplatform_outcomes


class DurableOutcomeLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("CREATE TABLE businesses(id TEXT PRIMARY KEY)")
        self.business_a = str(uuid4())
        self.business_b = str(uuid4())
        self.customer_a = str(uuid4())
        self.customer_b = str(uuid4())
        self.conn.executemany(
            "INSERT INTO businesses(id) VALUES(?)",
            ((self.business_a,), (self.business_b,)),
        )
        clientplatform_outcomes.ensure(self.conn)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _event(
        self,
        *,
        business_id: str | None = None,
        customer_id: str | None = None,
        outcome_type: OutcomeType = OutcomeType.BOOKING_CREATED,
        source_type: str = "booking_slot",
        source_id: str | None = None,
        occurred_at: datetime | None = None,
        money: OutcomeMoney | None = None,
        metadata: dict[str, object] | None = None,
        idempotency_key: str | None = None,
        subject_ref: str | None = None,
    ) -> BusinessOutcomeEvent:
        business = business_id or self.business_a
        source = source_id or str(uuid4())
        occurred = occurred_at or datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        return BusinessOutcomeEvent(
            id=str(uuid4()),
            business_id=business,
            outcome_type=outcome_type,
            occurred_at=occurred,
            source=OutcomeSource(source_type=source_type, source_id=source),
            customer_id=customer_id,
            subject_ref=subject_ref or f"{source_type}:{source}",
            money=money,
            idempotency_key=idempotency_key or f"{outcome_type.value}:{source}",
            metadata={} if metadata is None else metadata,
            metadata_version=1,
            created_at=occurred + timedelta(seconds=1),
        )

    def test_duplicate_append_returns_original_event_without_duplicate_row(self) -> None:
        repo = OutcomeRepository(self.conn)
        first = self._event(customer_id=self.customer_a)
        accepted = repo.append(first)
        replay = BusinessOutcomeEvent(
            id=str(uuid4()),
            business_id=first.business_id,
            outcome_type=first.outcome_type,
            occurred_at=first.occurred_at,
            source=first.source,
            customer_id=first.customer_id,
            subject_ref=first.subject_ref,
            money=first.money,
            idempotency_key=first.idempotency_key,
            metadata=first.metadata,
            metadata_version=first.metadata_version,
            created_at=first.created_at + timedelta(minutes=1),
        )

        replayed = repo.append(replay)

        self.assertEqual(accepted.id, replayed.id)
        count = self.conn.execute("SELECT COUNT(*) FROM business_outcome_events").fetchone()[0]
        self.assertEqual(1, count)

    def test_idempotency_key_reuse_for_different_fact_is_rejected(self) -> None:
        repo = OutcomeRepository(self.conn)
        first = self._event(customer_id=self.customer_a, idempotency_key="stable-key")
        repo.append(first)
        conflicting = self._event(
            customer_id=self.customer_a,
            source_id=str(uuid4()),
            idempotency_key="stable-key",
        )

        with self.assertRaises(OutcomeIdempotencyConflict):
            repo.append(conflicting)

    def test_same_idempotency_key_is_independent_between_businesses(self) -> None:
        repo = OutcomeRepository(self.conn)
        key = "booking_created:shared-source-id"
        repo.append(
            self._event(
                business_id=self.business_a,
                customer_id=self.customer_a,
                source_id=str(uuid4()),
                idempotency_key=key,
            )
        )
        repo.append(
            self._event(
                business_id=self.business_b,
                customer_id=self.customer_b,
                source_id=str(uuid4()),
                idempotency_key=key,
            )
        )

        rows_a = repo.list_events(business_id=self.business_a)
        rows_b = repo.list_events(business_id=self.business_b)

        self.assertEqual([self.business_a], [item.business_id for item in rows_a])
        self.assertEqual([self.business_b], [item.business_id for item in rows_b])

    def test_write_for_unknown_business_is_rejected(self) -> None:
        repo = OutcomeRepository(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            repo.append(
                self._event(
                    business_id=str(uuid4()),
                    customer_id=self.customer_a,
                )
            )

    def test_filters_are_business_scoped_and_use_half_open_time_range(self) -> None:
        repo = OutcomeRepository(self.conn)
        base = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
        booking = self._event(
            customer_id=self.customer_a,
            source_id="slot-a",
            occurred_at=base,
        )
        paid = self._event(
            customer_id=self.customer_a,
            outcome_type=OutcomeType.ORDER_PAID,
            source_type="order",
            source_id="order-a",
            occurred_at=base + timedelta(hours=1),
            money=OutcomeMoney(amount_minor=12500, currency="rub"),
        )
        outside = self._event(
            customer_id=self.customer_b,
            source_id="slot-b",
            occurred_at=base + timedelta(hours=2),
        )
        for event in (booking, paid, outside):
            repo.append(event)

        filtered = repo.list_events(
            business_id=self.business_a,
            outcome_type=OutcomeType.ORDER_PAID,
            source_type="order",
            customer_id=self.customer_a,
            occurred_from=base,
            occurred_to=base + timedelta(hours=2),
        )

        self.assertEqual([paid.id], [event.id for event in filtered])
        self.assertEqual("RUB", filtered[0].currency)
        self.assertEqual(12500, filtered[0].amount_minor)

    def test_monetary_outcome_requires_integer_minor_units_and_currency(self) -> None:
        with self.assertRaises(ValueError):
            OutcomeMoney(amount_minor=100, currency="")
        with self.assertRaises(TypeError):
            OutcomeMoney(amount_minor=True, currency="RUB")
        with self.assertRaises(ValueError):
            self._event(outcome_type=OutcomeType.ORDER_PAID, source_id="order-no-money")

    def test_correction_and_reversal_append_new_facts_without_mutating_original(self) -> None:
        repo = OutcomeRepository(self.conn)
        original = repo.append(self._event(customer_id=self.customer_a, source_id="slot-original"))
        correction = repo.append(
            self._event(
                customer_id=self.customer_a,
                outcome_type=OutcomeType.OUTCOME_CORRECTION,
                source_type="outcome_event",
                source_id=original.id,
                subject_ref=f"outcome:{original.id}",
                metadata={"reason": "canonical correction"},
            )
        )
        reversal = repo.append(
            self._event(
                customer_id=self.customer_a,
                outcome_type=OutcomeType.OUTCOME_REVERSAL,
                source_type="outcome_event",
                source_id=original.id,
                subject_ref=f"outcome:{original.id}",
                metadata={"reason": "canonical reversal"},
                idempotency_key=f"outcome_reversal:{original.id}",
            )
        )

        rows = repo.list_events(business_id=self.business_a)
        by_id = {row.id: row for row in rows}
        self.assertEqual(3, len(rows))
        self.assertEqual(OutcomeType.BOOKING_CREATED, by_id[original.id].outcome_type)
        self.assertEqual(OutcomeType.OUTCOME_CORRECTION, by_id[correction.id].outcome_type)
        self.assertEqual(OutcomeType.OUTCOME_REVERSAL, by_id[reversal.id].outcome_type)

    def _claim(self, *, business_id: str, customer_id: str, slot_id: str):
        booked_at = "2026-08-15T12:00:00+00:00"
        slot = SimpleNamespace(
            id=slot_id,
            business_id=business_id,
            booked_at=booked_at,
        )
        return SimpleNamespace(
            slot=SimpleNamespace(slot=slot),
            customer_id=customer_id,
        )

    @contextmanager
    def _transaction(self):
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except RuntimeError:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def test_booking_rolls_back_when_outcome_append_fails(self) -> None:
        slot_id = str(uuid4())
        self.conn.execute("CREATE TABLE booking_probe(id TEXT PRIMARY KEY, booked INTEGER NOT NULL)")
        self.conn.execute("INSERT INTO booking_probe(id, booked) VALUES(?, 0)", (slot_id,))
        self.conn.commit()
        claim = self._claim(
            business_id=self.business_a,
            customer_id=self.customer_a,
            slot_id=slot_id,
        )

        class FakeBookingRepository:
            def __init__(inner_self, conn):
                inner_self.conn = conn

            def book_slot(inner_self, **_kwargs):
                inner_self.conn.execute(
                    "UPDATE booking_probe SET booked=1 WHERE id=?",
                    (slot_id,),
                )
                return claim

        class FailingOutcomeRepository:
            def __init__(inner_self, _conn):
                pass

            def append(inner_self, _event):
                raise RuntimeError("forced outcome failure")

        with (
            patch.object(bookings, "get_db", self._transaction),
            patch.object(bookings, "assert_external_customer", lambda *_a, **_k: None),
            patch.object(bookings, "BookingRepository", FakeBookingRepository),
            patch.object(bookings, "OutcomeRepository", FailingOutcomeRepository),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced outcome failure"):
                bookings.book_customer_slot(
                    telegram_user_id=1,
                    business_id=self.business_a,
                    slot_id=slot_id,
                )

        booked = self.conn.execute(
            "SELECT booked FROM booking_probe WHERE id=?",
            (slot_id,),
        ).fetchone()[0]
        self.assertEqual(0, booked)

    def test_booking_replay_keeps_one_canonical_outcome(self) -> None:
        slot_id = str(uuid4())
        claim = self._claim(
            business_id=self.business_a,
            customer_id=self.customer_a,
            slot_id=slot_id,
        )

        class FakeBookingRepository:
            def __init__(inner_self, _conn):
                pass

            def book_slot(inner_self, **_kwargs):
                return claim

        with (
            patch.object(bookings, "get_db", self._transaction),
            patch.object(bookings, "assert_external_customer", lambda *_a, **_k: None),
            patch.object(bookings, "BookingRepository", FakeBookingRepository),
        ):
            bookings.book_customer_slot(
                telegram_user_id=1,
                business_id=self.business_a,
                slot_id=slot_id,
            )
            bookings.book_customer_slot(
                telegram_user_id=1,
                business_id=self.business_a,
                slot_id=slot_id,
            )

        rows = OutcomeRepository(self.conn).list_events(
            business_id=self.business_a,
            outcome_type=OutcomeType.BOOKING_CREATED,
            source_type="booking_slot",
            source_id=slot_id,
        )
        self.assertEqual(1, len(rows))
        self.assertEqual(self.customer_a, rows[0].customer_id)


if __name__ == "__main__":
    unittest.main()
