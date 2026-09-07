from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.application.activity import get_business_profile
from clientplatform.application.cockpit import resolve_cockpit_context
from clientplatform.application.sales_workspace import (
    add_sales_workspace_note,
    assign_sales_workspace_to_actor,
    get_sales_workspace_item,
    reopen_sales_workspace,
    set_sales_workspace_next_action,
    transition_sales_workspace,
    unassign_sales_workspace,
)
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.sales import SalesInvariantViolation, SalesLeadNotFound, SalesLeadStage
from clientplatform.domain.tenancy import TenantAccessDenied, TenantContext

_SCHEMA_VERSION = "2026-09-07.v1"
_STAGE_LABELS = {
    "new": "Новый",
    "contacted": "Связались",
    "qualified": "Интерес подтверждён",
    "checkout": "Оформление",
    "won": "Оплатил / выиграно",
    "lost": "Не состоялось",
}
_OPEN_STAGES = frozenset({"new", "contacted", "qualified", "checkout"})


@dataclass(frozen=True, slots=True)
class CockpitSalesLeadManagement:
    schema_version: str
    business_id: str
    business_name: str
    timezone_name: str
    lead_id: str
    customer_id: str
    customer_name: str
    stage: str
    stage_label: str
    assigned: bool
    assigned_to_me: bool
    next_action: str | None
    due_at: str | None
    due_local_value: str | None
    closure_reason: str | None
    source_kind: str | None
    closed: bool
    can_reopen: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _business_zone(actor: TenantContext) -> ZoneInfo:
    profile = get_business_profile(actor=actor)
    timezone_name = str(profile.timezone or "").strip()
    if not timezone_name:
        raise ValueError("business timezone is required")
    try:
        return ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("business timezone is invalid") from exc


def _current_sales_manager(actor: TenantContext) -> TenantContext:
    current = resolve_tenant_context(
        user_id=actor.user_id,
        business_id=actor.business_id,
    )
    current.assert_can_manage_customer_records()
    return current


def _resolve_manager(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
) -> tuple[TenantContext, str, ZoneInfo]:
    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if context.onboarding_required or context.business_id is None or context.business_name is None:
        raise TenantAccessDenied("active business membership was not found")
    actor = _current_sales_manager(
        resolve_tenant_context(
            user_id=context.user_id,
            business_id=context.business_id,
        )
    )
    return actor, context.business_name, _business_zone(actor)


def _due_local_value(value: object, *, zone: ZoneInfo) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("sales due timestamp must be timezone-aware")
    return parsed.astimezone(zone).strftime("%Y-%m-%dT%H:%M")


def _local_due_to_utc(value: str | None, *, zone: ZoneInfo) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        local = datetime.strptime(raw, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ValueError("sales due time must look like YYYY-MM-DDTHH:MM") from exc
    occurrences: list[datetime] = []
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) != local or round_trip.fold != fold:
            continue
        occurrences.append(candidate)
    if not occurrences:
        raise ValueError("sales due time does not exist locally because of a timezone transition")
    if len(occurrences) > 1:
        raise ValueError("sales due time is ambiguous because of a timezone transition")
    return occurrences[0].astimezone(timezone.utc).isoformat(timespec="seconds")


def _management_item(
    *,
    actor: TenantContext,
    business_name: str,
    zone: ZoneInfo,
    lead_id: str,
) -> CockpitSalesLeadManagement:
    item = get_sales_workspace_item(actor=actor, lead_id=lead_id)
    if item is None:
        raise SalesLeadNotFound("sales lead was not found in the active business")
    stage = str(item.get("stage") or "new")
    assigned_user = item.get("assigned_user_id")
    return CockpitSalesLeadManagement(
        schema_version=_SCHEMA_VERSION,
        business_id=actor.business_id,
        business_name=str(business_name),
        timezone_name=str(zone.key),
        lead_id=str(item.get("id") or ""),
        customer_id=str(item.get("customer_id") or ""),
        customer_name=" ".join(str(item.get("customer_name") or "Клиент").split()) or "Клиент",
        stage=stage,
        stage_label=_STAGE_LABELS.get(stage, stage),
        assigned=assigned_user is not None,
        assigned_to_me=(assigned_user is not None and int(assigned_user) == int(actor.user_id)),
        next_action=(" ".join(str(item.get("next_action") or "").split()) or None),
        due_at=(str(item.get("due_at") or "").strip() or None),
        due_local_value=_due_local_value(item.get("due_at"), zone=zone),
        closure_reason=(" ".join(str(item.get("closure_reason") or "").split()) or None),
        source_kind=(str(item.get("source_kind") or "").strip() or None),
        closed=stage not in _OPEN_STAGES,
        can_reopen=stage == SalesLeadStage.LOST.value,
    )


