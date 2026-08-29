from __future__ import annotations

from datetime import datetime, timedelta, timezone

from clientplatform.domain.automation_policy import (
    AutomationActionApproval,
    AutomationActionAuthorization,
    AutomationCandidateAction,
    AutomationMode,
    AutomationPolicy,
    AutomationPolicyNotFound,
    AutomationPolicySpec,
    AutomationSchedule,
    PolicyCheck,
    evaluate_automation_policy,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied
from clientplatform.infrastructure.automation_policy_repository import AutomationPolicyRepository
from services.db import get_db, get_db_ro, tx


def _now(value: datetime | str | None = None) -> datetime | str:
    return value if value is not None else datetime.now(timezone.utc)


def save_automation_policy_draft(
    *,
    actor: TenantContext,
    spec: AutomationPolicySpec,
    expected_latest_version: int | None = None,
    now: datetime | str | None = None,
) -> AutomationPolicy:
    with get_db() as conn:
        with tx(conn):
            return AutomationPolicyRepository(conn).create_draft(
                actor=actor,
                spec=spec,
                expected_latest_version=expected_latest_version,
                now=_now(now),
            )


def approve_automation_policy(
    *,
    actor: TenantContext,
    policy_id: str,
    expected_policy_hash: str,
    now: datetime | str | None = None,
) -> AutomationPolicy:
    with get_db() as conn:
        with tx(conn):
            return AutomationPolicyRepository(conn).approve(
                actor=actor,
                policy_id=policy_id,
                expected_policy_hash=expected_policy_hash,
                now=_now(now),
            )


def revoke_effective_automation_policy(
    *,
    actor: TenantContext,
    now: datetime | str | None = None,
) -> AutomationPolicy | None:
    with get_db() as conn:
        with tx(conn):
            return AutomationPolicyRepository(conn).revoke_effective(actor=actor, now=_now(now))


def get_latest_automation_policy(*, actor: TenantContext) -> AutomationPolicy | None:
    with get_db_ro() as conn:
        return AutomationPolicyRepository(conn).latest(actor=actor)


def get_effective_automation_policy(
    *,
    actor: TenantContext,
    now: datetime | str | None = None,
) -> AutomationPolicy | None:
    with get_db_ro() as conn:
        return AutomationPolicyRepository(conn).effective(actor=actor, now=_now(now))


def check_automation_action(
    *,
    actor: TenantContext,
    candidate: AutomationCandidateAction,
    now: datetime | str | None = None,
) -> PolicyCheck:
    current_time = _now(now)
    with get_db_ro() as conn:
        policy = AutomationPolicyRepository(conn).effective(actor=actor, now=current_time)
    if policy is None:
        raise AutomationPolicyNotFound("effective_automation_policy_required")
    return evaluate_automation_policy(policy=policy, candidate=candidate, now=current_time)


def request_automation_action_approval(
    *,
    actor: TenantContext,
    candidate: AutomationCandidateAction,
    idempotency_key: str,
    now: datetime | str | None = None,
    ttl_seconds: int = 86_400,
) -> AutomationActionApproval:
    with get_db() as conn:
        with tx(conn):
            return AutomationPolicyRepository(conn).request_action_approval(
                actor=actor,
                candidate=candidate,
                idempotency_key=idempotency_key,
                now=_now(now),
                ttl_seconds=ttl_seconds,
            )


def get_automation_action_approval(
    *,
    actor: TenantContext,
    approval_id: str,
) -> AutomationActionApproval:
    with get_db_ro() as conn:
        return AutomationPolicyRepository(conn).get_action_approval(
            actor=actor,
            approval_id=approval_id,
        )


def list_current_automation_action_approvals(
    *,
    actor: TenantContext,
    now: datetime | str | None = None,
    limit: int = 20,
) -> tuple[AutomationActionApproval, ...]:
    with get_db_ro() as conn:
        return AutomationPolicyRepository(conn).list_current_action_approvals(
            actor=actor,
            now=_now(now),
            limit=limit,
        )


def list_pending_automation_action_approvals(
    *,
    actor: TenantContext,
    now: datetime | str | None = None,
    limit: int = 20,
) -> tuple[AutomationActionApproval, ...]:
    with get_db_ro() as conn:
        return AutomationPolicyRepository(conn).list_pending_action_approvals(
            actor=actor,
            now=_now(now),
            limit=limit,
        )


def approve_automation_action(
    *,
    actor: TenantContext,
    approval_id: str,
    expected_request_fingerprint: str,
    now: datetime | str | None = None,
) -> AutomationActionApproval:
    with get_db() as conn:
        with tx(conn):
            return AutomationPolicyRepository(conn).approve_action_approval(
                actor=actor,
                approval_id=approval_id,
                expected_request_fingerprint=expected_request_fingerprint,
                now=_now(now),
            )


def reject_automation_action(
    *,
    actor: TenantContext,
    approval_id: str,
    expected_request_fingerprint: str,
    now: datetime | str | None = None,
) -> AutomationActionApproval:
    with get_db() as conn:
        with tx(conn):
            return AutomationPolicyRepository(conn).reject_action_approval(
                actor=actor,
                approval_id=approval_id,
                expected_request_fingerprint=expected_request_fingerprint,
                now=_now(now),
            )


def revoke_automation_action_approval(
    *,
    actor: TenantContext,
    approval_id: str,
    expected_request_fingerprint: str,
    now: datetime | str | None = None,
) -> AutomationActionApproval:
    with get_db() as conn:
        with tx(conn):
            return AutomationPolicyRepository(conn).revoke_action_approval(
                actor=actor,
                approval_id=approval_id,
                expected_request_fingerprint=expected_request_fingerprint,
                now=_now(now),
            )


def get_automation_action_authorization(
    *,
    actor: TenantContext,
    approval_id: str,
    expected_candidate_hash: str,
    expected_subject_ref: str,
    expected_payload_digest: str,
    now: datetime | str | None = None,
) -> AutomationActionAuthorization:
    with get_db_ro() as conn:
        return AutomationPolicyRepository(conn).get_action_authorization(
            actor=actor,
            approval_id=approval_id,
            expected_candidate_hash=expected_candidate_hash,
            expected_subject_ref=expected_subject_ref,
            expected_payload_digest=expected_payload_digest,
            now=_now(now),
        )


def _business_timezone(conn, *, business_id: str) -> str:
    row = conn.execute(
        "SELECT timezone FROM business_profiles WHERE business_id=? LIMIT 1",
        (business_id,),
    ).fetchone()
    if row is None:
        return "UTC"
    value = row["timezone"] if hasattr(row, "keys") else row[0]
    return str(value or "UTC").strip() or "UTC"


def _safe_growth_policy_spec(
    *,
    mode: AutomationMode,
    timezone_name: str,
    now: datetime,
) -> AutomationPolicySpec:
    """Owner-toggle policy for the current read-only Growth Autopilot surface.

    M5-001 intentionally authorizes no external write and no money action. Future
    execution slices must add explicit actions/limits through a newly owner-approved
    policy instead of interpreting this mode switch as provider permission.
    """

    return AutomationPolicySpec(
        mode=mode,
        allowed_actions=("growth.read_only_analysis",),
        forbidden_actions=(),
        allowed_channels=("internal",),
        allowed_audiences=("business_owner",),
        schedule=AutomationSchedule(timezone_name=timezone_name),
        expires_at=(now + timedelta(days=30)).isoformat(),
        stop_conditions=("business_suspended", "owner_stop"),
    )


def set_owner_autopilot_enabled(
    *,
    actor: TenantContext,
    enabled: bool,
    now: datetime | None = None,
) -> AutomationPolicy:
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be boolean")
    if actor.role != PlatformRole.OWNER:
        raise TenantPermissionDenied("autopilot policy mode requires owner approval")
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    with get_db() as conn:
        with tx(conn):
            repository = AutomationPolicyRepository(conn)
            timezone_name = _business_timezone(conn, business_id=actor.business_id)
            latest = repository.latest(actor=actor)
            draft = repository.create_draft(
                actor=actor,
                spec=_safe_growth_policy_spec(
                    mode=AutomationMode.AUTOPILOT if enabled else AutomationMode.CAUTIOUS,
                    timezone_name=timezone_name,
                    now=timestamp,
                ),
                expected_latest_version=None if latest is None else latest.version,
                now=timestamp,
            )
            return repository.approve(
                actor=actor,
                policy_id=draft.id,
                expected_policy_hash=draft.policy_hash,
                now=timestamp,
            )


def toggle_owner_autopilot(
    *,
    actor: TenantContext,
    now: datetime | None = None,
) -> bool:
    enabled = not is_owner_autopilot_enabled(actor=actor, now=now)
    set_owner_autopilot_enabled(actor=actor, enabled=enabled, now=now)
    return enabled


def is_owner_autopilot_enabled(
    *,
    actor: TenantContext,
    now: datetime | str | None = None,
) -> bool:
    with get_db_ro() as conn:
        return AutomationPolicyRepository(conn).autopilot_enabled_projection(
            actor=actor,
            now=_now(now),
        )


__all__ = [
    "approve_automation_action",
    "approve_automation_policy",
    "check_automation_action",
    "get_automation_action_approval",
    "get_automation_action_authorization",
    "get_effective_automation_policy",
    "get_latest_automation_policy",
    "is_owner_autopilot_enabled",
    "list_current_automation_action_approvals",
    "list_pending_automation_action_approvals",
    "reject_automation_action",
    "request_automation_action_approval",
    "revoke_automation_action_approval",
    "revoke_effective_automation_policy",
    "save_automation_policy_draft",
    "set_owner_autopilot_enabled",
    "toggle_owner_autopilot",
]
