from __future__ import annotations

from clientplatform.application.creative_growth_analytics import CreativeGrowthOutcomeSnapshot
from clientplatform.application.creative_growth_optimizer import (
    CreativeOptimizationMetric,
    CreativeOptimizationStatus,
    recommend_creative_growth_allocation,
)
from clientplatform.domain.creative_growth import (
    CreativeAttributionScope,
    CreativeTrafficPlan,
    CreativeTrialArm,
    CreativeTrialStatus,
    CreativeVariantOutcome,
)


BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
TRIAL_ID = "22222222-2222-4222-8222-222222222222"
JOB_A = "33333333-3333-4333-8333-333333333333"
JOB_B = "44444444-4444-4444-8444-444444444444"
JOB_C = "55555555-5555-4555-8555-555555555555"
CAMPAIGN = "66666666-6666-4666-8666-666666666666"


def _arm(variant_id: str, job_id: str, allocation: int) -> CreativeTrialArm:
    return CreativeTrialArm(
        variant_id=variant_id,
        publication_job_id=job_id,
        allocation_bps=allocation,
        promotion_campaign_id=CAMPAIGN,
        promotion_source_token=f"creative-{variant_id}",
    )


def _outcome(
    variant_id: str,
    job_id: str,
    *,
    leads: int,
    bookings: int,
    won: int = 0,
    scope: CreativeAttributionScope = CreativeAttributionScope.VARIANT,
) -> CreativeVariantOutcome:
    return CreativeVariantOutcome(
        variant_id=variant_id,
        publication_job_id=job_id,
        promotion_campaign_id=CAMPAIGN,
        attribution_scope=scope,
        leads=leads,
        bookings=bookings,
        won=won,
    )


def _snapshot(
    outcomes: tuple[CreativeVariantOutcome, ...],
    *,
    status: CreativeTrialStatus = CreativeTrialStatus.RUNNING,
    allocations: tuple[int, ...] | None = None,
) -> CreativeGrowthOutcomeSnapshot:
    jobs = (JOB_A, JOB_B, JOB_C)
    default_allocations = (5000, 5000) if len(outcomes) == 2 else (4000, 3000, 3000)
    chosen = allocations or default_allocations
    arms = tuple(
        _arm(outcome.variant_id, jobs[index], chosen[index])
        for index, outcome in enumerate(outcomes)
    )
    plan = CreativeTrafficPlan(
        trial_id=TRIAL_ID,
        business_id=BUSINESS_ID,
        status=status,
        revision=7,
        arms=arms,
    ).normalized()
    return CreativeGrowthOutcomeSnapshot(
        plan=plan,
        date_from="2026-07-14",
        date_to="2026-08-12",
        variants=outcomes,
        shared_campaigns=(),
    )


def test_optimizer_proposes_bounded_shift_for_clear_booking_winner() -> None:
    snapshot = _snapshot(
        (
            _outcome("variant-a", JOB_A, leads=100, bookings=45),
            _outcome("variant-b", JOB_B, leads=100, bookings=10),
        )
    )

    recommendation = recommend_creative_growth_allocation(snapshot)

    assert recommendation.status == CreativeOptimizationStatus.READY
    assert recommendation.can_apply is True
    assert recommendation.winner_variant_id == "variant-a"
    assert recommendation.trial_revision == 7
    assert recommendation.allocations == ((JOB_A, 6000), (JOB_B, 4000))
    assert sum(value for _, value in recommendation.allocations) == 10_000
    assert recommendation.evidence[0].confidence_low > recommendation.evidence[1].confidence_high


def test_optimizer_does_not_pick_winner_while_confidence_intervals_overlap() -> None:
    snapshot = _snapshot(
        (
            _outcome("variant-a", JOB_A, leads=100, bookings=25),
            _outcome("variant-b", JOB_B, leads=100, bookings=20),
        )
    )

    recommendation = recommend_creative_growth_allocation(snapshot)

    assert recommendation.status == CreativeOptimizationStatus.NO_CLEAR_WINNER
    assert recommendation.can_apply is False
    assert recommendation.winner_variant_id == ""
    assert recommendation.allocations == ()


