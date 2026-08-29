from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import automation_policy as application
from clientplatform.domain.automation_policy import (
    AutomationApprovalThreshold,
    AutomationCandidateAction,
    AutomationMode,
    AutomationMoneyLimit,
    AutomationPolicy,
    AutomationPolicyConflict,
    AutomationPolicyNotFound,
    AutomationPolicySpec,
    AutomationPolicyStatus,
    AutomationSchedule,
    PolicyDecision,
    evaluate_automation_policy,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied
from clientplatform.infrastructure.automation_policy_repository import AutomationPolicyRepository
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from services.db.schema import clientplatform_admin_ops, clientplatform_automation_policy, clientplatform_tenancy

_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def _id() -> str:
    return str(uuid4())


def _spec(*, mode: AutomationMode = AutomationMode.AUTOPILOT, expires_at: datetime | None = None) -> AutomationPolicySpec:
    return AutomationPolicySpec(
        mode=mode,
        allowed_actions=("growth.read_only_analysis", "ads.adjust_budget", "sales.followup"),
        forbidden_actions=("payments.refund",),
        allowed_channels=("internal", "yandex_direct", "email"),
        allowed_audiences=("business_owner", "prospect_opted_in"),
        schedule=AutomationSchedule(
            timezone_name="Europe/Tallinn",
            allowed_weekdays=(0, 1, 2, 3, 4, 5, 6),
            quiet_start="22:00",
            quiet_end="08:00",
        ),
        expires_at=(expires_at or (_NOW + timedelta(days=30))).isoformat(),
        money_limits=(
            AutomationMoneyLimit(
                action="ads.adjust_budget",
                currency="RUB",
                max_per_action_minor=10_000,
                max_daily_minor=30_000,
            ),
        ),
        ai_usage_limit_minor=500,
        ai_usage_currency="RUB",
        approval_required_actions=("sales.followup",),
        approval_required_channels=("email",),
        approval_thresholds=(
            AutomationApprovalThreshold(
                action="ads.adjust_budget",
                amount_minor=5_000,
                currency="RUB",
            ),
        ),
        allowed_content_topics=("service_offer", "retention"),
        forbidden_claims=("guaranteed_income",),
        stop_conditions=("owner_stop", "business_suspended", "payment_anomaly"),
    )


def _policy(
    *,
    business_id: str | None = None,
    status: AutomationPolicyStatus = AutomationPolicyStatus.APPROVED,
    mode: AutomationMode = AutomationMode.AUTOPILOT,
    expires_at: datetime | None = None,
) -> AutomationPolicy:
    business = business_id or _id()
    owner_member = _id()
    spec = _spec(mode=mode, expires_at=expires_at)
    approved = status == AutomationPolicyStatus.APPROVED
    return AutomationPolicy(
        id=_id(),
        business_id=business,
        version=1,
        status=status,
        spec=spec,
        policy_hash=spec.policy_hash,
        created_by_member_id=owner_member,
        created_at=_NOW.isoformat(),
        updated_at=_NOW.isoformat(),
        approved_by_member_id=owner_member if approved else None,
        approved_at=_NOW.isoformat() if approved else None,
    )


class AutomationPolicyDomainTests(unittest.TestCase):
    def test_policy_hash_is_canonical_and_round_trips(self) -> None:
        spec = _spec()
        restored = AutomationPolicySpec.from_json(spec.to_json())
        self.assertEqual(spec, restored)
        self.assertEqual(spec.policy_hash, restored.policy_hash)
        self.assertEqual(64, len(spec.policy_hash))

    def test_explicit_allowed_action_can_pass_without_external_execution(self) -> None:
        policy = _policy()
        candidate = AutomationCandidateAction(
            business_id=policy.business_id,
            action="growth.read_only_analysis",
            external_write=False,
            scheduled_at=_NOW,
        )
        check = evaluate_automation_policy(policy=policy, candidate=candidate, now=_NOW)
        self.assertEqual(PolicyDecision.ALLOW, check.decision)
        self.assertTrue(check.allowed)
        self.assertFalse(check.requires_approval)

    def test_unapproved_expired_and_cross_tenant_actions_fail_closed(self) -> None:
        policy = _policy(status=AutomationPolicyStatus.DRAFT)
        candidate = AutomationCandidateAction(
            business_id=policy.business_id,
            action="growth.read_only_analysis",
            external_write=False,
        )
        self.assertIn(
            "policy_not_effective",
            evaluate_automation_policy(policy=policy, candidate=candidate, now=_NOW).violations,
        )

        expired = _policy(expires_at=_NOW - timedelta(seconds=1))
        self.assertIn(
            "policy_not_effective",
            evaluate_automation_policy(
                policy=expired,
                candidate=replace(candidate, business_id=expired.business_id),
                now=_NOW,
            ).violations,
        )

        approved = _policy()
        cross_tenant = replace(candidate, business_id=_id())
        self.assertIn(
            "candidate_business_mismatch",
            evaluate_automation_policy(policy=approved, candidate=cross_tenant, now=_NOW).violations,
        )

    def test_money_threshold_requires_approval_but_caps_and_missing_evidence_deny(self) -> None:
        policy = _policy()
        base = AutomationCandidateAction(
            business_id=policy.business_id,
            action="ads.adjust_budget",
            external_write=True,
            channel="yandex_direct",
            audience="business_owner",
            scheduled_at=_NOW,
            amount_minor=4_000,
            currency="RUB",
            projected_daily_amount_minor=8_000,
        )
        self.assertEqual(
            PolicyDecision.ALLOW,
            evaluate_automation_policy(policy=policy, candidate=base, now=_NOW).decision,
        )
        threshold = replace(base, amount_minor=5_000)
        self.assertEqual(
            PolicyDecision.APPROVAL_REQUIRED,
            evaluate_automation_policy(policy=policy, candidate=threshold, now=_NOW).decision,
        )
        over_cap = replace(base, amount_minor=10_001)
        self.assertIn(
            "money_per_action_limit_exceeded",
            evaluate_automation_policy(policy=policy, candidate=over_cap, now=_NOW).violations,
        )
        missing_daily = replace(base, projected_daily_amount_minor=None)
        self.assertIn(
            "daily_money_evidence_missing",
            evaluate_automation_policy(policy=policy, candidate=missing_daily, now=_NOW).violations,
        )
        missing_amount = AutomationCandidateAction(
            business_id=policy.business_id,
            action="ads.adjust_budget",
            external_write=True,
            channel="yandex_direct",
            audience="business_owner",
            scheduled_at=_NOW,
        )
        self.assertIn(
            "money_evidence_required",
            evaluate_automation_policy(policy=policy, candidate=missing_amount, now=_NOW).violations,
        )

    def test_channel_audience_quiet_hours_claims_and_stop_conditions_are_enforced(self) -> None:
        policy = _policy()
        candidate = AutomationCandidateAction(
            business_id=policy.business_id,
            action="sales.followup",
            external_write=True,
            channel="email",
            audience="prospect_opted_in",
            scheduled_at=_NOW,
            content_topics=("service_offer",),
        )
        check = evaluate_automation_policy(policy=policy, candidate=candidate, now=_NOW)
        self.assertEqual(PolicyDecision.APPROVAL_REQUIRED, check.decision)
        self.assertIn("action_requires_approval", check.approval_reasons)
        self.assertIn("channel_requires_approval", check.approval_reasons)

        quiet = replace(candidate, scheduled_at=datetime(2026, 8, 29, 20, 0, tzinfo=timezone.utc))
        self.assertIn(
            "schedule_or_quiet_hours_block",
            evaluate_automation_policy(policy=policy, candidate=quiet, now=_NOW).violations,
        )
        blocked_claim = replace(candidate, claims=("guaranteed_income",))
        self.assertIn(
            "forbidden_claim",
            evaluate_automation_policy(policy=policy, candidate=blocked_claim, now=_NOW).violations,
        )
        stopped = replace(candidate, active_stop_conditions=("payment_anomaly",))
        self.assertIn(
            "stop_condition_active",
            evaluate_automation_policy(policy=policy, candidate=stopped, now=_NOW).violations,
        )
        unknown_channel = replace(candidate, channel="unknown_provider")
        self.assertIn(
            "channel_not_allowed",
            evaluate_automation_policy(policy=policy, candidate=unknown_channel, now=_NOW).violations,
        )

    def test_cautious_mode_never_silently_allows_external_write(self) -> None:
        policy = _policy(mode=AutomationMode.CAUTIOUS)
        candidate = AutomationCandidateAction(
            business_id=policy.business_id,
            action="sales.followup",
            external_write=True,
            channel="email",
            audience="prospect_opted_in",
            scheduled_at=_NOW,
        )
        check = evaluate_automation_policy(policy=policy, candidate=candidate, now=_NOW)
        self.assertEqual(PolicyDecision.APPROVAL_REQUIRED, check.decision)
        self.assertIn("cautious_mode_external_write", check.approval_reasons)


@contextmanager
def _same_db(conn: sqlite3.Connection):
    yield conn


class AutomationPolicyRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_admin_ops.ensure(self.conn)
        clientplatform_automation_policy.ensure(self.conn)
        repo = TenancyRepository(self.conn)
        self.owner_access = repo.create_business(owner_user_id=101, name="Policy business", now=_NOW.isoformat())
        self.owner = TenantContext(
            business_id=self.owner_access.business.id,
            user_id=self.owner_access.membership.user_id,
            membership_id=self.owner_access.membership.id,
            role=self.owner_access.membership.role,
        )
        admin_member = repo.grant_member(
            actor=self.owner,
            user_id=202,
            role=PlatformRole.ADMINISTRATOR,
            now=_NOW.isoformat(),
        )
        self.admin = TenantContext(
            business_id=self.owner.business_id,
            user_id=admin_member.user_id,
            membership_id=admin_member.id,
            role=admin_member.role,
        )
        other = repo.create_business(owner_user_id=303, name="Other", now=_NOW.isoformat())
        self.other_owner = TenantContext(
            business_id=other.business.id,
            user_id=other.membership.user_id,
            membership_id=other.membership.id,
            role=other.membership.role,
        )
        self.conn.execute(
            "CREATE TABLE business_profiles(business_id TEXT PRIMARY KEY, timezone TEXT NOT NULL)"
        )
        self.conn.execute(
            "INSERT INTO business_profiles(business_id, timezone) VALUES(?, 'Europe/Tallinn')",
            (self.owner.business_id,),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_admin_can_draft_but_only_owner_can_approve_exact_hash(self) -> None:
        repo = AutomationPolicyRepository(self.conn)
        draft = repo.create_draft(actor=self.admin, spec=_spec(), now=_NOW)
        self.assertEqual(AutomationPolicyStatus.DRAFT, draft.status)
        with self.assertRaisesRegex(TenantPermissionDenied, "owner"):
            repo.approve(
                actor=self.admin,
                policy_id=draft.id,
                expected_policy_hash=draft.policy_hash,
                now=_NOW,
            )
        with self.assertRaises(AutomationPolicyConflict):
            repo.approve(
                actor=self.owner,
                policy_id=draft.id,
                expected_policy_hash="0" * 64,
                now=_NOW,
            )
        approved = repo.approve(
            actor=self.owner,
            policy_id=draft.id,
            expected_policy_hash=draft.policy_hash,
            now=_NOW,
        )
        self.assertEqual(AutomationPolicyStatus.APPROVED, approved.status)
        self.assertEqual(self.owner.membership_id, approved.approved_by_member_id)
        self.assertEqual(approved.id, repo.effective(actor=self.owner, now=_NOW).id)  # type: ignore[union-attr]

    def test_versions_are_serialized_and_previous_approval_is_superseded(self) -> None:
        repo = AutomationPolicyRepository(self.conn)
        first = repo.create_draft(actor=self.owner, spec=_spec(), expected_latest_version=0, now=_NOW)
        first = repo.approve(
            actor=self.owner,
            policy_id=first.id,
            expected_policy_hash=first.policy_hash,
            now=_NOW,
        )
        with self.assertRaises(AutomationPolicyConflict):
            repo.create_draft(actor=self.owner, spec=_spec(), expected_latest_version=0, now=_NOW)
        second = repo.create_draft(
            actor=self.owner,
            spec=_spec(mode=AutomationMode.NORMAL),
            expected_latest_version=1,
            now=_NOW + timedelta(minutes=1),
        )
        self.assertEqual(first.id, repo.effective(actor=self.owner, now=_NOW).id)  # type: ignore[union-attr]
        second = repo.approve(
            actor=self.owner,
            policy_id=second.id,
            expected_policy_hash=second.policy_hash,
            now=_NOW + timedelta(minutes=2),
        )
        self.assertEqual(2, second.version)
        self.assertEqual(second.id, repo.effective(actor=self.owner, now=_NOW + timedelta(minutes=2)).id)  # type: ignore[union-attr]
        self.assertEqual(AutomationPolicyStatus.SUPERSEDED, repo.get(actor=self.owner, policy_id=first.id).status)

    def test_schema_allows_only_one_approved_policy_per_business(self) -> None:
        repo = AutomationPolicyRepository(self.conn)
        draft = repo.create_draft(actor=self.owner, spec=_spec(), now=_NOW)
        approved = repo.approve(
            actor=self.owner,
            policy_id=draft.id,
            expected_policy_hash=draft.policy_hash,
            now=_NOW,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO clientplatform_automation_policies(
                    id, business_id, version, status, mode, policy_json, policy_hash,
                    created_by_member_id, approved_by_member_id, created_at, updated_at,
                    approved_at, revoked_at
                )
                SELECT ?, business_id, version + 1, 'approved', mode, policy_json, policy_hash,
                       created_by_member_id, approved_by_member_id, created_at, updated_at,
                       approved_at, NULL
                FROM clientplatform_automation_policies
                WHERE id=? AND business_id=?
                """,
                (_id(), approved.id, self.owner.business_id),
            )

    def test_cross_tenant_lookup_does_not_leak_policy_and_revoke_is_fail_closed(self) -> None:
        repo = AutomationPolicyRepository(self.conn)
        draft = repo.create_draft(actor=self.owner, spec=_spec(), now=_NOW)
        approved = repo.approve(
            actor=self.owner,
            policy_id=draft.id,
            expected_policy_hash=draft.policy_hash,
            now=_NOW,
        )
        with self.assertRaises(AutomationPolicyNotFound):
            repo.get(actor=self.other_owner, policy_id=approved.id)
        revoked = repo.revoke_effective(actor=self.admin, now=_NOW + timedelta(minutes=1))
        self.assertIsNotNone(revoked)
        self.assertEqual(AutomationPolicyStatus.REVOKED, revoked.status)  # type: ignore[union-attr]
        self.assertIsNone(repo.effective(actor=self.owner, now=_NOW + timedelta(minutes=1)))

    def test_owner_toggle_writes_approved_policy_not_legacy_boolean(self) -> None:
        with (
            patch.object(application, "get_db", side_effect=lambda: _same_db(self.conn)),
            patch.object(application, "get_db_ro", side_effect=lambda: _same_db(self.conn)),
        ):
            enabled = application.toggle_owner_autopilot(actor=self.owner, now=_NOW)
            self.assertTrue(enabled)
            policy = application.get_effective_automation_policy(actor=self.owner, now=_NOW)
            self.assertIsNotNone(policy)
            self.assertEqual(AutomationMode.AUTOPILOT, policy.spec.mode)  # type: ignore[union-attr]
            self.assertEqual(("growth.read_only_analysis",), policy.spec.allowed_actions)  # type: ignore[union-attr]
            self.assertEqual(0, len(policy.spec.money_limits))  # type: ignore[union-attr]
            self.assertIsNone(
                self.conn.execute(
                    "SELECT 1 FROM business_admin_settings WHERE business_id=? AND setting_key='autopilot_enabled'",
                    (self.owner.business_id,),
                ).fetchone()
            )
            enabled = application.toggle_owner_autopilot(actor=self.owner, now=_NOW + timedelta(minutes=1))
            self.assertFalse(enabled)
            disabled = application.get_effective_automation_policy(
                actor=self.owner,
                now=_NOW + timedelta(minutes=1),
            )
            self.assertEqual(AutomationMode.CAUTIOUS, disabled.spec.mode)  # type: ignore[union-attr]

    def test_non_owner_cannot_turn_on_autopilot_policy(self) -> None:
        with patch.object(application, "get_db", side_effect=lambda: _same_db(self.conn)):
            with self.assertRaisesRegex(TenantPermissionDenied, "owner"):
                application.set_owner_autopilot_enabled(actor=self.admin, enabled=True, now=_NOW)


if __name__ == "__main__":
    unittest.main()
