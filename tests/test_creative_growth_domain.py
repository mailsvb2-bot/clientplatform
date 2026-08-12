from __future__ import annotations

from uuid import uuid4

import pytest

from clientplatform.domain.creative_growth import (
    CreativeTrafficPlan,
    CreativeTrialArm,
    CreativeTrialStatus,
)


def _arm(*, allocation: int) -> CreativeTrialArm:
    return CreativeTrialArm(
        variant_id=f"variant-{uuid4()}",
        publication_job_id=str(uuid4()),
        allocation_bps=allocation,
        promotion_campaign_id=str(uuid4()),
    )


def test_traffic_plan_requires_exact_full_allocation() -> None:
    with pytest.raises(ValueError, match="total 10000"):
        CreativeTrafficPlan(
            trial_id=str(uuid4()),
            business_id=str(uuid4()),
            status=CreativeTrialStatus.DRAFT,
            revision=1,
            arms=(_arm(allocation=4000), _arm(allocation=5000)),
        ).normalized()


def test_traffic_plan_rejects_duplicate_variant_or_job() -> None:
    job_id = str(uuid4())
    variant_id = "variant-same"
    base = CreativeTrialArm(
        variant_id=variant_id,
        publication_job_id=job_id,
        allocation_bps=5000,
    )
    with pytest.raises(ValueError, match="variants must be unique"):
        CreativeTrafficPlan(
            trial_id=str(uuid4()),
            business_id=str(uuid4()),
            status=CreativeTrialStatus.DRAFT,
            revision=1,
            arms=(base, CreativeTrialArm(variant_id=variant_id, publication_job_id=str(uuid4()), allocation_bps=5000)),
        ).normalized()
    with pytest.raises(ValueError, match="publication jobs must be unique"):
        CreativeTrafficPlan(
            trial_id=str(uuid4()),
            business_id=str(uuid4()),
            status=CreativeTrialStatus.DRAFT,
            revision=1,
            arms=(base, CreativeTrialArm(variant_id="variant-other", publication_job_id=job_id, allocation_bps=5000)),
        ).normalized()


def test_running_plan_assignment_is_stable_for_same_subject_and_revision() -> None:
    plan = CreativeTrafficPlan(
        trial_id=str(uuid4()),
        business_id=str(uuid4()),
        status=CreativeTrialStatus.RUNNING,
        revision=7,
        arms=(_arm(allocation=5000), _arm(allocation=5000)),
    ).normalized()

    first = plan.assign("customer:123")
    assert plan.assign("customer:123") == first
    assert first in plan.arms


def test_assignment_refuses_non_running_trial() -> None:
    plan = CreativeTrafficPlan(
        trial_id=str(uuid4()),
        business_id=str(uuid4()),
        status=CreativeTrialStatus.PAUSED,
        revision=1,
        arms=(_arm(allocation=5000), _arm(allocation=5000)),
    ).normalized()

    with pytest.raises(ValueError, match="not running"):
        plan.assign("customer:123")


def test_revision_is_part_of_deterministic_assignment_identity() -> None:
    arms = (_arm(allocation=5000), _arm(allocation=5000))
    base = CreativeTrafficPlan(
        trial_id=str(uuid4()),
        business_id=str(uuid4()),
        status=CreativeTrialStatus.RUNNING,
        revision=1,
        arms=arms,
    ).normalized()
    changed = CreativeTrafficPlan(
        trial_id=base.trial_id,
        business_id=base.business_id,
        status=base.status,
        revision=2,
        arms=arms,
    ).normalized()

    # Revision participates in the hash input even when a particular subject
    # happens to land in the same allocation bucket after rebalancing.
    subjects = [f"visitor:{index}" for index in range(200)]
    assert any(base.assign(subject) != changed.assign(subject) for subject in subjects)
