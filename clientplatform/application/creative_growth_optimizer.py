from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from clientplatform.application.creative_growth_analytics import CreativeGrowthOutcomeSnapshot
from clientplatform.domain.creative_growth import (
    CreativeAttributionScope,
    CreativeTrialStatus,
    CreativeVariantOutcome,
)


class CreativeOptimizationMetric(StrEnum):
    BOOKINGS = "bookings"
    WON = "won"


class CreativeOptimizationStatus(StrEnum):
    READY = "ready"
    NOT_RUNNING = "not_running"
    ATTRIBUTION_NOT_READY = "attribution_not_ready"
    INSUFFICIENT_DATA = "insufficient_data"
    NO_CLEAR_WINNER = "no_clear_winner"
    EXPLORATION_FLOOR_REACHED = "exploration_floor_reached"


@dataclass(frozen=True, slots=True)
class CreativeOptimizationArmEvidence:
    variant_id: str
    publication_job_id: str
    leads: int
    successes: int
    rate: float
    confidence_low: float
    confidence_high: float
    current_allocation_bps: int
    proposed_allocation_bps: int


@dataclass(frozen=True, slots=True)
class CreativeOptimizationRecommendation:
    trial_id: str
    trial_revision: int
    metric: CreativeOptimizationMetric
    status: CreativeOptimizationStatus
    reason: str
    winner_variant_id: str = ""
    evidence: tuple[CreativeOptimizationArmEvidence, ...] = ()

    @property
    def can_apply(self) -> bool:
        return self.status == CreativeOptimizationStatus.READY

    @property
    def allocations(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            (item.publication_job_id, item.proposed_allocation_bps)
            for item in self.evidence
        )