def resolve_cockpit_sales_lead(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    lead_id: str,
) -> CockpitSalesLeadManagement:
    actor, business_name, zone = _resolve_manager(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    return _management_item(
        actor=actor,
        business_name=business_name,
        zone=zone,
        lead_id=lead_id,
    )


def _require_open(item: CockpitSalesLeadManagement) -> None:
    if item.closed:
        raise SalesInvariantViolation("closed sales lead cannot be changed by this action")


def assign_cockpit_sales_lead(
    *, telegram_user_id: int, requested_business_id: str | None, lead_id: str
) -> CockpitSalesLeadManagement:
    actor, business_name, zone = _resolve_manager(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    _require_open(_management_item(actor=actor, business_name=business_name, zone=zone, lead_id=lead_id))
    assign_sales_workspace_to_actor(actor=actor, lead_id=lead_id)
    return _management_item(actor=actor, business_name=business_name, zone=zone, lead_id=lead_id)


def unassign_cockpit_sales_lead(
    *, telegram_user_id: int, requested_business_id: str | None, lead_id: str
) -> CockpitSalesLeadManagement:
    actor, business_name, zone = _resolve_manager(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    _require_open(_management_item(actor=actor, business_name=business_name, zone=zone, lead_id=lead_id))
    unassign_sales_workspace(actor=actor, lead_id=lead_id)
    return _management_item(actor=actor, business_name=business_name, zone=zone, lead_id=lead_id)


def set_cockpit_sales_stage(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    lead_id: str,
    stage: str,
    reason: str | None,
) -> CockpitSalesLeadManagement:
    actor, business_name, zone = _resolve_manager(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    selected = SalesLeadStage(str(stage))
    if selected == SalesLeadStage.NEW:
        raise ValueError("use reopen for a lost sales lead")
    if selected in {SalesLeadStage.WON, SalesLeadStage.LOST} and not str(reason or "").strip():
        raise ValueError("closing a sales lead requires a reason")
    transition_sales_workspace(actor=actor, lead_id=lead_id, stage=selected, reason=reason)
    return _management_item(actor=actor, business_name=business_name, zone=zone, lead_id=lead_id)


def reopen_cockpit_sales_lead(
    *, telegram_user_id: int, requested_business_id: str | None, lead_id: str
) -> CockpitSalesLeadManagement:
    actor, business_name, zone = _resolve_manager(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    current = _management_item(actor=actor, business_name=business_name, zone=zone, lead_id=lead_id)
    if not current.can_reopen:
        raise SalesInvariantViolation("only a lost sales lead can be reopened")
    reopen_sales_workspace(actor=actor, lead_id=lead_id, reason="reopened_from_cockpit")
    return _management_item(actor=actor, business_name=business_name, zone=zone, lead_id=lead_id)


def set_cockpit_sales_next_action(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    lead_id: str,
    next_action: str | None,
    due_local: str | None,
) -> CockpitSalesLeadManagement:
    actor, business_name, zone = _resolve_manager(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    current = _management_item(actor=actor, business_name=business_name, zone=zone, lead_id=lead_id)
    _require_open(current)
    normalized_action = " ".join(str(next_action or "").split()) or None
    due_at = _local_due_to_utc(due_local, zone=zone)
    if due_at is not None and normalized_action is None:
        raise SalesInvariantViolation("due time requires a next action")
    set_sales_workspace_next_action(
        actor=actor,
        lead_id=lead_id,
        next_action=normalized_action,
        due_at=due_at,
    )
    return _management_item(actor=actor, business_name=business_name, zone=zone, lead_id=lead_id)


def add_cockpit_sales_note(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    lead_id: str,
    note: str,
    interaction_key: str,
) -> CockpitSalesLeadManagement:
    actor, business_name, zone = _resolve_manager(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    normalized_key = str(interaction_key or "").strip()
    if not normalized_key or len(normalized_key) > 120:
        raise ValueError("sales note interaction key is invalid")
    add_sales_workspace_note(
        actor=actor,
        lead_id=lead_id,
        note=note,
        interaction_key=f"cockpit:{actor.membership_id}:{normalized_key}",
    )
    return _management_item(actor=actor, business_name=business_name, zone=zone, lead_id=lead_id)


__all__ = [
    "CockpitSalesLeadManagement",
    "add_cockpit_sales_note",
    "assign_cockpit_sales_lead",
    "reopen_cockpit_sales_lead",
    "resolve_cockpit_sales_lead",
    "set_cockpit_sales_next_action",
    "set_cockpit_sales_stage",
    "unassign_cockpit_sales_lead",
]
