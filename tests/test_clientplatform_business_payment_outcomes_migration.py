from __future__ import annotations

import sqlite3
from unittest import TestCase

from services.db import schema as db_schema
from services.migrations import clientplatform_business_payment_outcomes_v1 as migration


_STAMP = "2026-08-23T12:00:00.000000+00:00"


class ClientPlatformBusinessPaymentOutcomeMigrationTests(TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        db_schema.create_or_update_tables(self.conn)
        self.conn.execute(
            """
            INSERT INTO businesses(
                id, name, status, created_by_user_id, created_at, updated_at
            ) VALUES('business-1', 'Migration', 'active', 710001, ?, ?)
            """,
            (_STAMP, _STAMP),
        )
        self.conn.execute(
            """
            INSERT INTO business_members(
                id, business_id, user_id, role, status, created_at, updated_at
            ) VALUES('member-1', 'business-1', 710001, 'owner', 'active', ?, ?)
            """,
            (_STAMP, _STAMP),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_payment(
        self,
        *,
        payment_id: str,
        status: str,
        currency: str = "RUB",
    ) -> None:
        refunded_at = _STAMP if status == "refunded" else None
        self.conn.execute(
            """
            INSERT INTO business_payments(
                id, business_id, customer_id, amount_minor, currency,
                status, provider, external_reference, note,
                recorded_by_member_id, created_at, updated_at, paid_at,
                refunded_at
            ) VALUES(?, 'business-1', NULL, 15000, ?, ?, 'manual', NULL, '',
                     'member-1', ?, ?, ?, ?)
            """,
            (payment_id, currency, status, _STAMP, _STAMP, _STAMP, refunded_at),
        )

    def test_apply_backfills_paid_and_refund_facts_once(self) -> None:
        self._insert_payment(payment_id="payment-refunded", status="refunded")
        self.conn.commit()

        with self.conn:
            migration.apply(self.conn)
        with self.conn:
            migration.apply(self.conn)
        replay = migration.reconcile_business_payment_outcomes(self.conn)

        self.assertEqual(replay.payments_scanned, 1)
        self.assertEqual(replay.paid_evidence_created, 0)
        self.assertEqual(replay.refund_evidence_created, 0)
        evidence = self.conn.execute(
            """
            SELECT operation, provider, external_reference
            FROM business_payment_outcome_evidence
            WHERE business_id='business-1'
            ORDER BY operation
            """
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in evidence],
            [
                ("paid", "manual", None),
                ("refund", "legacy_migration", "payment-refunded"),
            ],
        )
        outcomes = self.conn.execute(
            """
            SELECT outcome_type, amount_minor, currency
            FROM business_outcome_events
            WHERE business_id='business-1'
            ORDER BY outcome_type
            """
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in outcomes],
            [
                ("order_paid", 15000, "RUB"),
                ("refund_recorded", 15000, "RUB"),
            ],
        )
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM clientplatform_admin_audit_events
                WHERE action='payment_outcome_backfilled'
                """
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
                (migration.NAME,),
            ).fetchone()[0],
            1,
        )

    def test_unknown_legacy_currency_rolls_back_and_is_not_marked(self) -> None:
        self._insert_payment(
            payment_id="payment-unknown-currency",
            status="paid",
            currency="ZZZ",
        )
        self.conn.commit()

        with self.assertRaisesRegex(ValueError, "known ISO 4217"):
            with self.conn:
                migration.apply(self.conn)

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM business_payment_outcome_evidence"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM business_outcome_events"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
                (migration.NAME,),
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
