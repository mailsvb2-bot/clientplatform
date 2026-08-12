from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from clientplatform.domain.creative_growth import (
    CreativeAttributionScope,
    CreativeTrafficPlan,
    CreativeVariantOutcome,
)


class CreativeWinnerDecision(StrEnum):
    INSUFFICIENT_ATTRIBUTION = "insufficient_attribution"
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    HOLD = "hold"
    SHIFT = "shift"


class CreativeWinnerMetric(StrEnum):
    BOOKINGS = "bookings"
    WON = "won"


@dataclass(frozen=True, slots=True)
class CreativeWinnerPolicy:
    """Conservative policy for evidence-driven traffic reallocation.

    A recommendation is intentionally harder to produce than a simple rate
    comparison: every arm needs exact downstream attribution and enough
    attributed opens; the selected conversion metric needs enough events; and
    the winner's Wilson lower bound must exceed every other arm's upper bound.
    """

    min_leads_per_arm: int = 30
    min_total_bookings: int = 8
    min_total_won: int = 5
    min_rate_delta: float = 0.05
    confidence_z: float = 1.96
    max_shift_bps: int = 1_000
    min_arm_allocation_bps: int = 1_000
    max_winner_allocation_bps: int = 8_000

    def __post_init__(self) -> None:
        if self.min_leads_per_arm < 1:
            raise ValueError("winner policy min_leads_per_arm must be positive")
        if self.min_total_bookings < 1 or self.min_total_won < 1:
            raise ValueError("winner policy event thresholds must be positive")
        if not 0.0 < self.min_rate_delta <= 1.0:
            raise ValueError("winner policy min_rate_delta must be in (0, 1]")
        if self.confidence_z <= 0.0:
            raise ValueError("winner policy confidence_z must be positive")
        if self.max_shift_bps < 1 or self.max_shift_bps > 10_000:
            raise ValueError("winner policy max_shift_bps is invalid")
        if self.min_arm_allocation_bps < 1:
            raise ValueError("winner policy exploration floor must be positive")
        if self.max_winner_allocation_bps > 10_000:
            raise ValueError("winner policy winner ceiling must not exceed 10000")
        if self.max_winner_allocation_bps <= self.min_arm_allocation_bps:
            raise ValueError("winner policy ceiling must exceed exploration floor")


@dataclass(frozen=True, slots=True)
class CreativeWinnerEvidence:
    variant_id: str
    leads: int
    successes: int
    rate: float
    lower_bound: float
    upper_bound: float


@dataclass(frozen=True, slots=True)
class CreativeWinnerRecommendation:
    decision: CreativeWinnerDecision
    reason: str
    trial_id: str
    expected_revision: int
    metric: CreativeWinnerMetric | None
    winner_variant_id: str
    evidence: tuple[CreativeWinnerEvidence, ...]
    recommended_allocations: tuple[tuple[str, int], ...]

    @property
    def can_apply(self) -> bool:
        return self.decision == CreativeWinnerDecision.SHIFT


def _wilson_interval(successes: int, total: int, z: float) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 1.0
    p = successes / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre = p + z2 / (2.0 * total)
    radius = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    lower = max(0.0, (centre - radius) / denominator)
    upper = min(1.0, (centre + radius) / denominator)
    return lower, upper


def _current_allocations(plan: CreativeTrafficPlan) -> tuple[tuple[str, int], ...]:
    return tuple((arm.publication_job_id, arm.allocation_bps) for arm in plan.arms)


def _validate_outcomes(
    plan: CreativeTrafficPlan,
    outcomes: tuple[CreativeVariantOutcome, ...],
) -> dict[str, CreativeVariantOutcome]:
    by_variant = {item.variant_id: item for item in outcomes}
    if len(by_variant) != len(outcomes):
        raise ValueError("creative winner outcomes contain duplicate variants")
    expected = {arm.variant_id for arm in plan.arms}
    if set(by_variant) != expected:
        raise ValueError("creative winner outcomes do not match trial variants")
    for arm in plan.arms:
        item = by_variant[arm.variant_id]
        if item.publication_job_id != arm.publication_job_id:
            raise ValueError("creative winner outcome publication job mismatch")
        if min(item.leads, item.bookings, item.won) < 0:
            raise ValueError("creative winner outcomes must not be negative")
        if item.bookings > item.leads or item.won > item.bookings:
            raise ValueError("creative winner outcome funnel is inconsistent")
    return by_variant


def _recommend_allocations(
    *,
    plan: CreativeTrafficPlan,
    winner_variant_id: str,
    rates: dict[str, float],
    policy: CreativeWinnerPolicy,
) -> tuple[tuple[str, int], ...] | None:
    allocations = {arm.variant_id: arm.allocation_bps for arm in plan.arms}
    winner_current = allocations[winner_variant_id]
    winner_capacity = policy.max_winner_allocation_bps - winner_current
    removable = sum(
        max(0, allocation - policy.min_arm_allocation_bps)
        for variant_id, allocation in allocations.items()
        if variant_id != winner_variant_id
    )
    shift = min(policy.max_shift_bps, winner_capacity, removable)
    if shift <= 0:
        return None

    remaining = shift
    losers = sorted(
        (variant_id for variant_id in allocations if variant_id != winner_variant_id),
        key=lambda variant_id: (rates[variant_id], variant_id),
    )
    for variant_id in losers:
        available = max(0, allocations[variant_id] - policy.min_arm_allocation_bps)
        take = min(available, remaining)
        allocations[variant_id] -= take
        remaining -= take
        if remaining == 0:
            break
    if remaining != 0:
        raise RuntimeError("creative winner allocation shift invariant failed")
    allocations[winner_variant_id] += shift
    if sum(allocations.values()) != 10_000:
        raise RuntimeError("creative winner allocation total invariant failed")
    return tuple(
        (arm.publication_job_id, allocations[arm.variant_id])
        for arm in plan.arms
    )


