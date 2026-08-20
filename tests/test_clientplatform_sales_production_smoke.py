from __future__ import annotations

import io
import sqlite3
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from unittest import mock

from scripts import clientplatform_sales_production_smoke as smoke
from services.db.schema import (
    clientplatform_activity,
    clientplatform_attribution,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_promotions,
    clientplatform_sales,
    clientplatform_tenancy,
)


class ClientPlatformSalesProductionSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_bookings.ensure(self.conn)
        clientplatform_sales.ensure(self.conn)
        clientplatform_promotions.ensure(self.conn)
        clientplatform_attribution.ensure(self.conn)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    @contextmanager
    def _db_context(self):
        with self.conn:
            yield self.conn

    @contextmanager
    def _ro_context(self):
        yield self.conn

    def _run(self) -> dict[str, object]:
        return smoke.run_production_smoke(
            db_factory=self._db_context,
            ro_factory=self._ro_context,
            require_postgres=False,
        )

    def test_complete_u008_contract_is_proven_then_rolled_back(self) -> None:
        payload = self._run()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["rollback_clean"])
        self.assertEqual(payload["contract_version"], smoke.CONTRACT_VERSION)
        expected_checks = {
            "owner_projection",
            "assignment_projection",
            "cross_tenant_assignee_blocked",
            "unassignment",
            "next_action_due",
            "note_dedupe",
            "lost_closure",
            "lost_reopen",
            "won_terminal",
            "cross_tenant_fail_closed",
            "audit_events",
        }
        self.assertEqual(set(payload["checks"]), expected_checks)
        self.assertTrue(all(payload["checks"].values()))
        self.assertTrue(all(value == 0 for value in payload["residue"].values()))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM clientplatform_sales_leads").fetchone()[0],
            0,
        )

    def test_unexpected_operation_failure_rolls_back_before_failing_closed(self) -> None:
        with mock.patch.object(
            smoke.SalesRepository,
            "add_note",
            side_effect=RuntimeError("synthetic failure"),
        ):
            with self.assertRaisesRegex(
                smoke.ProductionSalesSmokeError,
                "sales_operations_failed",
            ):
                self._run()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM clientplatform_sales_events").fetchone()[0],
            0,
        )

    def test_main_fails_safely_when_postgres_is_not_enabled(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(smoke, "is_postgres_enabled", return_value=False),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = smoke.main()

        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue().strip(),
            smoke.FAILURE_MARKER + "postgres_required",
        )
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
