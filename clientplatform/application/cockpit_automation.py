from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from clientplatform.application.activity import get_business_profile
from clientplatform.application.automation_policy import (
    approve_automation_action,
    get_automation_action_approval,
    get_effective_automation_policy,
    get_latest_automation_policy,
    is_owner_autopilot_enabled,
    list_current_automation_action_approvals,
    reject_automation_action,
    revoke_automation_action_approval,
    set_owner_autopilot_enabled,
)
from clientplatform.application.cockpit import resolve_cockpit_context
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.automation_policy import (
    AutomationActionApproval,
    AutomationApprovalConflict,
    AutomationApprovalStatus,
    AutomationMode,
)
from clientplatform.domain.money import settlement_currency_minor_unit_exponent
from clientplatform.domain.tenancy import PlatformRole, TenantAccessDenied, TenantContext

_SCHEMA_VERSION = "2026-09-14.v1"
_ACTION_LABELS = {
    "growth.read_only_analysis": "Проанализировать результаты бизнеса",
    "ads.adjust_budget": "Изменить рекламный бюджет",
    "sales.followup": "Связаться с клиентом",
    "events.commercial_followup": "Отправить сообщение после мероприятия",
    "payments.refund": "Оформить возврат",
}
_CHANNEL_LABELS = {
    "internal": "Внутри ClientPlatform",
    "email": "Email",
    "yandex_direct": "Яндекс Директ",
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "max": "MAX",
}
_REASON_LABELS = {
    "cautious_mode_external_write": "включён осторожный режим",
    "action_requires_approval": "это действие требует подтверждения владельца",
    "channel_requires_approval": "для этого канала требуется подтверждение",
    "action_threshold_requires_approval": "для действия установлен порог подтверждения",
    "money_threshold_requires_approval": "сумма достигла порога подтверждения",
}
_MODE_LABELS = {
    AutomationMode.CAUTIOUS: "Осторожный",
    AutomationMode.NORMAL: "Обычный",
    AutomationMode.AUTOPILOT: "Автопилот",
}
_STATUS_LABELS = {
    AutomationApprovalStatus.PENDING: "Нужно решение владельца",
    AutomationApprovalStatus.APPROVED: "Разрешено владельцем",
    AutomationApprovalStatus.REJECTED: "Отклонено",
    AutomationApprovalStatus.REVOKED: "Разрешение отозвано",
}


@dataclass(frozen=True, slots=True)
class CockpitAutomationApprovalItem:
    id: str
    request_fingerprint: str
    status: str
    status_label: str
    title: str
    channel_label: str | None
    reason: str | None
    amount_display: str | None
    requested_at: str
    expires_at: str
    can_approve: bool
    can_reject: bool
    can_revoke: bool


@dataclass(frozen=True, slots=True)
class CockpitAutomationSnapshot:
    schema_version: str
    business_id: str
    business_name: str
    mode: str
    mode_label: str
    policy_state: str
    policy_expires_at: str | None
    autopilot_enabled: bool
    can_change_policy: bool
    pending_count: int
    approvals: tuple[CockpitAutomationApprovalItem, ...]
    safety_note: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _local_timestamp(value: str | None, *, timezone_name: str) -> str | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("automation timestamp must be timezone-aware")
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%d.%m.%Y %H:%M")


def _money_display(item: AutomationActionApproval) -> str | None:
    amount = item.candidate.amount_minor
    currency = item.candidate.currency
    if amount is None or currency is None:
        return None
    exponent = settlement_currency_minor_unit_exponent(currency)
    scale = 10**exponent
    whole, fraction = divmod(int(amount), scale)
    if exponent == 0:
        return f"{whole} {currency}"
    return f"{whole},{fraction:0{exponent}d} {currency}"


def _approval_item(
    item: AutomationActionApproval,
    *,
    timezone_name: str,
    owner: bool,
) -> CockpitAutomationApprovalItem:
    candidate = item.candidate
    reasons = tuple(
        dict.fromkeys(
            _REASON_LABELS.get(reason, "требуется подтверждение владельца")
            for reason in item.approval_reasons
        )
    )
    return CockpitAutomationApprovalItem(
        id=item.id,
        request_fingerprint=item.request_fingerprint,
        status=item.status.value,
        status_label=_STATUS_LABELS[item.status],
        title=_ACTION_LABELS.get(candidate.action, "Автоматическое действие"),
        channel_label=(
            None
            if candidate.channel is None
            else _CHANNEL_LABELS.get(candidate.channel, "Подключённый канал")
        ),
        reason=("; ".join(reasons) if item.status == AutomationApprovalStatus.PENDING else None),
        amount_display=_money_display(item),
        requested_at=_local_timestamp(item.requested_at, timezone_name=timezone_name) or "",
        expires_at=_local_timestamp(item.expires_at, timezone_name=timezone_name) or "",
        can_approve=owner and item.status == AutomationApprovalStatus.PENDING,
        can_reject=owner and item.status == AutomationApprovalStatus.PENDING,
        can_revoke=owner and item.status == AutomationApprovalStatus.APPROVED,
    )


