from __future__ import annotations

from datetime import date, datetime

from clientplatform.application.creative_growth_analytics import (
    get_creative_growth_outcomes,
)
from clientplatform.application.creative_growth_optimizer import (
    CreativeOptimizationMetric,
    CreativeOptimizationRecommendation,
    recommend_creative_growth_allocation,
)
from clientplatform.domain.creative_growth import CreativeTrafficPlan
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.creative_growth_optimization_repository import (
    CreativeGrowthOptimizationRepository,
)
from services.db import get_db


def get_creative_growth_recommendation(
    *,
    actor: TenantContext,
    trial_id: str,
    days: int = 30,
    now: datetime | date | None = None,
    metric: CreativeOptimizationMetric = CreativeOptimizationMetric.BOOKINGS,
    min_leads_per_arm: int = 30,
    exploration_floor_bps: int = 1_000,
    max_shift_bps: int = 1_000,
) -> CreativeOptimizationRecommendation:
    snapshot = get_creative_growth_outcomes(
        actor=actor,
        trial_id=trial_id,
        days=days,
        now=now,
    )
    return recommend_creative_growth_allocation(
        snapshot,
        metric=metric,
        min_leads_per_arm=min_leads_per_arm,
        exploration_floor_bps=exploration_floor_bps,
        max_shift_bps=max_shift_bps,
    )


def apply_creative_growth_recommendation(
    *,
    actor: TenantContext,
    recommendation: CreativeOptimizationRecommendation,
    confirmed: bool,
) -> CreativeTrafficPlan:
    """Apply a reviewed recommendation only with explicit confirmation and CAS."""

    if confirmed is not True:
        raise ValueError("creative optimization requires explicit confirmation")
    if not recommendation.can_apply:
        raise ValueError("creative optimization recommendation is not applicable")
    allocations = tuple(
        (
            item.variant_id,
            item.publication_job_id,
            item.current_allocation_bps,
            item.proposed_allocation_bps,
        )
        for item in recommendation.evidence
    )
    with get_db() as conn:
        return CreativeGrowthOptimizationRepository(conn).apply(
            actor=actor,
            trial_id=recommendation.trial_id,
            expected_revision=recommendation.trial_revision,
            allocations=allocations,
        )


__all__ = [
    "apply_creative_growth_recommendation",
    "get_creative_growth_recommendation",
]
