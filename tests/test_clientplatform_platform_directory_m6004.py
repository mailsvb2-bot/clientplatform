from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import patch

from clientplatform.application import platform_directory as application
from clientplatform.domain.platform_directory import PlatformDirectoryQueryKind
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import create_or_update_tables


class PlatformDirectoryM6004Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        create_or_update_tables(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        self.alpha = self.tenancy.create_business(
            owner_user_id=101, name="Alpha % Studio", now="2026-09-02T10:00:00+00:00"
        )
        self.beta = self.tenancy.create_business(owner_user_id=202, name="Beta Studio", now="2026-09-02T10:01:00+00:00")
        self.literal = self.tenancy.create_business(
            owner_user_id=303, name="Alpha Percent Studio", now="2026-09-02T10:02:00+00:00"
        )
        alpha_actor = self.tenancy.resolve_context(user_id=101, business_id=self.alpha.business.id)
        beta_actor = self.tenancy.resolve_context(user_id=202, business_id=self.beta.business.id)
        self.tenancy.grant_member(actor=alpha_actor, user_id=555, role="support")
        self.tenancy.grant_member(actor=beta_actor, user_id=555, role="manager")

    def tearDown(self) -> None:
        self.conn.close()

    @contextmanager
    def _db(self):
        yield self.conn

    def _search(self, **kwargs):
        with (
            patch.object(application, "is_platform_admin", lambda user_id: user_id == 9001),
            patch.object(application, "get_db", self._db),
        ):
            return application.search_platform_directory(
                9001,
                now_utc=datetime(2026, 9, 2, 20, 0, tzinfo=UTC),
                **kwargs,
            )

    def test_unauthorized_operator_is_denied_before_database_open(self) -> None:
        def must_not_open():
            raise AssertionError("database must not open for unauthorized operator")

        with (
            patch.object(application, "is_platform_admin", lambda _user_id: False),
            patch.object(application, "get_db", must_not_open),
        ):
            with self.assertRaises(application.PlatformDirectoryPermissionDenied):
                application.search_platform_directory(17, query_kind="business_name", query="Alpha")

    def test_invalid_query_and_limit_fail_before_database_open(self) -> None:
        def must_not_open():
            raise AssertionError("invalid request must fail before database open")

        with (
            patch.object(application, "is_platform_admin", lambda user_id: user_id == 9001),
            patch.object(application, "get_db", must_not_open),
        ):
            for query in ("", "*", "%", "ab"):
                with self.subTest(query=query):
                    with self.assertRaises(ValueError):
                        application.search_platform_directory(9001, query_kind="business_name", query=query)
            for invalid_limit in (True, 1.5, "2.5", 0, 21):
                with self.subTest(limit=invalid_limit):
                    with self.assertRaisesRegex(ValueError, "directory limit"):
                        application.search_platform_directory(
                            9001,
                            query_kind="business_name",
                            query="Alpha",
                            limit=invalid_limit,  # type: ignore[arg-type]
                        )
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                application.search_platform_directory(
                    9001,
                    query_kind="business_name",
                    query="Alpha",
                    now_utc=datetime(2026, 9, 2, 20, 0),
                )

    def test_exact_business_lookup_returns_minimal_metadata_and_audit(self) -> None:
        before = self.conn.execute("SELECT COUNT(*) FROM business_members").fetchone()[0]
        result = self._search(
            query_kind=PlatformDirectoryQueryKind.BUSINESS_ID,
            query=self.alpha.business.id,
        )
        self.assertEqual(len(result.matches), 1)
        match = result.matches[0]
        self.assertEqual(match.business_id, self.alpha.business.id)
        self.assertEqual(match.business_name, "Alpha % Studio")
        self.assertEqual(match.active_member_count, 2)
        self.assertEqual(match.active_owner_count, 1)
        self.assertIsNone(match.matched_user_id)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM business_members").fetchone()[0],
            before,
        )
        audit = self.conn.execute(
            "SELECT * FROM clientplatform_platform_operator_audit_events WHERE id=?",
            (result.audit_id,),
        ).fetchone()
        self.assertIsNotNone(audit)
        self.assertEqual(audit["operator_user_id"], 9001)
        self.assertEqual(audit["query_kind"], "business_id")
        self.assertEqual(audit["result_count"], 1)

    def test_user_lookup_is_deterministic_and_reports_membership_reference(self) -> None:
        result = self._search(query_kind="user_id", query=555)
        self.assertEqual(
            [item.business_id for item in result.matches],
            [self.alpha.business.id, self.beta.business.id],
        )
        self.assertEqual([item.matched_user_id for item in result.matches], [555, 555])
        self.assertEqual(
            [item.matched_role.value for item in result.matches if item.matched_role],
            ["support", "manager"],
        )
        self.assertEqual(
            [item.matched_membership_status for item in result.matches],
            ["active", "active"],
        )

    def test_business_name_search_treats_sql_wildcards_as_literals(self) -> None:
        literal = self._search(query_kind="business_name", query="Alpha %")
        self.assertEqual([item.business_id for item in literal.matches], [self.alpha.business.id])
        ordinary = self._search(query_kind="business_name", query="Alpha")
        self.assertEqual(
            [item.business_id for item in ordinary.matches],
            [self.alpha.business.id, self.literal.business.id],
        )

    def test_business_name_search_has_hard_cap_and_deterministic_order(self) -> None:
        for index in range(25):
            self.tenancy.create_business(
                owner_user_id=1000 + index,
                name=f"Directory Studio {index:02d}",
                now=f"2026-09-02T20:{index:02d}:00+00:00",
            )
        result = self._search(
            query_kind="business_name",
            query="Directory Studio",
            limit=20,
        )
        self.assertEqual(len(result.matches), 20)
        self.assertEqual(
            [item.business_name for item in result.matches],
            [f"Directory Studio {index:02d}" for index in range(20)],
        )

    def test_audit_repository_rejects_coerced_count_and_invalid_timestamp(self) -> None:
        from clientplatform.infrastructure.platform_operator_audit_repository import (
            PlatformOperatorAuditRepository,
        )

        repo = PlatformOperatorAuditRepository(self.conn)
        kwargs = {
            "operator_user_id": 9001,
            "query_kind": "business_name",
            "query_fingerprint": "a" * 64,
            "result_fingerprint": "b" * 64,
            "created_at": "2026-09-03T10:00:00+00:00",
        }
        for invalid_count in (True, 1.5):
            with self.subTest(result_count=invalid_count):
                with self.assertRaisesRegex(ValueError, "result_count must be an integer"):
                    repo.record_directory_lookup(
                        result_count=invalid_count,  # type: ignore[arg-type]
                        **kwargs,
                    )
        for invalid_timestamp in ("not-a-date", "2026-09-03T10:00:00"):
            with self.subTest(created_at=invalid_timestamp):
                with self.assertRaisesRegex(ValueError, "created_at"):
                    repo.record_directory_lookup(
                        result_count=0,
                        **{**kwargs, "created_at": invalid_timestamp},
                    )

    def test_directory_audit_has_no_raw_query_or_business_payload_columns(self) -> None:
        raw_query = "Private Search Needle"
        self.tenancy.create_business(owner_user_id=404, name=raw_query)
        result = self._search(query_kind="business_name", query=raw_query)
        row = self.conn.execute(
            "SELECT * FROM clientplatform_platform_operator_audit_events WHERE id=?",
            (result.audit_id,),
        ).fetchone()
        self.assertNotIn(raw_query, "|".join(str(value) for value in row))
        columns = {
            item["name"]
            for item in self.conn.execute("PRAGMA table_info(clientplatform_platform_operator_audit_events)").fetchall()
        }
        self.assertTrue({"operator_user_id", "query_kind", "query_fingerprint", "result_count"}.issubset(columns))
        self.assertTrue({"business_id", "query", "username", "display_name", "customer_id"}.isdisjoint(columns))

    def test_audit_failure_fails_closed_instead_of_returning_directory_results(self) -> None:
        with (
            patch.object(application, "is_platform_admin", lambda user_id: user_id == 9001),
            patch.object(application, "get_db", self._db),
            patch.object(
                application.PlatformOperatorAuditRepository,
                "record_directory_lookup",
                side_effect=RuntimeError("audit unavailable"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                application.search_platform_directory(9001, query_kind="business_name", query="Alpha")

    def test_platform_directory_audit_is_globally_privacy_governed_and_retained(self) -> None:
        from services.privacy_manifest import POLICIES, validate_privacy_manifest

        policy = POLICIES["clientplatform_platform_operator_audit_events"]
        self.assertEqual(policy.ownership_columns, ("operator_user_id",))
        self.assertEqual(policy.disposition, "retain")
        report = validate_privacy_manifest(self.conn, strict=False)
        self.assertNotIn(
            "clientplatform_platform_operator_audit_events",
            report.unknown_tables,
        )
        self.assertIn(
            "clientplatform_platform_operator_audit_events",
            report.discovered_user_tables,
        )

    def test_platform_directory_audit_is_not_a_business_scoped_privacy_table(self) -> None:
        from clientplatform.privacy_manifest import validate_clientplatform_privacy_manifest

        report = validate_clientplatform_privacy_manifest(self.conn, strict=True)
        self.assertNotIn(
            "clientplatform_platform_operator_audit_events",
            report.discovered_business_tables,
        )


if __name__ == "__main__":
    unittest.main()