def build_cockpit_automation(
    *,
    actor: TenantContext,
    business_name: str,
    now: datetime | None = None,
) -> CockpitAutomationSnapshot:
    current = resolve_tenant_context(user_id=actor.user_id, business_id=actor.business_id)
    current.assert_can_manage_business()
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    profile = get_business_profile(actor=current)
    effective = get_effective_automation_policy(actor=current, now=timestamp)
    latest = get_latest_automation_policy(actor=current)
    approvals = list_current_automation_action_approvals(actor=current, now=timestamp, limit=20)
    owner = current.role == PlatformRole.OWNER

    if effective is not None:
        mode = effective.spec.mode
        policy_state = "Согласованная политика действует"
        policy_expires_at = _local_timestamp(effective.spec.expires_at, timezone_name=profile.timezone)
    elif latest is not None:
        mode = AutomationMode.CAUTIOUS
        policy_state = "Согласованной политики сейчас нет — автоматические действия не разрешены"
        policy_expires_at = None
    else:
        mode = AutomationMode.CAUTIOUS
        policy_state = "Политика ещё не настроена"
        policy_expires_at = None

    projected = tuple(
        _approval_item(item, timezone_name=profile.timezone, owner=owner)
        for item in approvals
    )
    return CockpitAutomationSnapshot(
        schema_version=_SCHEMA_VERSION,
        business_id=current.business_id,
        business_name=str(business_name),
        mode=mode.value,
        mode_label=_MODE_LABELS[mode],
        policy_state=policy_state,
        policy_expires_at=policy_expires_at,
        autopilot_enabled=is_owner_autopilot_enabled(actor=current, now=timestamp),
        can_change_policy=owner,
        pending_count=sum(item.status == AutomationApprovalStatus.PENDING for item in approvals),
        approvals=projected,
        safety_note=(
            "Разрешение фиксирует решение владельца, но само по себе не запускает внешнее действие. "
            "Деньги, сообщения и другие внешние операции остаются за отдельными проверяемыми контурами."
        ),
    )


def _resolve_actor(
    *, telegram_user_id: int, requested_business_id: str | None
) -> tuple[TenantContext, str]:
    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if context.onboarding_required or context.business_id is None or context.business_name is None:
        raise TenantAccessDenied("active business membership was not found")
    actor = resolve_tenant_context(user_id=context.user_id, business_id=context.business_id)
    return actor, context.business_name


def resolve_cockpit_automation(
    *, telegram_user_id: int, requested_business_id: str | None = None
) -> CockpitAutomationSnapshot:
    actor, business_name = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    return build_cockpit_automation(actor=actor, business_name=business_name)


def set_cockpit_autopilot(
    *, telegram_user_id: int, requested_business_id: str | None, enabled: bool
) -> CockpitAutomationSnapshot:
    actor, business_name = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be boolean")
    set_owner_autopilot_enabled(actor=actor, enabled=enabled)
    return build_cockpit_automation(actor=actor, business_name=business_name)


def decide_cockpit_automation_approval(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    approval_id: str,
    request_fingerprint: str,
    decision: str,
) -> CockpitAutomationSnapshot:
    actor, business_name = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    approval = get_automation_action_approval(actor=actor, approval_id=approval_id)
    expected = str(request_fingerprint or "").strip().lower()
    if expected != approval.request_fingerprint:
        raise AutomationApprovalConflict("automation_action_approval_changed")
    if decision == "approve":
        approve_automation_action(
            actor=actor,
            approval_id=approval.id,
            expected_request_fingerprint=expected,
        )
    elif decision == "reject":
        reject_automation_action(
            actor=actor,
            approval_id=approval.id,
            expected_request_fingerprint=expected,
        )
    elif decision == "revoke":
        revoke_automation_action_approval(
            actor=actor,
            approval_id=approval.id,
            expected_request_fingerprint=expected,
        )
    else:
        raise ValueError("unsupported automation decision")
    return build_cockpit_automation(actor=actor, business_name=business_name)


__all__ = [
    "CockpitAutomationApprovalItem",
    "CockpitAutomationSnapshot",
    "build_cockpit_automation",
    "decide_cockpit_automation_approval",
    "resolve_cockpit_automation",
    "set_cockpit_autopilot",
]
