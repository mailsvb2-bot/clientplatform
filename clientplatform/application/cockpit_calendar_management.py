from __future__ import annotations

from dataclasses import asdict, dataclass

from clientplatform.application.activity import (
    get_business_profile,
    list_business_capabilities,
    list_business_offerings,
)
from clientplatform.application.bookings import create_booking_slot
from clientplatform.application.cockpit import resolve_cockpit_context
from clientplatform.application.owner_booking_journey import (
    cancel_owner_booking_slot,
    replace_owner_booking_slot,
)
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.activity import OfferingStatus
from clientplatform.domain.bookings import BookingSlotView
from clientplatform.domain.tenancy import TenantAccessDenied, TenantContext

_SCHEMA_VERSION = "2026-09-06.v1"


@dataclass(frozen=True, slots=True)
class CockpitCalendarOffering:
    id: str
    title: str


@dataclass(frozen=True, slots=True)
class CockpitCalendarManagementSnapshot:
    schema_version: str
    business_id: str
    business_name: str
    timezone_name: str
    offerings: tuple[CockpitCalendarOffering, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _current_calendar_manager(actor: TenantContext) -> TenantContext:
    current = resolve_tenant_context(
        user_id=actor.user_id,
        business_id=actor.business_id,
    )
    # Management lives inside the customer-record calendar surface. Requiring
    # both assertions prevents a content-only role from gaining access to booked
    # appointment metadata merely because it can edit programs.
    current.assert_can_view_customer_records()
    current.assert_can_manage_programs()
    return current


def build_cockpit_calendar_management(
    *,
    actor: TenantContext,
    business_name: str,
) -> CockpitCalendarManagementSnapshot:
    """Project only canonical booking-management choices for the Mini App."""

    current = _current_calendar_manager(actor)
    profile = get_business_profile(actor=current)
    offerings: list[CockpitCalendarOffering] = []
    for capability in list_business_capabilities(actor=current):
        for offering in list_business_offerings(
            actor=current,
            capability_id=capability.id,
        ):
            if offering.status != OfferingStatus.ACTIVE:
                continue
            offerings.append(
                CockpitCalendarOffering(
                    id=str(offering.id),
                    title=str(offering.title),
                )
            )
    offerings.sort(key=lambda item: (item.title.casefold(), item.id))
    return CockpitCalendarManagementSnapshot(
        schema_version=_SCHEMA_VERSION,
        business_id=current.business_id,
        business_name=str(business_name),
        timezone_name=str(profile.timezone),
        offerings=tuple(offerings),
    )


def _resolve_manager(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
) -> tuple[TenantContext, str]:
    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if (
        context.onboarding_required
        or context.business_id is None
        or context.business_name is None
    ):
        raise TenantAccessDenied("active business membership was not found")
    actor = resolve_tenant_context(
        user_id=context.user_id,
        business_id=context.business_id,
    )
    return _current_calendar_manager(actor), context.business_name


def resolve_cockpit_calendar_management(
    *,
    telegram_user_id: int,
    requested_business_id: str | None = None,
) -> CockpitCalendarManagementSnapshot:
    actor, business_name = _resolve_manager(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    return build_cockpit_calendar_management(
        actor=actor,
        business_name=business_name,
    )


def create_cockpit_calendar_slot(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    offering_id: str,
    local_start: str,
    duration_minutes: int,
) -> BookingSlotView:
    actor, _business_name = _resolve_manager(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    return create_booking_slot(
        actor=actor,
        offering_id=offering_id,
        local_start=local_start,
        duration_minutes=duration_minutes,
    )


def replace_cockpit_calendar_slot(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    slot_id: str,
    local_start: str,
    duration_minutes: int,
) -> BookingSlotView:
    actor, _business_name = _resolve_manager(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    return replace_owner_booking_slot(
        actor=actor,
        slot_id=slot_id,
        local_start=local_start,
        duration_minutes=duration_minutes,
    )


def cancel_cockpit_calendar_slot(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    slot_id: str,
) -> BookingSlotView:
    actor, _business_name = _resolve_manager(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    return cancel_owner_booking_slot(actor=actor, slot_id=slot_id)


__all__ = [
    "CockpitCalendarManagementSnapshot",
    "CockpitCalendarOffering",
    "build_cockpit_calendar_management",
    "cancel_cockpit_calendar_slot",
    "create_cockpit_calendar_slot",
    "replace_cockpit_calendar_slot",
    "resolve_cockpit_calendar_management",
]
