from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.application.activity import get_business_profile
from clientplatform.application.cockpit import resolve_cockpit_context
from clientplatform.application.sales_workspace import sales_workspace_snapshot
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.tenancy import TenantAccessDenied, TenantContext

_SCHEMA_VERSION = "2026-09-07.v2"
_MAX_ITEMS = 30
_RECENT_LOST_LIMIT = 5
class CockpitSalesUnavailable(RuntimeError):
    """Canonical sales projection metadata is unavailable or invalid."""


_STAGE_LABELS = {
    "new": "Новый",
    "contacted": "Связались",
    "qualified": "Интерес подтверждён",
    "checkout": "Оформление",
    "won": "Оплатил / выиграно",
    "lost": "Не состоялось",
}


@dataclass(frozen=True, slots=True)
class CockpitSalesItem:
    lead_id: str
    customer_id: str
    customer_name: str
    stage: str
    stage_label: str
    next_action: str | None
    due_at: str | None
    due_display: str | None
    overdue: bool
    source_kind: str | None


@dataclass(frozen=True, slots=True)
class CockpitSalesSnapshot:
    schema_version: str
    business_id: str
    business_name: str
    timezone_name: str
    as_of: str
    items: tuple[CockpitSalesItem, ...]
    recent_lost: tuple[CockpitSalesItem, ...]
    handoff_count: int
    has_more: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _bounded_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("limit must be a positive integer")
    return min(value, _MAX_ITEMS)


def _due_projection(value: object, *, zone: ZoneInfo, now: datetime) -> tuple[str | None, str | None, bool]:
    raw = str(value or "").strip()
    if not raw:
        return None, None, False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, None, False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, None, False
    due_utc = parsed.astimezone(timezone.utc)
    return raw, due_utc.astimezone(zone).strftime("%d.%m %H:%M"), due_utc <= now


def _project_sales_item(
    raw_item: object,
    *,
    zone: ZoneInfo,
    now: datetime,
) -> CockpitSalesItem | None:
    if not isinstance(raw_item, dict):
        return None
    stage = str(raw_item.get("stage") or "new")
    due_at, due_display, overdue = _due_projection(
        raw_item.get("due_at"),
        zone=zone,
        now=now,
    )
    next_action = " ".join(str(raw_item.get("next_action") or "").split()) or None
    return CockpitSalesItem(
        lead_id=str(raw_item.get("id") or ""),
        customer_id=str(raw_item.get("customer_id") or ""),
        customer_name=(
            " ".join(str(raw_item.get("customer_name") or "Клиент").split())
            or "Клиент"
        ),
        stage=stage,
        stage_label=_STAGE_LABELS.get(stage, stage),
        next_action=next_action,
        due_at=due_at,
        due_display=due_display,
        overdue=overdue,
        source_kind=(str(raw_item.get("source_kind") or "").strip() or None),
    )


def build_cockpit_sales(
    *,
    actor: TenantContext,
    business_name: str,
    limit: int = 20,
    now: datetime | None = None,
    workspace_loader: Callable[..., object] = sales_workspace_snapshot,
) -> CockpitSalesSnapshot:
    """Project canonical open work plus reopenable recent losses for the Mini App."""

    current = resolve_tenant_context(user_id=actor.user_id, business_id=actor.business_id)
    current.assert_can_view_customer_records()
    selected_limit = _bounded_limit(limit)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("cockpit sales now must be timezone-aware")
    current_time = current_time.astimezone(timezone.utc)
    profile = get_business_profile(actor=current)
    timezone_name = str(profile.timezone)
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CockpitSalesUnavailable("business timezone is invalid") from exc
    snapshot = workspace_loader(actor=current, limit=selected_limit + 1)
    open_work = tuple(getattr(snapshot, "open_work", ()))
    recent_closed = tuple(getattr(snapshot, "recent_closed", ()))

    items: list[CockpitSalesItem] = []
    for raw_item in open_work[:selected_limit]:
        projected = _project_sales_item(raw_item, zone=zone, now=current_time)
        if projected is not None:
            items.append(projected)

    recent_lost: list[CockpitSalesItem] = []
    for raw_item in recent_closed:
        if not isinstance(raw_item, dict) or str(raw_item.get("stage") or "") != "lost":
            continue
        projected = _project_sales_item(raw_item, zone=zone, now=current_time)
        if projected is None:
            continue
        recent_lost.append(projected)
        if len(recent_lost) >= _RECENT_LOST_LIMIT:
            break

    return CockpitSalesSnapshot(
        schema_version=_SCHEMA_VERSION,
        business_id=current.business_id,
        business_name=str(business_name),
        timezone_name=timezone_name,
        as_of=current_time.isoformat(),
        items=tuple(items),
        recent_lost=tuple(recent_lost),
        handoff_count=max(0, int(getattr(snapshot, "handoff_count", 0) or 0)),
        has_more=len(open_work) > selected_limit,
    )


def resolve_cockpit_sales(
    *,
    telegram_user_id: int,
    requested_business_id: str | None = None,
    limit: int = 20,
) -> CockpitSalesSnapshot:
    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if context.onboarding_required or context.business_id is None or context.business_name is None:
        raise TenantAccessDenied("active business membership was not found")
    actor = resolve_tenant_context(user_id=context.user_id, business_id=context.business_id)
    return build_cockpit_sales(
        actor=actor,
        business_name=context.business_name,
        limit=limit,
    )


__all__ = [
    "CockpitSalesItem",
    "CockpitSalesUnavailable",
    "CockpitSalesSnapshot",
    "build_cockpit_sales",
    "resolve_cockpit_sales",
]
