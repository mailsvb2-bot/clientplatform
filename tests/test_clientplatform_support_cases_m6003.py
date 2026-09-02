from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application import support_cases as application
from clientplatform.domain.support_cases import SupportCaseStatus
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.support_case_repository import (
    SupportCaseConflict,
    SupportCaseRepository,
    SupportCaseUnavailable,
)
from clientplatform.privacy_manifest import TENANT_POLICIES, validate_clientplatform_privacy_manifest
from services.db.schema import create_or_update_tables


class SupportCaseM6003Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        create_or_update_tables(self.conn)
        tenancy = TenancyRepository(self.conn)
        first = tenancy.create_business(owner_user_id=101, name="First")
        second = tenancy.create_business(owner_user_id=202, name="Second")
        self.actor_first = tenancy.resolve_context(
            user_id=101, business_id=first.business.id
        )
        self.actor_second = tenancy.resolve_context(
            user_id=202, business_id=second.business.id
        )
        self.repo = SupportCaseRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_summary_rejects_provider_credentials(self) -> None:
        long_secret = "1234" * 8
        alpha_secret = "abcdefgh" * 4
        telegram_secret = "ABCDEFGHIJ" * 4
        jwt_part = "abcdefghijk"
        for summary in (
            "api_key=" + long_secret,
            "Authorization: " + alpha_secret,
            "Bearer " + alpha_secret,
            "123456789:" + telegram_secret,
            "eyJ" + jwt_part + "." + jwt_part + "." + jwt_part,
        ):
            with self.subTest(summary_prefix=summary[:12]):
                with self.assertRaisesRegex(ValueError, "credentials or secrets"):
                    self.repo.create(
                        actor=self.actor_first,
                        category="technical",
                        summary=summary,
                        idempotency_key=f"secret-{len(summary)}-{summary[:4]}",
                    )

    def test_create_is_idempotent_and_audited_once(self) -> None:
        first = self.repo.create(
            actor=self.actor_first,
            category="technical",
            summary="Cannot connect messenger",
            idempotency_key="telegram:1:10",
            now="2026-09-02T16:00:00+00:00",
        )
        replay = self.repo.create(
            actor=self.actor_first,
            category="technical",
            summary="Cannot connect messenger",
            idempotency_key="telegram:1:10",
            now="2026-09-02T16:01:00+00:00",
        )
        self.assertEqual(replay.id, first.id)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM clientplatform_support_case_audit_events "
            "WHERE case_id=? AND event_type='created'",
            (first.id,),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_create_idempotency_conflict_fails_closed(self) -> None:
        self.repo.create(
            actor=self.actor_first,
            category="general",
            summary="First summary",
            idempotency_key="same",
        )
        with self.assertRaises(SupportCaseConflict):
            self.repo.create(
                actor=self.actor_first,
                category="security",
                summary="Different work",
                idempotency_key="same",
            )

    def test_tenant_list_is_business_scoped(self) -> None:
        one = self.repo.create(
            actor=self.actor_first,
            category="general",
            summary="First tenant case",
            idempotency_key="1",
        )
        two = self.repo.create(
            actor=self.actor_second,
            category="billing",
            summary="Second tenant case",
            idempotency_key="2",
        )
        self.assertEqual(
            [item.id for item in self.repo.list_for_tenant(actor=self.actor_first)],
            [one.id],
        )
        self.assertEqual(
            [item.id for item in self.repo.list_for_tenant(actor=self.actor_second)],
            [two.id],
        )

    def test_claim_release_resolve_lifecycle_and_queue(self) -> None:
        case = self.repo.create(
            actor=self.actor_first,
            category="integration",
            summary="Provider callback issue",
            idempotency_key="new",
        )
        claimed = self.repo.claim_platform(
            operator_user_id=9001, case_id=case.id, idempotency_key="claim"
        )
        self.assertEqual(claimed.status, SupportCaseStatus.CLAIMED)
        self.assertEqual(claimed.claimed_by_operator_user_id, 9001)
        released = self.repo.release_platform(
            operator_user_id=9001, case_id=case.id, idempotency_key="release"
        )
        self.assertEqual(released.status, SupportCaseStatus.OPEN)
        claimed_again = self.repo.claim_platform(
            operator_user_id=9001, case_id=case.id, idempotency_key="claim-2"
        )
        resolved = self.repo.resolve_platform(
            operator_user_id=9001, case_id=case.id, idempotency_key="resolve"
        )
        self.assertEqual(claimed_again.status, SupportCaseStatus.CLAIMED)
        self.assertEqual(resolved.status, SupportCaseStatus.RESOLVED)
        self.assertEqual(self.repo.list_platform_queue(), [])
        events = [
            row[0]
            for row in self.conn.execute(
                "SELECT event_type FROM clientplatform_support_case_audit_events "
                "WHERE case_id=? ORDER BY created_at,event_type",
                (case.id,),
            ).fetchall()
        ]
        self.assertEqual(set(events), {"created", "claimed", "released", "resolved"})

    def test_other_operator_cannot_release_or_resolve(self) -> None:
        case = self.repo.create(
            actor=self.actor_first,
            category="general",
            summary="Need support",
            idempotency_key="case",
        )
        self.repo.claim_platform(
            operator_user_id=9001, case_id=case.id, idempotency_key="claim"
        )
        with self.assertRaises(SupportCaseConflict):
            self.repo.release_platform(
                operator_user_id=9002,
                case_id=case.id,
                idempotency_key="release-other",
            )
        with self.assertRaises(SupportCaseConflict):
            self.repo.resolve_platform(
                operator_user_id=9002,
                case_id=case.id,
                idempotency_key="resolve-other",
            )

    def test_new_operation_key_cannot_masquerade_as_claim_or_resolve_replay(self) -> None:
        case = self.repo.create(
            actor=self.actor_first,
            category="general",
            summary="Need support",
            idempotency_key="case-replay",
        )
        self.repo.claim_platform(
            operator_user_id=9001, case_id=case.id, idempotency_key="claim-1"
        )
        with self.assertRaisesRegex(SupportCaseConflict, "already claimed"):
            self.repo.claim_platform(
                operator_user_id=9001, case_id=case.id, idempotency_key="claim-2"
            )
        resolved = self.repo.resolve_platform(
            operator_user_id=9001, case_id=case.id, idempotency_key="resolve-1"
        )
        self.assertEqual(resolved.status, SupportCaseStatus.RESOLVED)
        with self.assertRaisesRegex(SupportCaseUnavailable, "already resolved"):
            self.repo.resolve_platform(
                operator_user_id=9001, case_id=case.id, idempotency_key="resolve-2"
            )

    def test_stale_claim_replay_fails_after_release(self) -> None:
        case = self.repo.create(
            actor=self.actor_first,
            category="general",
            summary="Need support",
            idempotency_key="case",
        )
        self.repo.claim_platform(
            operator_user_id=9001, case_id=case.id, idempotency_key="claim"
        )
        self.repo.release_platform(
            operator_user_id=9001, case_id=case.id, idempotency_key="release"
        )
        with self.assertRaisesRegex(SupportCaseUnavailable, "replay is stale"):
            self.repo.claim_platform(
                operator_user_id=9001, case_id=case.id, idempotency_key="claim"
            )

    def test_platform_gate_denies_before_database(self) -> None:
        def must_not_open():
            raise AssertionError("database must not open")

        with (
            patch.object(application, "is_platform_admin", return_value=False),
            patch.object(application, "get_db_ro", side_effect=must_not_open),
            self.assertRaises(application.PlatformSupportCasePermissionDenied),
        ):
            application.list_platform_support_queue(17)

    def test_case_to_support_session_requires_exact_claim_owner(self) -> None:
        case = SimpleNamespace(
            id="f3b3c9dd-fcb1-43ad-b911-32dfd81222ac",
            business_id="ad67e150-0d91-48c9-a879-44a44782250d",
            status=SupportCaseStatus.CLAIMED,
            claimed_by_operator_user_id=9001,
        )
        connection = object()

        @contextmanager
        def fake_db():
            yield connection

        class FakeRepo:
            def __init__(self, conn):
                if conn is not connection:
                    raise AssertionError("support bridge must reuse exact DB connection")

            def require_claimed_for_platform_session(
                self, *, operator_user_id: int, case_id: str
            ):
                if case_id != case.id:
                    raise AssertionError("wrong support case")
                if case.status != SupportCaseStatus.CLAIMED:
                    raise SupportCaseUnavailable("support case must be claimed")
                if case.claimed_by_operator_user_id != operator_user_id:
                    raise SupportCaseConflict("support case is owned by another operator")
                return case

        captured: dict[str, object] = {}

        def fake_issue(user_id, *, conn, **kwargs):
            if conn is not connection:
                raise AssertionError("support session must use caller transaction")
            captured.update(user_id=user_id, **kwargs)
            return "session"

        with (
            patch.object(application, "is_platform_admin", side_effect=lambda uid: uid == 9001),
            patch.object(application, "get_db", side_effect=fake_db),
            patch.object(application, "SupportCaseRepository", FakeRepo),
            patch.object(
                application,
                "issue_support_session_in_transaction",
                side_effect=fake_issue,
            ),
        ):
            result = application.issue_support_session_for_case(
                9001,
                case_id=case.id,
                reason="Investigate exact case",
                idempotency_key="telegram:1:99",
            )
            self.assertEqual(result, "session")
            self.assertEqual(captured["business_id"], case.business_id)
            self.assertEqual(captured["ticket_ref"], f"support-case:{case.id}")

            case.claimed_by_operator_user_id = 9002
            with self.assertRaisesRegex(SupportCaseConflict, "another operator"):
                application.issue_support_session_for_case(
                    9001,
                    case_id=case.id,
                    reason="No",
                    idempotency_key="telegram:1:100",
                )

    def test_privacy_manifest_classifies_support_case_tables(self) -> None:
        self.assertEqual(
            TENANT_POLICIES["clientplatform_support_cases"].disposition,
            "anonymize",
        )
        self.assertEqual(
            TENANT_POLICIES["clientplatform_support_case_audit_events"].disposition,
            "anonymize",
        )
        report = validate_clientplatform_privacy_manifest(self.conn, strict=True)
        self.assertTrue(report.ok)
        self.assertIn("clientplatform_support_cases", report.discovered_business_tables)
        self.assertIn(
            "clientplatform_support_case_audit_events",
            report.discovered_business_tables,
        )


if __name__ == "__main__":
    unittest.main()
