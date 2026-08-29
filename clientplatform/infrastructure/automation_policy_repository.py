from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any
from uuid import uuid4

from clientplatform.domain.automation_policy import (
    AutomationActionApproval,
    AutomationActionAuthorization,
    AutomationApprovalConflict,
    AutomationApprovalNotFound,
    AutomationApprovalStatus,
    AutomationCandidateAction,
    AutomationMode,
    AutomationPolicy,
    AutomationPolicyConflict,
    AutomationPolicyNotFound,
    AutomationPolicySpec,
    AutomationPolicyStatus,
    PolicyDecision,
    build_automation_action_authorization,
    build_pending_automation_action_approval,
    evaluate_automation_policy,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied, normalize_uuid
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository


def _utc_now(value: datetime | str | None = None) -> str:
    if isinstance(value, datetime):
        current = value
    elif value is not None:
        raw = str(value).strip().replace("Z", "+00:00")
        current = datetime.fromisoformat(raw)
    else:
        current = datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("automation policy timestamp must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _policy_from_row(row: Any) -> AutomationPolicy:
    return AutomationPolicy(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        version=int(_value(row, "version", 2)),
        status=AutomationPolicyStatus(str(_value(row, "status", 3))),
        spec=AutomationPolicySpec.from_json(str(_value(row, "policy_json", 5))),
        policy_hash=str(_value(row, "policy_hash", 6)),
        created_by_member_id=str(_value(row, "created_by_member_id", 7)),
        approved_by_member_id=(
            None if _value(row, "approved_by_member_id", 8) is None else str(_value(row, "approved_by_member_id", 8))
        ),
        created_at=str(_value(row, "created_at", 9)),
        updated_at=str(_value(row, "updated_at", 10)),
        approved_at=None if _value(row, "approved_at", 11) is None else str(_value(row, "approved_at", 11)),
        revoked_at=None if _value(row, "revoked_at", 12) is None else str(_value(row, "revoked_at", 12)),
    )


_COLUMNS = """
    id, business_id, version, status, mode, policy_json, policy_hash,
    created_by_member_id, approved_by_member_id, created_at, updated_at,
    approved_at, revoked_at
""".strip()


_APPROVAL_COLUMNS = """
    id, business_id, idempotency_key, request_fingerprint, candidate_json,
    candidate_hash, policy_id, policy_version, policy_hash, approval_reasons_json,
    status, requested_by_member_id, decided_by_member_id, requested_at,
    expires_at, decided_at, revoked_at
""".strip()


def _approval_from_row(row: Any) -> AutomationActionApproval:
    raw_reasons = json.loads(str(_value(row, "approval_reasons_json", 9)))
    if not isinstance(raw_reasons, list):
        raise AutomationApprovalConflict("automation_action_approval_reasons_invalid")
    return AutomationActionApproval(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        idempotency_key=str(_value(row, "idempotency_key", 2)),
        request_fingerprint=str(_value(row, "request_fingerprint", 3)),
        candidate=AutomationCandidateAction.from_json(str(_value(row, "candidate_json", 4))),
        candidate_hash=str(_value(row, "candidate_hash", 5)),
        policy_id=str(_value(row, "policy_id", 6)),
        policy_version=int(_value(row, "policy_version", 7)),
        policy_hash=str(_value(row, "policy_hash", 8)),
        approval_reasons=tuple(str(item) for item in raw_reasons),
        status=AutomationApprovalStatus(str(_value(row, "status", 10))),
        requested_by_member_id=str(_value(row, "requested_by_member_id", 11)),
        decided_by_member_id=(
            None if _value(row, "decided_by_member_id", 12) is None else str(_value(row, "decided_by_member_id", 12))
        ),
        requested_at=str(_value(row, "requested_at", 13)),
        expires_at=str(_value(row, "expires_at", 14)),
        decided_at=None if _value(row, "decided_at", 15) is None else str(_value(row, "decided_at", 15)),
        revoked_at=None if _value(row, "revoked_at", 16) is None else str(_value(row, "revoked_at", 16)),
    )


_AUTOMATION_APPROVAL_READ_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.MARKETER,
    }
)