def test_optimizer_requires_minimum_sample_for_every_arm() -> None:
    snapshot = _snapshot(
        (
            _outcome("variant-a", JOB_A, leads=100, bookings=50),
            _outcome("variant-b", JOB_B, leads=29, bookings=1),
        )
    )

    recommendation = recommend_creative_growth_allocation(snapshot)

    assert recommendation.status == CreativeOptimizationStatus.INSUFFICIENT_DATA
    assert "30" in recommendation.reason


def test_optimizer_requires_exact_variant_attribution() -> None:
    snapshot = _snapshot(
        (
            _outcome("variant-a", JOB_A, leads=100, bookings=50),
            _outcome(
                "variant-b",
                JOB_B,
                leads=100,
                bookings=5,
                scope=CreativeAttributionScope.SHARED_CAMPAIGN,
            ),
        )
    )

    recommendation = recommend_creative_growth_allocation(snapshot)

    assert recommendation.status == CreativeOptimizationStatus.ATTRIBUTION_NOT_READY


def test_optimizer_does_not_reallocate_non_running_trial() -> None:
    snapshot = _snapshot(
        (
            _outcome("variant-a", JOB_A, leads=100, bookings=50),
            _outcome("variant-b", JOB_B, leads=100, bookings=5),
        ),
        status=CreativeTrialStatus.PAUSED,
    )

    recommendation = recommend_creative_growth_allocation(snapshot)

    assert recommendation.status == CreativeOptimizationStatus.NOT_RUNNING


def test_optimizer_preserves_exploration_floor() -> None:
    snapshot = _snapshot(
        (
            _outcome("variant-a", JOB_A, leads=200, bookings=120),
            _outcome("variant-b", JOB_B, leads=200, bookings=5),
        ),
        allocations=(9000, 1000),
    )

    recommendation = recommend_creative_growth_allocation(snapshot)

    assert recommendation.status == CreativeOptimizationStatus.EXPLORATION_FLOOR_REACHED
    assert recommendation.winner_variant_id == "variant-a"
    assert recommendation.can_apply is False


def test_optimizer_uses_won_metric_when_requested() -> None:
    snapshot = _snapshot(
        (
            _outcome("variant-a", JOB_A, leads=160, bookings=50, won=35),
            _outcome("variant-b", JOB_B, leads=160, bookings=55, won=5),
        )
    )

    recommendation = recommend_creative_growth_allocation(
        snapshot,
        metric=CreativeOptimizationMetric.WON,
    )

    assert recommendation.status == CreativeOptimizationStatus.READY
    assert recommendation.metric == CreativeOptimizationMetric.WON
    assert recommendation.winner_variant_id == "variant-a"


def test_optimizer_takes_step_from_weakest_variants_first_without_starving_them() -> None:
    snapshot = _snapshot(
        (
            _outcome("variant-a", JOB_A, leads=200, bookings=100),
            _outcome("variant-b", JOB_B, leads=200, bookings=20),
            _outcome("variant-c", JOB_C, leads=200, bookings=2),
        ),
        allocations=(4000, 3000, 3000),
    )

    recommendation = recommend_creative_growth_allocation(
        snapshot,
        exploration_floor_bps=1500,
        max_shift_bps=1200,
    )

    assert recommendation.status == CreativeOptimizationStatus.READY
    assert recommendation.allocations == ((JOB_A, 5200), (JOB_B, 3000), (JOB_C, 1800))


def test_optimizer_rejects_invalid_policy_values() -> None:
    snapshot = _snapshot(
        (
            _outcome("variant-a", JOB_A, leads=100, bookings=50),
            _outcome("variant-b", JOB_B, leads=100, bookings=5),
        )
    )

    for kwargs in (
        {"min_leads_per_arm": 0},
        {"exploration_floor_bps": 0},
        {"exploration_floor_bps": 5000},
        {"max_shift_bps": 0},
        {"max_shift_bps": 5001},
    ):
        try:
            recommend_creative_growth_allocation(snapshot, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {kwargs}")
