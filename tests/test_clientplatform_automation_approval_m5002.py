from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import unittest

from clientplatform.application import admin_ops
from clientplatform.domain.automation_policy import (
    AutomationApprovalConflict,
    AutomationApprovalNotFound,
    AutomationApprovalStatus,
    AutomationCandidateAction,
    AutomationMode,
    AutomationMoneyLimit,
    AutomationPolicySpec,
    AutomationSchedule,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied
from clientplatform.infrastructure.automation_policy_repository import AutomationPolicyRepository
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from clientplatform.privacy_manifest import TENANT_POLICIES, validate_clientplatform_privacy_manifest
from services.db.schema import clientplatform_admin_ops, clientplatform_automation_policy, clientplatform_tenancy

_NOW = datetime(2026, 8, 29, 15, 0, tzinfo=timezone.utc)


def _id() -> str:
    return str(uuid4())


def _spec(*, mode: AutomationMode = AutomationMode.NORMAL) -> AutomationPolicySpec:
    return AutomationPolicySpec(
        mode=mode,
        allowed_actions=("growth.read_only_analysis", "sales.followup"),
        forbidden_actions=("payments.refund",),
        allowed_channels=("internal", "email"),
        allowed_audiences=("business_owner", "prospect_opted_in"),
        schedule=AutomationSchedule(timezone_name="UTC"),
        expires_at=(_NOW + timedelta(days=30)).isoformat(),
        approval_required_actions=("sales.followup",),
        approval_required_channels=("email",),
        stop_conditions=("business_suspended", "owner_stop"),
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _candidate(
    business_id: str,
    *,
    topic: str | None = None,
    subject_ref: str = "customer:fixture-1",
    payload_text: str | None = None,
) -> AutomationCandidateAction:
    content = payload_text or f"followup:{topic or 'default'}"
    return AutomationCandidateAction(
        business_id=business_id,
        action="sales.followup",
        external_write=True,
        channel="email",
        audience="prospect_opted_in",
        scheduled_at=_NOW,
        content_topics=() if topic is None else (topic,),
        subject_ref=subject_ref,
        payload_digest=_digest(content),
    )


class Fixture:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_admin_ops.ensure(self.conn)
        clientplatform_automation_policy.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        owner_access = tenancy.create_business(owner_user_id=501, name="Approval business", now=_NOW.isoformat())
        self.owner = tenancy.resolve_context(user_id=501, business_id=owner_access.business.id)
        self.members: dict[PlatformRole, TenantContext] = {}
        user = 600
        for role in (
            PlatformRole.ADMINISTRATOR,
            PlatformRole.MANAGER,
            PlatformRole.MARKETER,
            PlatformRole.ANALYST,
        ):
            user += 1
            member = tenancy.grant_member(actor=self.owner, user_id=user, role=role, now=_NOW.isoformat())
            self.members[role] = TenantContext(
                business_id=self.owner.business_id,
                user_id=member.user_id,
                membership_id=member.id,
                role=member.role,
            )
        other_access = tenancy.create_business(owner_user_id=999, name="Other", now=_NOW.isoformat())
        self.other_owner = tenancy.resolve_context(user_id=999, business_id=other_access.business.id)
        self.repo = AutomationPolicyRepository(self.conn)
        draft = self.repo.create_draft(actor=self.owner, spec=_spec(), expected_latest_version=0, now=_NOW)
        self.policy = self.repo.approve(
            actor=self.owner,
            policy_id=draft.id,
            expected_policy_hash=draft.policy_hash,
            now=_NOW,
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def request(self, *, actor: TenantContext | None = None, key: str = "m5002:followup:1"):
        return self.repo.request_action_approval(
            actor=actor or self.members[PlatformRole.MARKETER],
            candidate=_candidate(self.owner.business_id),
            idempotency_key=key,
            now=_NOW + timedelta(minutes=1),
        )


class AutomationActionApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_money_approval_currency_must_be_known_settlement_iso4217(self) -> None:
        for currency in ("XXX", "ZZZ"):
            with self.assertRaisesRegex(ValueError, "known settlement ISO 4217"):
                AutomationMoneyLimit(
                    action="ads.adjust_budget",
                    currency=currency,
                    max_per_action_minor=1,
                )


    def test_candidate_fingerprint_round_trip_is_canonical(self) -> None:
        fx = self.fx
        candidate = _candidate(fx.owner.business_id, topic="service_offer")
        restored = AutomationCandidateAction.from_json(candidate.to_json())
        assert restored == candidate
        assert restored.candidate_hash == candidate.candidate_hash
        assert len(candidate.candidate_hash) == 64


    def test_external_approval_requires_exact_subject_and_payload_binding(self) -> None:
        fx = self.fx
        unbound = replace(
            _candidate(fx.owner.business_id),
            subject_ref=None,
            payload_digest=None,
        )
        with self.assertRaisesRegex(AutomationApprovalConflict, "exact_binding_required"):
            fx.repo.request_action_approval(
                actor=fx.owner,
                candidate=unbound,
                idempotency_key="m5002:unbound",
                now=_NOW + timedelta(minutes=1),
            )

    def test_candidate_hash_binds_subject_and_exact_payload_digest(self) -> None:
        fx = self.fx
        candidate = _candidate(fx.owner.business_id)
        other_subject = replace(candidate, subject_ref="customer:fixture-2")
        other_payload = replace(candidate, payload_digest=_digest("followup:different"))
        assert candidate.candidate_hash != other_subject.candidate_hash
        assert candidate.candidate_hash != other_payload.candidate_hash

    def test_request_is_durable_and_exact_replay_is_idempotent(self) -> None:
        fx = self.fx
        first = fx.request()
        replay = fx.request()
        assert first.id == replay.id
        assert first.status == AutomationApprovalStatus.PENDING
        assert first.request_fingerprint == replay.request_fingerprint
        assert first.policy_hash == fx.policy.policy_hash
        assert first.candidate_hash == first.candidate.candidate_hash
        count = fx.conn.execute(
            "SELECT COUNT(*) FROM clientplatform_automation_action_approvals WHERE business_id=?",
            (fx.owner.business_id,),
        ).fetchone()[0]
        assert count == 1


    def test_same_idempotency_key_with_changed_candidate_fails_closed(self) -> None:
        fx = self.fx
        fx.request()
        with self.assertRaisesRegex(AutomationApprovalConflict, "idempotency_conflict"):
            fx.repo.request_action_approval(
                actor=fx.members[PlatformRole.MARKETER],
                candidate=_candidate(fx.owner.business_id, topic="retention"),
                idempotency_key="m5002:followup:1",
                now=_NOW + timedelta(minutes=2),
            )


    def test_allow_or_denied_action_cannot_fabricate_an_approval(self) -> None:
        fx = self.fx
        allowed = AutomationCandidateAction(
            business_id=fx.owner.business_id,
            action="growth.read_only_analysis",
            external_write=False,
            channel="internal",
            audience="business_owner",
            scheduled_at=_NOW,
        )
        with self.assertRaisesRegex(AutomationApprovalConflict, "approval_not_required"):
            fx.repo.request_action_approval(
                actor=fx.owner,
                candidate=allowed,
                idempotency_key="m5002:read-only",
                now=_NOW + timedelta(minutes=1),
            )
        denied = replace(_candidate(fx.owner.business_id), action="custom.unclassified_write")
        with self.assertRaisesRegex(AutomationApprovalConflict, "denied_by_policy"):
            fx.repo.request_action_approval(
                actor=fx.owner,
                candidate=denied,
                idempotency_key="m5002:denied",
                now=_NOW + timedelta(minutes=1),
            )


    def test_business_automation_roles_can_read_but_only_owner_can_decide(self) -> None:
        fx = self.fx
        approval = fx.request(actor=fx.members[PlatformRole.MANAGER])
        assert fx.repo.list_pending_action_approvals(actor=fx.members[PlatformRole.ADMINISTRATOR], now=_NOW + timedelta(minutes=1))[0].id == approval.id
        with self.assertRaisesRegex(TenantPermissionDenied, "owner"):
            fx.repo.approve_action_approval(
                actor=fx.members[PlatformRole.ADMINISTRATOR],
                approval_id=approval.id,
                expected_request_fingerprint=approval.request_fingerprint,
                now=_NOW + timedelta(minutes=2),
            )
        with self.assertRaisesRegex(TenantPermissionDenied, "not allowed"):
            fx.repo.list_pending_action_approvals(actor=fx.members[PlatformRole.ANALYST], now=_NOW)


    def test_owner_approval_is_idempotent_and_authorization_is_deterministic(self) -> None:
        fx = self.fx
        approval = fx.request()
        approved = fx.repo.approve_action_approval(
            actor=fx.owner,
            approval_id=approval.id,
            expected_request_fingerprint=approval.request_fingerprint,
            now=_NOW + timedelta(minutes=2),
        )
        replay = fx.repo.approve_action_approval(
            actor=fx.owner,
            approval_id=approval.id,
            expected_request_fingerprint=approval.request_fingerprint,
            now=_NOW + timedelta(minutes=3),
        )
        assert approved == replay
        first = fx.repo.get_action_authorization(
            actor=fx.owner,
            approval_id=approval.id,
            expected_candidate_hash=approval.candidate_hash,
            expected_subject_ref=approval.candidate.subject_ref or "",
            expected_payload_digest=approval.candidate.payload_digest or "",
            now=_NOW + timedelta(minutes=3),
        )
        second = fx.repo.get_action_authorization(
            actor=fx.members[PlatformRole.MARKETER],
            approval_id=approval.id,
            expected_candidate_hash=approval.candidate_hash,
            expected_subject_ref=approval.candidate.subject_ref or "",
            expected_payload_digest=approval.candidate.payload_digest or "",
            now=_NOW + timedelta(minutes=3),
        )
        assert first == second
        assert first.subject_ref == approval.candidate.subject_ref
        assert first.payload_digest == approval.candidate.payload_digest
        assert len(first.authorization_hash) == 64
        audit = fx.conn.execute(
            "SELECT COUNT(*) FROM clientplatform_admin_audit_events WHERE business_id=? AND action='automation_action_owner_approved'",
            (fx.owner.business_id,),
        ).fetchone()[0]
        assert audit == 1


    def test_reject_is_idempotent_and_cannot_be_approved_afterward(self) -> None:
        fx = self.fx
        approval = fx.request()
        rejected = fx.repo.reject_action_approval(
            actor=fx.owner,
            approval_id=approval.id,
            expected_request_fingerprint=approval.request_fingerprint,
            now=_NOW + timedelta(minutes=2),
        )
        replay = fx.repo.reject_action_approval(
            actor=fx.owner,
            approval_id=approval.id,
            expected_request_fingerprint=approval.request_fingerprint,
            now=_NOW + timedelta(minutes=3),
        )
        assert rejected == replay
        assert rejected.status == AutomationApprovalStatus.REJECTED
        with self.assertRaisesRegex(AutomationApprovalConflict, "already_decided"):
            fx.repo.approve_action_approval(
                actor=fx.owner,
                approval_id=approval.id,
                expected_request_fingerprint=approval.request_fingerprint,
                now=_NOW + timedelta(minutes=4),
            )


    def test_approved_action_can_be_revoked_and_no_longer_authorizes(self) -> None:
        fx = self.fx
        approval = fx.request()
        approved = fx.repo.approve_action_approval(
            actor=fx.owner,
            approval_id=approval.id,
            expected_request_fingerprint=approval.request_fingerprint,
            now=_NOW + timedelta(minutes=2),
        )
        revoked = fx.repo.revoke_action_approval(
            actor=fx.owner,
            approval_id=approval.id,
            expected_request_fingerprint=approved.request_fingerprint,
            now=_NOW + timedelta(minutes=3),
        )
        replay = fx.repo.revoke_action_approval(
            actor=fx.owner,
            approval_id=approval.id,
            expected_request_fingerprint=approved.request_fingerprint,
            now=_NOW + timedelta(minutes=4),
        )
        assert revoked == replay
        assert revoked.status == AutomationApprovalStatus.REVOKED
        with self.assertRaisesRegex(AutomationApprovalConflict, "not_approved"):
            fx.repo.get_action_authorization(
                actor=fx.owner,
                approval_id=approval.id,
                expected_candidate_hash=approval.candidate_hash,
                expected_subject_ref=approval.candidate.subject_ref or "",
                expected_payload_digest=approval.candidate.payload_digest or "",
                now=_NOW + timedelta(minutes=4),
            )


    def test_policy_change_invalidates_pending_and_approved_authority(self) -> None:
        fx = self.fx
        pending = fx.request(key="m5002:stale-pending")
        second_draft = fx.repo.create_draft(
            actor=fx.owner,
            spec=_spec(mode=AutomationMode.AUTOPILOT),
            expected_latest_version=1,
            now=_NOW + timedelta(minutes=2),
        )
        second = fx.repo.approve(
            actor=fx.owner,
            policy_id=second_draft.id,
            expected_policy_hash=second_draft.policy_hash,
            now=_NOW + timedelta(minutes=3),
        )
        assert second.version == 2
        assert fx.repo.list_pending_action_approvals(actor=fx.owner, now=_NOW + timedelta(minutes=3)) == ()
        with self.assertRaisesRegex(AutomationApprovalConflict, "policy_changed"):
            fx.repo.approve_action_approval(
                actor=fx.owner,
                approval_id=pending.id,
                expected_request_fingerprint=pending.request_fingerprint,
                now=_NOW + timedelta(minutes=3),
            )


    def test_new_policy_after_action_approval_invalidates_authorization(self) -> None:
        fx = self.fx
        approval = fx.request(key="m5002:stale-approved")
        fx.repo.approve_action_approval(
            actor=fx.owner,
            approval_id=approval.id,
            expected_request_fingerprint=approval.request_fingerprint,
            now=_NOW + timedelta(minutes=2),
        )
        draft = fx.repo.create_draft(
            actor=fx.owner,
            spec=_spec(mode=AutomationMode.AUTOPILOT),
            expected_latest_version=1,
            now=_NOW + timedelta(minutes=3),
        )
        fx.repo.approve(
            actor=fx.owner,
            policy_id=draft.id,
            expected_policy_hash=draft.policy_hash,
            now=_NOW + timedelta(minutes=4),
        )
        with self.assertRaisesRegex(AutomationApprovalConflict, "policy_changed"):
            fx.repo.get_action_authorization(
                actor=fx.owner,
                approval_id=approval.id,
                expected_candidate_hash=approval.candidate_hash,
                expected_subject_ref=approval.candidate.subject_ref or "",
                expected_payload_digest=approval.candidate.payload_digest or "",
                now=_NOW + timedelta(minutes=4),
            )


    def test_expiry_and_expected_hashes_fail_closed(self) -> None:
        fx = self.fx
        approval = fx.repo.request_action_approval(
            actor=fx.owner,
            candidate=_candidate(fx.owner.business_id),
            idempotency_key="m5002:short",
            now=_NOW + timedelta(minutes=1),
            ttl_seconds=60,
        )
        assert fx.repo.list_pending_action_approvals(actor=fx.owner, now=_NOW + timedelta(minutes=3)) == ()
        with self.assertRaisesRegex(AutomationApprovalConflict, "changed"):
            fx.repo.approve_action_approval(
                actor=fx.owner,
                approval_id=approval.id,
                expected_request_fingerprint="0" * 64,
                now=_NOW + timedelta(minutes=1, seconds=30),
            )
        with self.assertRaisesRegex(AutomationApprovalConflict, "expired"):
            fx.repo.approve_action_approval(
                actor=fx.owner,
                approval_id=approval.id,
                expected_request_fingerprint=approval.request_fingerprint,
                now=_NOW + timedelta(minutes=3),
            )


    def test_cross_tenant_approval_lookup_fails_closed(self) -> None:
        fx = self.fx
        approval = fx.request()
        with self.assertRaises(AutomationApprovalNotFound):
            fx.repo.get_action_approval(actor=fx.other_owner, approval_id=approval.id)


    def test_authorization_requires_exact_candidate_hash(self) -> None:
        fx = self.fx
        approval = fx.request()
        approved = fx.repo.approve_action_approval(
            actor=fx.owner,
            approval_id=approval.id,
            expected_request_fingerprint=approval.request_fingerprint,
            now=_NOW + timedelta(minutes=2),
        )
        with self.assertRaisesRegex(AutomationApprovalConflict, "candidate_changed"):
            fx.repo.get_action_authorization(
                actor=fx.owner,
                approval_id=approved.id,
                expected_candidate_hash="0" * 64,
                expected_subject_ref=approval.candidate.subject_ref or "",
                expected_payload_digest=approval.candidate.payload_digest or "",
                now=_NOW + timedelta(minutes=2),
            )


    def test_authorization_requires_exact_subject_and_payload_digest(self) -> None:
        fx = self.fx
        approval = fx.request(key="m5002:auth-binding")
        approved = fx.repo.approve_action_approval(
            actor=fx.owner,
            approval_id=approval.id,
            expected_request_fingerprint=approval.request_fingerprint,
            now=_NOW + timedelta(minutes=2),
        )
        with self.assertRaisesRegex(AutomationApprovalConflict, "subject_changed"):
            fx.repo.get_action_authorization(
                actor=fx.owner,
                approval_id=approved.id,
                expected_candidate_hash=approval.candidate_hash,
                expected_subject_ref="customer:other",
                expected_payload_digest=approval.candidate.payload_digest or "",
                now=_NOW + timedelta(minutes=2),
            )
        with self.assertRaisesRegex(AutomationApprovalConflict, "payload_changed"):
            fx.repo.get_action_authorization(
                actor=fx.owner,
                approval_id=approved.id,
                expected_candidate_hash=approval.candidate_hash,
                expected_subject_ref=approval.candidate.subject_ref or "",
                expected_payload_digest="0" * 64,
                now=_NOW + timedelta(minutes=2),
            )

    def test_current_approval_list_prioritizes_pending_before_approved_limit(self) -> None:
        fx = self.fx
        for index in range(4):
            approval = fx.repo.request_action_approval(
                actor=fx.owner,
                candidate=_candidate(
                    fx.owner.business_id,
                    subject_ref=f"customer:approved-{index}",
                    payload_text=f"approved payload {index}",
                ),
                idempotency_key=f"m5002:approved:{index}",
                now=_NOW + timedelta(minutes=index + 1),
            )
            fx.repo.approve_action_approval(
                actor=fx.owner,
                approval_id=approval.id,
                expected_request_fingerprint=approval.request_fingerprint,
                now=_NOW + timedelta(minutes=index + 1, seconds=10),
            )
        pending = fx.repo.request_action_approval(
            actor=fx.owner,
            candidate=_candidate(
                fx.owner.business_id,
                subject_ref="customer:pending-new",
                payload_text="pending newest payload",
            ),
            idempotency_key="m5002:pending:new",
            now=_NOW + timedelta(minutes=6),
        )
        visible = fx.repo.list_current_action_approvals(
            actor=fx.owner,
            now=_NOW + timedelta(minutes=7),
            limit=4,
        )
        assert visible[0].id == pending.id
        assert any(item.status == AutomationApprovalStatus.PENDING for item in visible)

    def test_schema_privacy_and_owner_copy_cover_action_approval(self) -> None:
        fx = self.fx
        report = validate_clientplatform_privacy_manifest(fx.conn, strict=True)
        assert report.ok
        assert "clientplatform_automation_action_approvals" in TENANT_POLICIES
        columns = {
            row["name"]
            for row in fx.conn.execute(
                "PRAGMA table_info(clientplatform_automation_action_approvals)"
            ).fetchall()
        }
        assert {
            "business_id",
            "idempotency_key",
            "request_fingerprint",
            "candidate_hash",
            "policy_id",
            "policy_version",
            "policy_hash",
            "status",
            "expires_at",
        }.issubset(columns)

        approval = fx.request(key="m5002:copy")
        rendered = admin_ops.format_automation_action_approval(approval, timezone_name="UTC")
        assert "Нужно Ваше подтверждение" in rendered
        assert "Отправить клиенту follow-up" in rendered
        assert "Канал: email" in rendered
        assert "Почему:" in rendered
        assert f"Цель: {approval.candidate.subject_ref}" in rendered
        assert f"Отпечаток содержимого: {approval.candidate.payload_digest}" in rendered
        assert approval.candidate_hash not in rendered
        assert approval.policy_hash not in rendered
        assert approval.id not in rendered