def _wilson_interval(
    successes: int,
    trials: int,
    *,
    z: float = 1.959963984540054,
) -> tuple[float, float]:
    n = int(trials)
    k = int(successes)
    if n <= 0:
        return (0.0, 1.0)
    if k < 0 or k > n:
        raise ValueError("creative optimization successes must be between zero and leads")
    p = k / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denominator
    margin = (
        z
        * math.sqrt((p * (1.0 - p) / n) + (z2 / (4.0 * n * n)))
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _successes(item: CreativeVariantOutcome, metric: CreativeOptimizationMetric) -> int:
    if metric == CreativeOptimizationMetric.BOOKINGS:
        return int(item.bookings)
    if metric == CreativeOptimizationMetric.WON:
        return int(item.won)
    raise ValueError("unsupported creative optimization metric")


def recommend_creative_growth_allocation(
    snapshot: CreativeGrowthOutcomeSnapshot,
    *,
    metric: CreativeOptimizationMetric = CreativeOptimizationMetric.BOOKINGS,
    min_leads_per_arm: int = 30,
    exploration_floor_bps: int = 1_000,
    max_shift_bps: int = 1_000,
) -> CreativeOptimizationRecommendation:
    """Recommend one bounded allocation step when evidence clearly separates a winner.

    This function does not mutate persistence and never changes the provider's total
    advertising budget. It only proposes how the existing 10,000-bps application
    allocation could move while preserving exploration for every losing arm.
    """

    minimum = int(min_leads_per_arm)
    floor = int(exploration_floor_bps)
    step = int(max_shift_bps)
    chosen_metric = CreativeOptimizationMetric(metric)
    if minimum < 1:
        raise ValueError("creative optimization minimum leads must be positive")
    if floor < 1 or floor >= 5_000:
        raise ValueError(
            "creative optimization exploration floor must be between 1 and 4999 bps"
        )
    if step < 1 or step > 5_000:
        raise ValueError("creative optimization shift must be between 1 and 5000 bps")

    plan = snapshot.plan.normalized()
    base = dict(
        trial_id=plan.trial_id,
        trial_revision=plan.revision,
        metric=chosen_metric,
    )
    if plan.status != CreativeTrialStatus.RUNNING:
        return CreativeOptimizationRecommendation(
            **base,
            status=CreativeOptimizationStatus.NOT_RUNNING,
            reason="creative trial must be running before optimization",
        )

    outcomes = {item.variant_id: item for item in snapshot.variants}
    arm_by_variant = {arm.variant_id: arm for arm in plan.arms}
    if (
        set(outcomes) != set(arm_by_variant)
        or any(
            item.attribution_scope != CreativeAttributionScope.VARIANT
            for item in snapshot.variants
        )
        or any(
            outcomes[variant_id].publication_job_id != arm.publication_job_id
            for variant_id, arm in arm_by_variant.items()
        )
    ):
        return CreativeOptimizationRecommendation(
            **base,
            status=CreativeOptimizationStatus.ATTRIBUTION_NOT_READY,
            reason="exact current per-variant downstream attribution is required",
        )

    if any(int(outcomes[arm.variant_id].leads) < minimum for arm in plan.arms):
        return CreativeOptimizationRecommendation(
            **base,
            status=CreativeOptimizationStatus.INSUFFICIENT_DATA,
            reason=f"each creative variant needs at least {minimum} attributed leads",
        )

    scored: list[tuple[float, float, float, str, CreativeVariantOutcome]] = []
    for arm in plan.arms:
        item = outcomes[arm.variant_id]
        leads = int(item.leads)
        successes = _successes(item, chosen_metric)
        low, high = _wilson_interval(successes, leads)
        scored.append((successes / leads, low, high, arm.variant_id, item))
    scored.sort(key=lambda row: (-row[0], row[3]))
    _, winner_low, _, winner_id, _ = scored[0]
    strongest_competitor_upper = max(row[2] for row in scored[1:])
    if winner_low <= strongest_competitor_upper:
        return CreativeOptimizationRecommendation(
            **base,
            status=CreativeOptimizationStatus.NO_CLEAR_WINNER,
            reason="95% confidence intervals still overlap; keep collecting evidence",
        )

    allocations = {arm.variant_id: int(arm.allocation_bps) for arm in plan.arms}
    losers = [row for row in scored[1:]]
    available = sum(max(0, allocations[row[3]] - floor) for row in losers)
    shift = min(step, available)
    if shift <= 0:
        return CreativeOptimizationRecommendation(
            **base,
            status=CreativeOptimizationStatus.EXPLORATION_FLOOR_REACHED,
            reason="all losing variants are already at the configured exploration floor",
            winner_variant_id=winner_id,
        )

    remaining = shift
    # Take the bounded step from weakest observed variants first, but never below
    # the exploration floor. This is deterministic and keeps every arm alive.
    for _, _, _, variant_id, _ in sorted(losers, key=lambda row: (row[0], row[3])):
        removable = max(0, allocations[variant_id] - floor)
        take = min(removable, remaining)
        allocations[variant_id] -= take
        remaining -= take
        if remaining == 0:
            break
    allocations[winner_id] += shift - remaining

    evidence: list[CreativeOptimizationArmEvidence] = []
    score_by_variant = {row[3]: row for row in scored}
    for arm in plan.arms:
        rate, low, high, _, item = score_by_variant[arm.variant_id]
        evidence.append(
            CreativeOptimizationArmEvidence(
                variant_id=arm.variant_id,
                publication_job_id=arm.publication_job_id,
                leads=int(item.leads),
                successes=_successes(item, chosen_metric),
                rate=rate,
                confidence_low=low,
                confidence_high=high,
                current_allocation_bps=int(arm.allocation_bps),
                proposed_allocation_bps=allocations[arm.variant_id],
            )
        )

    if sum(item.proposed_allocation_bps for item in evidence) != 10_000:
        raise RuntimeError("creative optimization allocation invariant failed")
    return CreativeOptimizationRecommendation(
        **base,
        status=CreativeOptimizationStatus.READY,
        reason=(
            "one creative variant is statistically separated; bounded reallocation "
            "is safe to propose"
        ),
        winner_variant_id=winner_id,
        evidence=tuple(evidence),
    )


__all__ = [
    "CreativeOptimizationArmEvidence",
    "CreativeOptimizationMetric",
    "CreativeOptimizationRecommendation",
    "CreativeOptimizationStatus",
    "recommend_creative_growth_allocation",
]
