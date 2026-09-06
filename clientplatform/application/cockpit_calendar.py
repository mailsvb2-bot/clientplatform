from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.application.activity import get_business_profile
from clientplatform.application.bookings import list_booking_slots
from clientplatform.application.cockpit import resolve_cockpit_context
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.tenancy import TenantAccessDenied, TenantContext

_SCHEMA_VERSION = "2026-09-06.v1"
_MAX_ITEMS = 50


class CockpitCalendarUnavailable(RuntimeError):
    """Canonical calendar projection data is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class CockpitCalendarItem:
    slot_id: str
    offering_title: str
    starts_at: str
    local_start: str
    duration_minutes: int
    status: str


@dataclass(frozen=True, slots=True)
class CockpitCalendarSnapshot:
    schema_version: str
    business_id: str
    business_name: str
    timezone_name: str
    as_of: str
    items: tuple[CockpitCalendarItem, ...]
    has_more: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _bounded_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("limit must be a positive integer")
    return min(value, _MAX_ITEMS)


def build_cockpit_calendar(
    *,
    actor: TenantContext,
    business_name: str,
    limit: int = 30,
    now: datetime | None = None,
    booking_loader: Callable[..., list[object]] = list_booking_slots,
) -> CockpitCalendarSnapshot:
    """Project upcoming canonical booking slots for the first-party Mini App."""

    current = resolve_tenant_context(user_id=actor.user_id, business_id=actor.business_id)
    current.assert_can_view_customer_records()
    selected_limit = _bounded_limit(limit)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("cockpit calendar now must be timezone-aware")
    profile = get_business_profile(actor=current)
    timezone_name = str(profile.timezone)
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CockpitCalendarUnavailable("business timezone is invalid") from exc
    views = booking_loader(actor=current, include_unavailable=True)
    projected: list[CockpitCalendarItem] = []
    for view in views:
        slot = getattr(view, "slot", view)
        status = str(getattr(getattr(slot, "status", ""), "value", getattr(slot, "status", "")))
        if status not in {"open", "booked"}:
            continue
        starts_at = str(getattr(slot, "starts_at", ""))
        try:
            parsed = datetime.fromisoformat(starts_at)
        except ValueError as exc:
            raise CockpitCalendarUnavailable("booking start is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise CockpitCalendarUnavailable("booking start is missing timezone")
        if parsed.astimezone(timezone.utc) < current_time.astimezone(timezone.utc):
            continue
        try:
            local_start = str(getattr(view, "local_start", starts_at))
        except ValueError as exc:
            raise CockpitCalendarUnavailable("booking local time is invalid") from exc
        projected.append(
            CockpitCalendarItem(
                slot_id=str(getattr(slot, "id", "")),
                offering_title=str(getattr(view, "offering_title", "Услуга") or "Услуга"),
                starts_at=starts_at,
                local_start=local_start,
                duration_minutes=int(getattr(slot, "duration_minutes", 0) or 0),
                status=status,
            )
        )
    projected.sort(key=lambda item: (item.starts_at, item.slot_id))
    has_more = len(projected) > selected_limit
    return CockpitCalendarSnapshot(
        schema_version=_SCHEMA_VERSION,
        business_id=current.business_id,
        business_name=str(business_name),
        timezone_name=timezone_name,
        as_of=current_time.astimezone(timezone.utc).isoformat(),
        items=tuple(projected[:selected_limit]),
        has_more=has_more,
    )


def resolve_cockpit_calendar(
    *,
    telegram_user_id: int,
    requested_business_id: str | None = None,
    limit: int = 30,
) -> CockpitCalendarSnapshot:
    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if context.onboarding_required or context.business_id is None or context.business_name is None:
        raise TenantAccessDenied("active business membership was not found")
    actor = resolve_tenant_context(user_id=context.user_id, business_id=context.business_id)
    return build_cockpit_calendar(
        actor=actor,
        business_name=context.business_name,
        limit=limit,
    )


__all__ = [
    "CockpitCalendarItem",
    "CockpitCalendarUnavailable",
    "CockpitCalendarSnapshot",
    "build_cockpit_calendar",
    "resolve_cockpit_calendar",
]
