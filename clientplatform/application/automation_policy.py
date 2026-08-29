from __future__ import annotations

from datetime import datetime, timedelta, timezone

from clientplatform.domain.automation_policy import (
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
    current = get_effective_automation_policy(actor=actor, now=now)
    enabled = current is None or current.spec.mode != AutomationMode.AUTOPILOT
    set_owner_autopilot_enabled(actor=actor, enabled=enabled, now=now)
    return enabled


def is_owner_autopilot_enabled(
    *,
    actor: TenantContext,
    now: datetime | str | None = None,
) -> bool:
    policy = get_effective_automation_policy(actor=actor, now=now)
    return policy is not None and policy.spec.mode == AutomationMode.AUTOPILOT


__all__ = [
    "approve_automation_policy",
    "check_automation_action",
    "get_effective_automation_policy",
    "get_latest_automation_policy",
    "is_owner_autopilot_enabled",
    "revoke_effective_automation_policy",
    "save_automation_policy_draft",
    "set_owner_autopilot_enabled",
    "toggle_owner_autopilot",
]