class AutomationPolicyRepository:
    """Versioned canonical automation policy store scoped by active business membership."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _current(self, actor: TenantContext, *, manage: bool = False) -> TenantContext:
        current = self._tenancy.resolve_context(user_id=actor.user_id, business_id=actor.business_id)
        if manage:
            current.assert_can_manage_business()
        return current

    def _lock_business(self, business_id: str) -> None:
        cursor = self._conn.execute(
            "UPDATE businesses SET updated_at=updated_at WHERE id=? AND status='active'",
            (normalize_uuid(business_id, field_name="business_id"),),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AutomationPolicyConflict("automation_policy_business_inactive")

    def _audit(
        self,
        *,
        actor: TenantContext,
        action: str,
        policy: AutomationPolicy,
        detail: str,
        now: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO clientplatform_admin_audit_events(
                id, business_id, actor_user_id, action, subject_type,
                subject_id, detail, created_at
            ) VALUES(?, ?, ?, ?, 'automation_policy', ?, ?, ?)
            """,
            (
                str(uuid4()),
                actor.business_id,
                actor.user_id,
                action,
                policy.id,
                detail[:1000],
                now,
            ),
        )

    def create_draft(
        self,
        *,
        actor: TenantContext,
        spec: AutomationPolicySpec,
        expected_latest_version: int | None = None,
        policy_id: str | None = None,
        now: datetime | str | None = None,
    ) -> AutomationPolicy:
        current = self._current(actor, manage=True)
        timestamp = _utc_now(now)
        self._lock_business(current.business_id)
        row = self._conn.execute(
            "SELECT MAX(version) AS version FROM clientplatform_automation_policies WHERE business_id=?",
            (current.business_id,),
        ).fetchone()
        latest = 0 if row is None or _value(row, "version", 0) is None else int(_value(row, "version", 0))
        if expected_latest_version is not None and int(expected_latest_version) != latest:
            raise AutomationPolicyConflict("automation_policy_version_changed")
        version = latest + 1
        identifier = normalize_uuid(policy_id or str(uuid4()), field_name="policy_id")
        self._conn.execute(
            """
            INSERT INTO clientplatform_automation_policies(
                id, business_id, version, status, mode, policy_json, policy_hash,
                created_by_member_id, approved_by_member_id, created_at, updated_at,
                approved_at, revoked_at
            ) VALUES(?, ?, ?, 'draft', ?, ?, ?, ?, NULL, ?, ?, NULL, NULL)
            """,
            (
                identifier,
                current.business_id,
                version,
                spec.mode.value,
                spec.to_json(),
                spec.policy_hash,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        policy = self.get(actor=current, policy_id=identifier)
        self._audit(
            actor=current,
            action="automation_policy_draft_created",
            policy=policy,
            detail=f"version={version};hash={policy.policy_hash};mode={policy.spec.mode.value}",
            now=timestamp,
        )
        return policy

    def get(self, *, actor: TenantContext, policy_id: str) -> AutomationPolicy:
        current = self._current(actor)
        identifier = normalize_uuid(policy_id, field_name="policy_id")
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM clientplatform_automation_policies WHERE business_id=? AND id=? LIMIT 1",  # nosec B608
            (current.business_id, identifier),
        ).fetchone()
        if row is None:
            raise AutomationPolicyNotFound("automation_policy_not_found")
        return _policy_from_row(row)

    def latest(self, *, actor: TenantContext) -> AutomationPolicy | None:
        current = self._current(actor)
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM clientplatform_automation_policies "  # nosec B608
            "WHERE business_id=? ORDER BY version DESC LIMIT 1",
            (current.business_id,),
        ).fetchone()
        return None if row is None else _policy_from_row(row)

    def effective(self, *, actor: TenantContext, now: datetime | str | None = None) -> AutomationPolicy | None:
        current = self._current(actor)
        timestamp = _utc_now(now)
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM clientplatform_automation_policies "  # nosec B608
            "WHERE business_id=? AND status='approved' ORDER BY version DESC",
            (current.business_id,),
        ).fetchall()
        for row in rows:
            policy = _policy_from_row(row)
            if policy.is_effective(now=timestamp):
                return policy
        return None

    def autopilot_enabled_projection(
        self,
        *,
        actor: TenantContext,
        now: datetime | str | None = None,
    ) -> bool:
        """Read-only compatibility projection for the pre-M5 Growth toggle.

        A legacy `autopilot_enabled=true` may keep the existing recommendation UX
        enabled only while this business has never written an AutomationPolicy.
        It is deliberately *not* an effective policy and can never authorize an
        action through `effective()` / PolicyCheck. The first owner-approved
        policy makes the canonical ledger authoritative forever for this business;
        an administrator-only draft cannot silently disable this projection.
        """

        current = self._current(actor)
        effective = self.effective(actor=current, now=now)
        if effective is not None:
            return effective.spec.mode == AutomationMode.AUTOPILOT
        owner_authority_row = self._conn.execute(
            """
            SELECT 1
            FROM clientplatform_automation_policies
            WHERE business_id=? AND approved_by_member_id IS NOT NULL
            LIMIT 1
            """,
            (current.business_id,),
        ).fetchone()
        if owner_authority_row is not None:
            return False
        row = self._conn.execute(
            """
            SELECT setting_value
            FROM business_admin_settings
            WHERE business_id=? AND setting_key='autopilot_enabled'
            LIMIT 1
            """,
            (current.business_id,),
        ).fetchone()
        if row is None:
            return False
        return str(_value(row, "setting_value", 0) or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def approve(
        self,
        *,
        actor: TenantContext,
        policy_id: str,
        expected_policy_hash: str,
        now: datetime | str | None = None,
    ) -> AutomationPolicy:
        current = self._current(actor, manage=True)
        if current.role != PlatformRole.OWNER:
            raise TenantPermissionDenied("automation policy approval requires owner role")
        timestamp = _utc_now(now)
        self._lock_business(current.business_id)
        policy = self.get(actor=current, policy_id=policy_id)
        if policy.status != AutomationPolicyStatus.DRAFT:
            raise AutomationPolicyConflict("automation_policy_not_draft")
        if policy.policy_hash != str(expected_policy_hash or "").strip().lower():
            raise AutomationPolicyConflict("automation_policy_changed_before_approval")
        if datetime.fromisoformat(policy.spec.expires_at) <= datetime.fromisoformat(timestamp):
            raise AutomationPolicyConflict("automation_policy_expired_before_approval")
        self._conn.execute(
            """
            UPDATE clientplatform_automation_policies
            SET status='superseded', updated_at=?
            WHERE business_id=? AND status='approved' AND id<>?
            """,
            (timestamp, current.business_id, policy.id),
        )
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_automation_policies
            SET status='approved', approved_by_member_id=?, approved_at=?, updated_at=?
            WHERE business_id=? AND id=? AND status='draft' AND policy_hash=?
            """,
            (
                current.membership_id,
                timestamp,
                timestamp,
                current.business_id,
                policy.id,
                policy.policy_hash,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AutomationPolicyConflict("automation_policy_concurrent_approval")
        approved = self.get(actor=current, policy_id=policy.id)
        self._audit(
            actor=current,
            action="automation_policy_owner_approved",
            policy=approved,
            detail=f"version={approved.version};hash={approved.policy_hash};mode={approved.spec.mode.value}",
            now=timestamp,
        )
        return approved

    def revoke_effective(
        self,
        *,
        actor: TenantContext,
        now: datetime | str | None = None,
    ) -> AutomationPolicy | None:
        current = self._current(actor, manage=True)
        timestamp = _utc_now(now)
        self._lock_business(current.business_id)
        policy = self.effective(actor=current, now=timestamp)
        if policy is None:
            return None
        self._conn.execute(
            """
            UPDATE clientplatform_automation_policies
            SET status='revoked', revoked_at=?, updated_at=?
            WHERE business_id=? AND id=? AND status='approved'
            """,
            (timestamp, timestamp, current.business_id, policy.id),
        )
        revoked = self.get(actor=current, policy_id=policy.id)
        self._audit(
            actor=current,
            action="automation_policy_revoked",
            policy=revoked,
            detail=f"version={revoked.version};hash={revoked.policy_hash}",
            now=timestamp,
        )
        return revoked

    def _approval_actor(self, actor: TenantContext, *, owner: bool = False) -> TenantContext:
        current = self._current(actor)
        if current.role not in _AUTOMATION_APPROVAL_READ_ROLES:
            raise TenantPermissionDenied("automation approvals are not allowed for this business role")
        if owner and current.role != PlatformRole.OWNER:
            raise TenantPermissionDenied("automation action decision requires owner role")
        return current

    def _audit_approval(
        self,
        *,
        actor: TenantContext,
        action: str,
        approval: AutomationActionApproval,
        detail: str,
        now: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO clientplatform_admin_audit_events(
                id, business_id, actor_user_id, action, subject_type,
                subject_id, detail, created_at
            ) VALUES(?, ?, ?, ?, 'automation_action_approval', ?, ?, ?)
            """,
            (
                str(uuid4()),
                actor.business_id,
                actor.user_id,
                action,
                approval.id,
                detail[:1000],
                now,
            ),
        )

    def get_action_approval(
        self,
        *,
        actor: TenantContext,
        approval_id: str,
    ) -> AutomationActionApproval:
        current = self._approval_actor(actor)
        identifier = normalize_uuid(approval_id, field_name="approval_id")
        row = self._conn.execute(
            f"SELECT {_APPROVAL_COLUMNS} FROM clientplatform_automation_action_approvals "  # nosec B608
            "WHERE business_id=? AND id=? LIMIT 1",
            (current.business_id, identifier),
        ).fetchone()
        if row is None:
            raise AutomationApprovalNotFound("automation_action_approval_not_found")
        return _approval_from_row(row)

    def _approval_by_idempotency(
        self,
        *,
        actor: TenantContext,
        idempotency_key: str,
    ) -> AutomationActionApproval | None:
        row = self._conn.execute(
            f"SELECT {_APPROVAL_COLUMNS} FROM clientplatform_automation_action_approvals "  # nosec B608
            "WHERE business_id=? AND idempotency_key=? LIMIT 1",
            (actor.business_id, str(idempotency_key or "").strip()),
        ).fetchone()
        return None if row is None else _approval_from_row(row)

    def _validate_replay(
        self,
        *,
        existing: AutomationActionApproval,
        candidate: AutomationCandidateAction,
        policy: AutomationPolicy,
        approval_reasons: tuple[str, ...],
    ) -> None:
        if (
            existing.candidate_hash != candidate.candidate_hash
            or existing.policy_id != policy.id
            or existing.policy_version != policy.version
            or existing.policy_hash != policy.policy_hash
            or existing.approval_reasons != tuple(sorted(set(approval_reasons)))
        ):
            raise AutomationApprovalConflict("automation_action_idempotency_conflict")

    def request_action_approval(
        self,
        *,
        actor: TenantContext,
        candidate: AutomationCandidateAction,
        idempotency_key: str,
        approval_id: str | None = None,
        now: datetime | str | None = None,
        ttl_seconds: int = 86_400,
    ) -> AutomationActionApproval:
        current = self._approval_actor(actor)
        timestamp = _utc_now(now)
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= 604_800:
            raise ValueError("approval ttl_seconds must be between 60 and 604800")
        self._lock_business(current.business_id)
        policy = self.effective(actor=current, now=timestamp)
        if policy is None:
            raise AutomationPolicyNotFound("effective_automation_policy_required")
        check = evaluate_automation_policy(policy=policy, candidate=candidate, now=timestamp)
        if check.decision == PolicyDecision.DENY:
            raise AutomationApprovalConflict("automation_action_denied_by_policy")
        if check.decision != PolicyDecision.APPROVAL_REQUIRED:
            raise AutomationApprovalConflict("automation_action_approval_not_required")
        existing = self._approval_by_idempotency(actor=current, idempotency_key=idempotency_key)
        if existing is not None:
            self._validate_replay(
                existing=existing,
                candidate=candidate,
                policy=policy,
                approval_reasons=check.approval_reasons,
            )
            return existing
        requested = datetime.fromisoformat(timestamp)
        policy_expiry = datetime.fromisoformat(policy.spec.expires_at)
        expiry = min(requested + timedelta(seconds=ttl_seconds), policy_expiry)
        approval = build_pending_automation_action_approval(
            approval_id=approval_id or str(uuid4()),
            business_id=current.business_id,
            idempotency_key=idempotency_key,
            candidate=candidate,
            policy_check=check,
            requested_by_member_id=current.membership_id,
            requested_at=timestamp,
            expires_at=expiry,
        )
        self._conn.execute(
            """
            INSERT INTO clientplatform_automation_action_approvals(
                id, business_id, idempotency_key, request_fingerprint, candidate_json,
                candidate_hash, policy_id, policy_version, policy_hash, approval_reasons_json,
                status, requested_by_member_id, decided_by_member_id, requested_at,
                expires_at, decided_at, revoked_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, ?, ?, NULL, NULL)
            """,
            (
                approval.id,
                current.business_id,
                approval.idempotency_key,
                approval.request_fingerprint,
                approval.candidate.to_json(),
                approval.candidate_hash,
                approval.policy_id,
                approval.policy_version,
                approval.policy_hash,
                json.dumps(list(approval.approval_reasons), separators=(",", ":")),
                current.membership_id,
                approval.requested_at,
                approval.expires_at,
            ),
        )
        created = self.get_action_approval(actor=current, approval_id=approval.id)
        self._audit_approval(
            actor=current,
            action="automation_action_approval_requested",
            approval=created,
            detail=(
                f"candidate_hash={created.candidate_hash};policy_id={created.policy_id};"
                f"policy_version={created.policy_version};policy_hash={created.policy_hash}"
            ),
            now=timestamp,
        )
        return created

    def list_pending_action_approvals(
        self,
        *,
        actor: TenantContext,
        now: datetime | str | None = None,
        limit: int = 20,
    ) -> tuple[AutomationActionApproval, ...]:
        current = self._approval_actor(actor)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("approval list limit must be between 1 and 100")
        timestamp = _utc_now(now)
        policy = self.effective(actor=current, now=timestamp)
        if policy is None:
            return ()
        rows = self._conn.execute(
            f"SELECT {_APPROVAL_COLUMNS} FROM clientplatform_automation_action_approvals "  # nosec B608
            "WHERE business_id=? AND status='pending' AND requested_at<=? AND expires_at>? "
            "AND policy_id=? AND policy_version=? AND policy_hash=? "
            "ORDER BY requested_at ASC, id ASC LIMIT ?",
            (
                current.business_id,
                timestamp,
                timestamp,
                policy.id,
                policy.version,
                policy.policy_hash,
                limit,
            ),
        ).fetchall()
        return tuple(_approval_from_row(row) for row in rows)

    def list_current_action_approvals(
        self,
        *,
        actor: TenantContext,
        now: datetime | str | None = None,
        limit: int = 20,
    ) -> tuple[AutomationActionApproval, ...]:
        current = self._approval_actor(actor)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("approval list limit must be between 1 and 100")
        timestamp = _utc_now(now)
        policy = self.effective(actor=current, now=timestamp)
        if policy is None:
            return ()
        rows = self._conn.execute(
            f"SELECT {_APPROVAL_COLUMNS} FROM clientplatform_automation_action_approvals "  # nosec B608
            "WHERE business_id=? AND status IN ('pending','approved') AND requested_at<=? AND expires_at>? "
            "AND policy_id=? AND policy_version=? AND policy_hash=? "
            "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, requested_at ASC, id ASC LIMIT ?",
            (
                current.business_id,
                timestamp,
                timestamp,
                policy.id,
                policy.version,
                policy.policy_hash,
                limit,
            ),
        ).fetchall()
        return tuple(_approval_from_row(row) for row in rows)

    def _expected_approval(
        self,
        *,
        actor: TenantContext,
        approval_id: str,
        expected_request_fingerprint: str,
    ) -> AutomationActionApproval:
        approval = self.get_action_approval(actor=actor, approval_id=approval_id)
        if approval.request_fingerprint != str(expected_request_fingerprint or "").strip().lower():
            raise AutomationApprovalConflict("automation_action_approval_changed")
        return approval

    def _validate_current_action_policy(
        self,
        *,
        actor: TenantContext,
        approval: AutomationActionApproval,
        now: str,
    ) -> None:
        if approval.is_expired(now=now):
            raise AutomationApprovalConflict("automation_action_approval_expired")
        policy = self.effective(actor=actor, now=now)
        if policy is None:
            raise AutomationApprovalConflict("automation_action_policy_not_effective")
        if (
            policy.id != approval.policy_id
            or policy.version != approval.policy_version
            or policy.policy_hash != approval.policy_hash
        ):
            raise AutomationApprovalConflict("automation_action_policy_changed")
        check = evaluate_automation_policy(policy=policy, candidate=approval.candidate, now=now)
        if (
            check.decision != PolicyDecision.APPROVAL_REQUIRED
            or check.approval_reasons != approval.approval_reasons
            or check.policy_id != approval.policy_id
            or check.policy_version != approval.policy_version
            or check.policy_hash != approval.policy_hash
        ):
            raise AutomationApprovalConflict("automation_action_policy_check_changed")

    def approve_action_approval(
        self,
        *,
        actor: TenantContext,
        approval_id: str,
        expected_request_fingerprint: str,
        now: datetime | str | None = None,
    ) -> AutomationActionApproval:
        current = self._approval_actor(actor, owner=True)
        timestamp = _utc_now(now)
        self._lock_business(current.business_id)
        approval = self._expected_approval(
            actor=current,
            approval_id=approval_id,
            expected_request_fingerprint=expected_request_fingerprint,
        )
        if approval.status == AutomationApprovalStatus.APPROVED:
            self._validate_current_action_policy(actor=current, approval=approval, now=timestamp)
            return approval
        if approval.status != AutomationApprovalStatus.PENDING:
            raise AutomationApprovalConflict("automation_action_approval_already_decided")
        self._validate_current_action_policy(actor=current, approval=approval, now=timestamp)
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_automation_action_approvals
            SET status='approved', decided_by_member_id=?, decided_at=?
            WHERE business_id=? AND id=? AND status='pending' AND request_fingerprint=?
            """,
            (
                current.membership_id,
                timestamp,
                current.business_id,
                approval.id,
                approval.request_fingerprint,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AutomationApprovalConflict("automation_action_approval_concurrent_decision")
        approved = self.get_action_approval(actor=current, approval_id=approval.id)
        self._audit_approval(
            actor=current,
            action="automation_action_owner_approved",
            approval=approved,
            detail=f"candidate_hash={approved.candidate_hash};policy_hash={approved.policy_hash}",
            now=timestamp,
        )
        return approved

    def reject_action_approval(
        self,
        *,
        actor: TenantContext,
        approval_id: str,
        expected_request_fingerprint: str,
        now: datetime | str | None = None,
    ) -> AutomationActionApproval:
        current = self._approval_actor(actor, owner=True)
        timestamp = _utc_now(now)
        self._lock_business(current.business_id)
        approval = self._expected_approval(
            actor=current,
            approval_id=approval_id,
            expected_request_fingerprint=expected_request_fingerprint,
        )
        if approval.status == AutomationApprovalStatus.REJECTED:
            return approval
        if approval.status != AutomationApprovalStatus.PENDING:
            raise AutomationApprovalConflict("automation_action_approval_already_decided")
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_automation_action_approvals
            SET status='rejected', decided_by_member_id=?, decided_at=?
            WHERE business_id=? AND id=? AND status='pending' AND request_fingerprint=?
            """,
            (
                current.membership_id,
                timestamp,
                current.business_id,
                approval.id,
                approval.request_fingerprint,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AutomationApprovalConflict("automation_action_approval_concurrent_decision")
        rejected = self.get_action_approval(actor=current, approval_id=approval.id)
        self._audit_approval(
            actor=current,
            action="automation_action_owner_rejected",
            approval=rejected,
            detail=f"candidate_hash={rejected.candidate_hash};policy_hash={rejected.policy_hash}",
            now=timestamp,
        )
        return rejected

    def revoke_action_approval(
        self,
        *,
        actor: TenantContext,
        approval_id: str,
        expected_request_fingerprint: str,
        now: datetime | str | None = None,
    ) -> AutomationActionApproval:
        current = self._approval_actor(actor, owner=True)
        timestamp = _utc_now(now)
        self._lock_business(current.business_id)
        approval = self._expected_approval(
            actor=current,
            approval_id=approval_id,
            expected_request_fingerprint=expected_request_fingerprint,
        )
        if approval.status == AutomationApprovalStatus.REVOKED:
            return approval
        if approval.status != AutomationApprovalStatus.APPROVED:
            raise AutomationApprovalConflict("automation_action_approval_not_approved")
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_automation_action_approvals
            SET status='revoked', revoked_at=?
            WHERE business_id=? AND id=? AND status='approved' AND request_fingerprint=?
            """,
            (timestamp, current.business_id, approval.id, approval.request_fingerprint),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AutomationApprovalConflict("automation_action_approval_concurrent_revoke")
        revoked = self.get_action_approval(actor=current, approval_id=approval.id)
        self._audit_approval(
            actor=current,
            action="automation_action_owner_revoked",
            approval=revoked,
            detail=f"candidate_hash={revoked.candidate_hash};policy_hash={revoked.policy_hash}",
            now=timestamp,
        )
        return revoked

    def get_action_authorization(
        self,
        *,
        actor: TenantContext,
        approval_id: str,
        expected_candidate_hash: str,
        expected_subject_ref: str,
        expected_payload_digest: str,
        now: datetime | str | None = None,
    ) -> AutomationActionAuthorization:
        current = self._approval_actor(actor)
        timestamp = _utc_now(now)
        approval = self.get_action_approval(actor=current, approval_id=approval_id)
        if approval.status != AutomationApprovalStatus.APPROVED:
            raise AutomationApprovalConflict("automation_action_not_approved")
        if approval.candidate_hash != str(expected_candidate_hash or "").strip().lower():
            raise AutomationApprovalConflict("automation_action_candidate_changed")
        if approval.candidate.external_write:
            if approval.candidate.subject_ref != str(expected_subject_ref or "").strip().lower():
                raise AutomationApprovalConflict("automation_action_subject_changed")
            if approval.candidate.payload_digest != str(expected_payload_digest or "").strip().lower():
                raise AutomationApprovalConflict("automation_action_payload_changed")
        self._validate_current_action_policy(actor=current, approval=approval, now=timestamp)
        return build_automation_action_authorization(approval)


__all__ = ["AutomationPolicyRepository"]
