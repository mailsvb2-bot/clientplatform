from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any

from clientplatform.domain.automation_policy import (
    AutomationActionApproval,
    AutomationActionAuthorization,
    AutomationActionScope,
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



_EVENT_FOLLOWUP_ACTION = "events.commercial_followup"
_EVENT_FOLLOWUP_CHANNELS = ("email", "max", "vk")
_EVENT_FOLLOWUP_AUDIENCE = "prospect_opted_in"
_EVENT_FOLLOWUP_TOPIC = "service_offer"
_GROWTH_ACTION = "growth.read_only_analysis"


def _event_action_scope(*, timezone_name: str, channels: tuple[str, ...]) -> AutomationActionScope:
    selected = tuple(sorted(set(channels)))
    if not selected or not set(selected).issubset(_EVENT_FOLLOWUP_CHANNELS):
        raise ValueError("event autosend channels are invalid")
    return AutomationActionScope(
        action=_EVENT_FOLLOWUP_ACTION,
        allowed_channels=selected,
        allowed_audiences=(_EVENT_FOLLOWUP_AUDIENCE,),
        allowed_content_topics=(_EVENT_FOLLOWUP_TOPIC,),
        schedule=AutomationSchedule(
            timezone_name=timezone_name,
            quiet_start="22:00",
            quiet_end="08:00",
        ),
    )


def _growth_action_scope() -> AutomationActionScope:
    return AutomationActionScope(
        action=_GROWTH_ACTION,
        allowed_channels=("internal",),
        allowed_audiences=("business_owner",),
    )


def _replace_action_scope(payload: dict[str, Any], scope: AutomationActionScope) -> None:
    scopes = [
        AutomationActionScope.from_payload(dict(item))
        for item in (payload.get("action_scopes") or ())
    ]
    scopes = [item for item in scopes if item.action != scope.action]
    scopes.append(scope)
    payload["action_scopes"] = [item.payload() for item in sorted(scopes, key=lambda item: item.action)]


def _drop_action_scope(payload: dict[str, Any], action: str) -> None:
    scopes = [
        AutomationActionScope.from_payload(dict(item))
        for item in (payload.get("action_scopes") or ())
    ]
    remaining = [item for item in scopes if item.action != action]
    if remaining:
        payload["action_scopes"] = [
            item.payload() for item in sorted(remaining, key=lambda item: item.action)
        ]
    else:
        payload.pop("action_scopes", None)


def _commit_composed_owner_policy(
    repository: AutomationPolicyRepository,
    *,
    actor: TenantContext,
    effective: AutomationPolicy | None,
    latest: AutomationPolicy | None,
    spec: AutomationPolicySpec,
    now: datetime,
) -> AutomationPolicy:
    if effective is not None and spec.policy_hash == effective.policy_hash:
        return effective
    draft = repository.create_draft(
        actor=actor,
        spec=spec,
        expected_latest_version=None if latest is None else latest.version,
        now=now,
    )
    return repository.approve(
        actor=actor,
        policy_id=draft.id,
        expected_policy_hash=draft.policy_hash,
        now=now,
    )


def set_owner_event_autosend_policy_in_conn(
    conn,
    *,
    actor: TenantContext,
    allowed_channels: tuple[str, ...],
    now: datetime,
) -> AutomationPolicy:
    """Grant only the selected event autosend channels in the canonical policy."""

    repository = AutomationPolicyRepository(conn)
    current, effective, latest = repository.locked_policy_state(actor=actor, now=now)
    growth_enabled = repository.autopilot_enabled_projection(actor=current, now=now)
    timezone_name = _business_timezone(conn, business_id=current.business_id)
    event_scope = _event_action_scope(timezone_name=timezone_name, channels=allowed_channels)
    if effective is None:
        actions = {_EVENT_FOLLOWUP_ACTION}
        scopes = [event_scope]
        if growth_enabled:
            actions.add(_GROWTH_ACTION)
            scopes.append(_growth_action_scope())
        spec = AutomationPolicySpec(
            mode=AutomationMode.AUTOPILOT if growth_enabled else AutomationMode.NORMAL,
            allowed_actions=tuple(sorted(actions)),
            forbidden_actions=(),
            allowed_channels=(),
            allowed_audiences=(),
            schedule=AutomationSchedule(timezone_name=timezone_name),
            expires_at=(now + timedelta(days=365)).isoformat(timespec="seconds"),
            stop_conditions=("business_suspended", "owner_stop"),
            action_scopes=tuple(scopes),
        )
    else:
        if _EVENT_FOLLOWUP_ACTION in effective.spec.forbidden_actions:
            raise ValueError("Текущая политика автоматизации запрещает автоматические сообщения после мероприятия")
        if _EVENT_FOLLOWUP_ACTION in effective.spec.approval_required_actions:
            raise ValueError("Текущая политика требует ручного одобрения каждого сообщения после мероприятия")
        if set(allowed_channels) & set(effective.spec.approval_required_channels):
            raise ValueError("Текущая политика требует ручного одобрения выбранных каналов автоматических сообщений")
        payload = effective.spec.payload()
        actions = set(payload["allowed_actions"])
        if growth_enabled:
            actions.add(_GROWTH_ACTION)
            _replace_action_scope(payload, _growth_action_scope())
        else:
            actions.discard(_GROWTH_ACTION)
            _drop_action_scope(payload, _GROWTH_ACTION)
        if effective.spec.mode == AutomationMode.CAUTIOUS:
            unrelated = actions - {_EVENT_FOLLOWUP_ACTION}
            if unrelated:
                raise ValueError(
                    "Текущая политика работает в осторожном режиме; для автоматических сообщений "
                    "после мероприятия сначала пересмотрите другие автоматические действия"
                )
            payload["mode"] = AutomationMode.NORMAL.value
        actions.add(_EVENT_FOLLOWUP_ACTION)
        payload["allowed_actions"] = sorted(actions)
        _replace_action_scope(payload, event_scope)
        spec = AutomationPolicySpec.from_json(json.dumps(payload, ensure_ascii=False))
    return _commit_composed_owner_policy(
        repository, actor=current, effective=effective, latest=latest, spec=spec, now=now
    )


def _safe_growth_policy_spec(
    *,
    mode: AutomationMode,
    timezone_name: str,
    now: datetime,
) -> AutomationPolicySpec:
    """Owner-toggle policy for the current read-only Growth Autopilot surface."""

    return AutomationPolicySpec(
        mode=mode,
        allowed_actions=(_GROWTH_ACTION,),
        forbidden_actions=(),
        allowed_channels=(),
        allowed_audiences=(),
        schedule=AutomationSchedule(timezone_name=timezone_name),
        expires_at=(now + timedelta(days=30)).isoformat(),
        stop_conditions=("business_suspended", "owner_stop"),
        action_scopes=(_growth_action_scope(),),
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
            current, effective, latest = repository.locked_policy_state(
                actor=actor,
                now=timestamp,
            )
            timezone_name = _business_timezone(conn, business_id=current.business_id)
            if effective is None:
                spec = _safe_growth_policy_spec(
                    mode=AutomationMode.AUTOPILOT if enabled else AutomationMode.CAUTIOUS,
                    timezone_name=timezone_name,
                    now=timestamp,
                )
            else:
                payload = effective.spec.payload()
                actions = set(payload["allowed_actions"])
                if enabled:
                    actions.add(_GROWTH_ACTION)
                    _replace_action_scope(payload, _growth_action_scope())
                    if effective.spec.mode == AutomationMode.CAUTIOUS:
                        unrelated = actions - {_GROWTH_ACTION}
                        if unrelated:
                            raise ValueError(
                                "Текущая осторожная политика содержит другие действия; "
                                "включение Growth Autopilot требует отдельного пересмотра политики"
                            )
                        payload["mode"] = AutomationMode.AUTOPILOT.value
                else:
                    actions.discard(_GROWTH_ACTION)
                    _drop_action_scope(payload, _GROWTH_ACTION)
                    if not actions:
                        payload["mode"] = AutomationMode.CAUTIOUS.value
                payload["allowed_actions"] = sorted(actions)
                spec = AutomationPolicySpec.from_json(
                    json.dumps(payload, ensure_ascii=False)
                )
            return _commit_composed_owner_policy(
                repository,
                actor=current,
                effective=effective,
                latest=latest,
                spec=spec,
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
    "set_owner_event_autosend_policy_in_conn",
    "toggle_owner_autopilot",
]
