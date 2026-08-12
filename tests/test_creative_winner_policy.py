from __future__ import annotations

from uuid import uuid4

import pytest

from clientplatform.domain.creative_growth import (
    CreativeAttributionScope,
    CreativeTrafficPlan,
    CreativeTrialArm,
    CreativeTrialStatus,
    CreativeVariantOutcome,
)
from clientplatform.domain.creative_winner_policy import (
    CreativeWinnerDecision,
    CreativeWinnerMetric,
    CreativeWinnerPolicy,
    recommend_creative_winner,
)


def _plan(allocations: tuple[int, ...] = (5000, 5000)) -> CreativeTrafficPlan:
    return CreativeTrafficPlan(
        trial_id=str(uuid4()),
        business_id=str(uuid4()),
        status=CreativeTrialStatus.RUNNING,
        revision=4,
        arms=tuple(
            CreativeTrialArm(
                variant_id=f"variant-{index}",
                publication_job_id=str(uuid4()),
                allocation_bps=allocation,
                promotion_campaign_id=str(uuid4()),
                promotion_source_token=f"variantSource{index}XYZ",
            )
            for index, allocation in enumerate(allocations)
        ),
    ).normalized()


def _outcome(
    plan: CreativeTrafficPlan,
    index: int,
    *,
    leads: int,
    bookings: int,
    won: int = 0,
    scope: CreativeAttributionScope = CreativeAttributionScope.VARIANT,
) -> CreativeVariantOutcome:
    arm = plan.arms[index]
    return CreativeVariantOutcome(
        variant_id=arm.variant_id,
        publication_job_id=arm.publication_job_id,
        promotion_campaign_id=arm.promotion_campaign_id,
        attribution_scope=scope,
        leads=leads,
        bookings=bookings,
        won=won,
    )


def test_exact_clear_booking_winner_gets_only_bounded_shift() -> None:
    plan = _plan()
    recommendation = recommend_creative_winner(
        plan=plan,
        outcomes=(
            _outcome(plan, 0, leads=100, bookings=30),
            _outcome(plan, 1, leads=100, bookings=5),
        ),
    )

    assert recommendation.decision == CreativeWinnerDecision.SHIFT
    assert recommendation.can_apply is True
    assert recommendation.metric == CreativeWinnerMetric.BOOKINGS
    assert recommendation.winner_variant_id == "variant-0"
    assert recommendation.expected_revision == 4
    assert recommendation.recommended_allocations == (
        (plan.arms[0].publication_job_id, 6000),
        (plan.arms[1].publication_job_id, 4000),
    )


def test_won_metric_takes_priority_once_enough_won_evidence_exists() -> None:
    plan = _plan()
    recommendation = recommend_creative_winner(
        plan=plan,
        outcomes=(
            _outcome(plan, 0, leads=100, bookings=30, won=20),
            _outcome(plan, 1, leads=100, bookings=25, won=2),
        ),
    )

    assert recommendation.decision == CreativeWinnerDecision.SHIFT
    assert recommendation.metric == CreativeWinnerMetric.WON
    assert recommendation.winner_variant_id == "variant-0"


def test_shared_or_unavailable_attribution_never_selects_winner() -> None:
    plan = _plan()
    recommendation = recommend_creative_winner(
        plan=plan,
        outcomes=(
            _outcome(
                plan,
                0,
                leads=100,
                bookings=40,
                scope=CreativeAttributionScope.SHARED_CAMPAIGN,
            ),
            _outcome(plan, 1, leads=100, bookings=5),
        ),
    )

    assert recommendation.decision == CreativeWinnerDecision.INSUFFICIENT_ATTRIBUTION
    assert recommendation.reason == "exact_variant_attribution_required"
    assert recommendation.can_apply is False


def test_minimum_sample_is_required_for_every_arm() -> None:
    plan = _plan()
    recommendation = recommend_creative_winner(
        plan=plan,
        outcomes=(
            _outcome(plan, 0, leads=100, bookings=30),
            _outcome(plan, 1, leads=29, bookings=1),
        ),
    )

    assert recommendation.decision == CreativeWinnerDecision.INSUFFICIENT_SAMPLE
    assert recommendation.reason == "minimum_attributed_opens_not_reached"


def test_minimum_conversion_event_count_is_required() -> None:
    plan = _plan()
    recommendation = recommend_creative_winner(
        plan=plan,
        outcomes=(
            _outcome(plan, 0, leads=100, bookings=4),
            _outcome(plan, 1, leads=100, bookings=3),
        ),
    )

    assert recommendation.decision == CreativeWinnerDecision.INSUFFICIENT_SAMPLE
    assert recommendation.reason == "minimum_conversion_events_not_reached"


def test_overlapping_confidence_intervals_hold_allocation() -> None:
    plan = _plan()
    recommendation = recommend_creative_winner(
        plan=plan,
        outcomes=(
            _outcome(plan, 0, leads=100, bookings=20),
            _outcome(plan, 1, leads=100, bookings=15),
        ),
    )

    assert recommendation.decision == CreativeWinnerDecision.HOLD
    assert recommendation.reason == "confidence_intervals_overlap"
    assert recommendation.recommended_allocations == (
        (plan.arms[0].publication_job_id, 5000),
        (plan.arms[1].publication_job_id, 5000),
    )


def test_policy_refuses_shift_after_winner_ceiling_is_reached() -> None:
    plan = _plan((8000, 2000))
    recommendation = recommend_creative_winner(
        plan=plan,
        outcomes=(
            _outcome(plan, 0, leads=100, bookings=35),
            _outcome(plan, 1, leads=100, bookings=2),
        ),
    )

    assert recommendation.decision == CreativeWinnerDecision.HOLD
    assert recommendation.reason == "allocation_policy_bound_reached"


def test_three_arm_shift_removes_traffic_from_worst_rate_first() -> None:
    plan = _plan((4000, 3000, 3000))
    recommendation = recommend_creative_winner(
        plan=plan,
        outcomes=(
            _outcome(plan, 0, leads=150, bookings=60),
            _outcome(plan, 1, leads=150, bookings=15),
            _outcome(plan, 2, leads=150, bookings=3),
        ),
    )

    assert recommendation.decision == CreativeWinnerDecision.SHIFT
    assert recommendation.recommended_allocations == (
        (plan.arms[0].publication_job_id, 5000),
        (plan.arms[1].publication_job_id, 3000),
        (plan.arms[2].publication_job_id, 2000),
    )


def test_outcome_funnel_and_variant_identity_are_validated() -> None:
    plan = _plan()
    with pytest.raises(ValueError, match="funnel is inconsistent"):
        recommend_creative_winner(
            plan=plan,
            outcomes=(
                _outcome(plan, 0, leads=30, bookings=31),
                _outcome(plan, 1, leads=30, bookings=1),
            ),
        )

    wrong = CreativeVariantOutcome(
        variant_id="other-variant",
        publication_job_id=plan.arms[1].publication_job_id,
        promotion_campaign_id=plan.arms[1].promotion_campaign_id,
        attribution_scope=CreativeAttributionScope.VARIANT,
        leads=30,
        bookings=1,
        won=0,
    )
    with pytest.raises(ValueError, match="do not match"):
        recommend_creative_winner(
            plan=plan,
            outcomes=(_outcome(plan, 0, leads=30, bookings=10), wrong),
        )


def test_policy_configuration_rejects_unsafe_bounds() -> None:
    with pytest.raises(ValueError, match="ceiling must exceed"):
        CreativeWinnerPolicy(
            min_arm_allocation_bps=5000,
            max_winner_allocation_bps=5000,
        )
