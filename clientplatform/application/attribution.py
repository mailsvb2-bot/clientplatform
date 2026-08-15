from __future__ import annotations

from clientplatform.domain.attribution import AttributionTrace
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.attribution_repository import AttributionRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db_ro


def get_customer_acquisition_trace(
    *,
    actor: TenantContext,
    customer_id: str,
) -> AttributionTrace | None:
    """Return raw customer-linked acquisition provenance to trusted business operators."""

    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_attribution_spine()
        return AttributionRepository(conn).get_customer_trace(
            business_id=current.business_id,
            customer_id=customer_id,
        )


def get_booking_acquisition_trace(
    *,
    actor: TenantContext,
    booking_slot_id: str,
) -> AttributionTrace | None:
    """Return the immutable first touch attributed to one booked slot."""

    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_attribution_spine()
        return AttributionRepository(conn).get_booking_trace(
            business_id=current.business_id,
            booking_slot_id=booking_slot_id,
        )
