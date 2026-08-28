from __future__ import annotations

from datetime import datetime

from clientplatform.domain.outcomes import OutcomeMoney
from clientplatform.domain.revenue_attribution import (
    RevenueAttributionRecord,
    RevenueJourneySnapshot,
    UnitEconomicsSnapshot,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.revenue_attribution_repository import (
    RevenueAttributionRepository,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db


def attribute_monetary_outcome(
    *,
    actor: TenantContext,
    outcome_event_id: str,
) -> RevenueAttributionRecord | None:
    """Persist one explainable first-touch attribution decision for a monetary outcome."""

    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_outcome_ledger()
        current.assert_can_view_attribution_spine()
        return RevenueAttributionRepository(conn).materialize_outcome(
            business_id=current.business_id,
            outcome_event_id=outcome_event_id,
        )


def get_business_unit_economics(
    *,
    actor: TenantContext,
    occurred_from: datetime,
    occurred_to: datetime,
    verified_spend: OutcomeMoney | None = None,
) -> UnitEconomicsSnapshot:
    """Return deterministic economics; optional spend must come from verified provider evidence.

    The application never accepts a UI-entered or LLM-inferred spend value as a business fact.
    Callers that cannot prove spend currency and amount must leave ``verified_spend`` unset;
    the result will explicitly report that limitation and will not manufacture ROAS/CAC/CPL.
    """

    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_outcome_ledger()
        current.assert_can_view_attribution_spine()
        return RevenueAttributionRepository(conn).snapshot(
            business_id=current.business_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            verified_spend=verified_spend,
        )


def get_business_revenue_journey(
    *,
    actor: TenantContext,
    occurred_from: datetime,
    occurred_to: datetime,
) -> RevenueJourneySnapshot:
    """Return the canonical customer/revenue journey without creating new business state."""

    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_outcome_ledger()
        current.assert_can_view_attribution_spine()
        return RevenueAttributionRepository(conn).journey_snapshot(
            business_id=current.business_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