def recommend_creative_winner(
    *,
    plan: CreativeTrafficPlan,
    outcomes: tuple[CreativeVariantOutcome, ...],
    policy: CreativeWinnerPolicy | None = None,
) -> CreativeWinnerRecommendation:
    normalized_plan = plan.normalized()
    rules = policy or CreativeWinnerPolicy()
    by_variant = _validate_outcomes(normalized_plan, outcomes)
    current = _current_allocations(normalized_plan)

    if any(
        by_variant[arm.variant_id].attribution_scope != CreativeAttributionScope.VARIANT
        for arm in normalized_plan.arms
    ):
        return CreativeWinnerRecommendation(
            decision=CreativeWinnerDecision.INSUFFICIENT_ATTRIBUTION,
            reason="exact_variant_attribution_required",
            trial_id=normalized_plan.trial_id,
            expected_revision=normalized_plan.revision,
            metric=None,
            winner_variant_id="",
            evidence=(),
            recommended_allocations=current,
        )

    if any(
        by_variant[arm.variant_id].leads < rules.min_leads_per_arm
        for arm in normalized_plan.arms
    ):
        return CreativeWinnerRecommendation(
            decision=CreativeWinnerDecision.INSUFFICIENT_SAMPLE,
            reason="minimum_attributed_opens_not_reached",
            trial_id=normalized_plan.trial_id,
            expected_revision=normalized_plan.revision,
            metric=None,
            winner_variant_id="",
            evidence=(),
            recommended_allocations=current,
        )

    total_won = sum(by_variant[arm.variant_id].won for arm in normalized_plan.arms)
    total_bookings = sum(by_variant[arm.variant_id].bookings for arm in normalized_plan.arms)
    if total_won >= rules.min_total_won:
        metric = CreativeWinnerMetric.WON
        success_field = "won"
    elif total_bookings >= rules.min_total_bookings:
        metric = CreativeWinnerMetric.BOOKINGS
        success_field = "bookings"
    else:
        return CreativeWinnerRecommendation(
            decision=CreativeWinnerDecision.INSUFFICIENT_SAMPLE,
            reason="minimum_conversion_events_not_reached",
            trial_id=normalized_plan.trial_id,
            expected_revision=normalized_plan.revision,
            metric=None,
            winner_variant_id="",
            evidence=(),
            recommended_allocations=current,
        )

    evidence: list[CreativeWinnerEvidence] = []
    for arm in normalized_plan.arms:
        item = by_variant[arm.variant_id]
        successes = int(getattr(item, success_field))
        rate = successes / item.leads
        lower, upper = _wilson_interval(successes, item.leads, rules.confidence_z)
        evidence.append(
            CreativeWinnerEvidence(
                variant_id=arm.variant_id,
                leads=item.leads,
                successes=successes,
                rate=rate,
                lower_bound=lower,
                upper_bound=upper,
            )
        )

    ranked = sorted(evidence, key=lambda item: (-item.rate, item.variant_id))
    winner = ranked[0]
    runner_up = ranked[1]
    if winner.rate == runner_up.rate:
        return CreativeWinnerRecommendation(
            decision=CreativeWinnerDecision.HOLD,
            reason="top_conversion_rates_are_tied",
            trial_id=normalized_plan.trial_id,
            expected_revision=normalized_plan.revision,
            metric=metric,
            winner_variant_id="",
            evidence=tuple(evidence),
            recommended_allocations=current,
        )
    if winner.rate - runner_up.rate < rules.min_rate_delta:
        return CreativeWinnerRecommendation(
            decision=CreativeWinnerDecision.HOLD,
            reason="conversion_delta_below_policy_threshold",
            trial_id=normalized_plan.trial_id,
            expected_revision=normalized_plan.revision,
            metric=metric,
            winner_variant_id=winner.variant_id,
            evidence=tuple(evidence),
            recommended_allocations=current,
        )
    if winner.lower_bound <= max(item.upper_bound for item in ranked[1:]):
        return CreativeWinnerRecommendation(
            decision=CreativeWinnerDecision.HOLD,
            reason="confidence_intervals_overlap",
            trial_id=normalized_plan.trial_id,
            expected_revision=normalized_plan.revision,
            metric=metric,
            winner_variant_id=winner.variant_id,
            evidence=tuple(evidence),
            recommended_allocations=current,
        )

    rates = {item.variant_id: item.rate for item in evidence}
    recommended = _recommend_allocations(
        plan=normalized_plan,
        winner_variant_id=winner.variant_id,
        rates=rates,
        policy=rules,
    )
    if recommended is None:
        return CreativeWinnerRecommendation(
            decision=CreativeWinnerDecision.HOLD,
            reason="allocation_policy_bound_reached",
            trial_id=normalized_plan.trial_id,
            expected_revision=normalized_plan.revision,
            metric=metric,
            winner_variant_id=winner.variant_id,
            evidence=tuple(evidence),
            recommended_allocations=current,
        )
    return CreativeWinnerRecommendation(
        decision=CreativeWinnerDecision.SHIFT,
        reason="statistically_separated_conversion_rate",
        trial_id=normalized_plan.trial_id,
        expected_revision=normalized_plan.revision,
        metric=metric,
        winner_variant_id=winner.variant_id,
        evidence=tuple(evidence),
        recommended_allocations=recommended,
    )


__all__ = [
    "CreativeWinnerDecision",
    "CreativeWinnerEvidence",
    "CreativeWinnerMetric",
    "CreativeWinnerPolicy",
    "CreativeWinnerRecommendation",
    "recommend_creative_winner",
]
