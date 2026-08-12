from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from clientplatform.application import creative_winner as app
from clientplatform.application.creative_growth_analytics import CreativeGrowthOutcomeSnapshot
from clientplatform.application.creative_growth_optimizer import (
    CreativeOptimizationMetric,
    CreativeOptimizationStatus,
)
from clientplatform.domain.creative_growth import (
    CreativeAttributionScope,
    CreativeTrafficPlan,
    CreativeTrialArm,
    CreativeTrialStatus,
    CreativeVariantOutcome,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.infrastructure.creative_growth_optimization_repository import (
    StaleCreativeOptimizationError,
)


def _fixture() -> tuple[TenantContext, CreativeTrafficPlan, CreativeGrowthOutcomeSnapshot]:
    actor = TenantContext(
        business_id=str(uuid4()),
        user_id=701,
        membership_id=str(uuid4()),
        role=PlatformRole.OWNER,
    )
    arms = tuple(
        CreativeTrialArm(
            variant_id=f"variant-{index}",
            publication_job_id=str(uuid4()),
            allocation_bps=5000,
            promotion_campaign_id=str(uuid4()),
            promotion_source_token=f"variantSource{index}XYZ",
        )
        for index in range(2)
    )
    plan = CreativeTrafficPlan(
        trial_id=str(uuid4()),
        business_id=actor.business_id,
        status=CreativeTrialStatus.RUNNING,
        revision=7,
        arms=arms,
    ).normalized()
    outcomes = (
        CreativeVariantOutcome(
            variant_id=arms[0].variant_id,
            publication_job_id=arms[0].publication_job_id,
            promotion_campaign_id=arms[0].promotion_campaign_id,
            attribution_scope=CreativeAttributionScope.VARIANT,
            leads=100,
            bookings=35,
            won=20,
        ),
        CreativeVariantOutcome(
            variant_id=arms[1].variant_id,
            publication_job_id=arms[1].publication_job_id,
            promotion_campaign_id=arms[1].promotion_campaign_id,
            attribution_scope=CreativeAttributionScope.VARIANT,
            leads=100,
            bookings=5,
            won=1,
        ),
    )
    snapshot = CreativeGrowthOutcomeSnapshot(
        plan=plan,
        date_from="2026-07-14",
        date_to="2026-08-12",
        variants=outcomes,
        shared_campaigns=(),
    )
    return actor, plan, snapshot


def test_preview_fingerprint_is_stable_and_evidence_bound(monkeypatch) -> None:
    actor, plan, snapshot = _fixture()
    monkeypatch.setattr(app, "get_creative_growth_outcomes", lambda **_kwargs: snapshot)

    first = app.preview_creative_winner(actor=actor, trial_id=plan.trial_id)
    second = app.preview_creative_winner(actor=actor, trial_id=plan.trial_id)

    assert first == second
    assert first.recommendation.status == CreativeOptimizationStatus.READY
    assert len(first.fingerprint) == 16

    changed = replace(
        snapshot,
        variants=(replace(snapshot.variants[0], bookings=36), snapshot.variants[1]),
    )
    monkeypatch.setattr(app, "get_creative_growth_outcomes", lambda **_kwargs: changed)
    newer = app.preview_creative_winner(actor=actor, trial_id=plan.trial_id)
    assert newer.fingerprint != first.fingerprint


def test_fingerprint_binds_selected_metric_and_policy(monkeypatch) -> None:
    actor, plan, snapshot = _fixture()
    monkeypatch.setattr(app, "get_creative_growth_outcomes", lambda **_kwargs: snapshot)

    bookings = app.preview_creative_winner(
        actor=actor,
        trial_id=plan.trial_id,
        metric=CreativeOptimizationMetric.BOOKINGS,
    )
    won = app.preview_creative_winner(
        actor=actor,
        trial_id=plan.trial_id,
        metric=CreativeOptimizationMetric.WON,
    )
    stricter = app.preview_creative_winner(
        actor=actor,
        trial_id=plan.trial_id,
        metric=CreativeOptimizationMetric.BOOKINGS,
        min_leads_per_arm=40,
    )

    assert bookings.fingerprint != won.fingerprint
    assert bookings.fingerprint != stricter.fingerprint


def test_confirmation_requires_explicit_true_before_recompute(monkeypatch) -> None:
    actor, plan, _snapshot = _fixture()
    touched = {"read": False}

    def fail_if_read(**_kwargs):
        touched["read"] = True
        raise AssertionError("evidence should not be read without confirmation")

    monkeypatch.setattr(app, "get_creative_growth_outcomes", fail_if_read)
    with pytest.raises(app.CreativeWinnerApplyError, match="explicit confirmation"):
        app.apply_creative_winner(
            actor=actor,
            trial_id=plan.trial_id,
            expected_revision=plan.revision,
            expected_fingerprint="deadbeefdeadbeef",
            confirmed=False,
        )
    assert touched["read"] is False


def test_confirmation_refuses_changed_evidence_before_write(monkeypatch) -> None:
    actor, plan, snapshot = _fixture()
    current = {"snapshot": snapshot}
    monkeypatch.setattr(
        app,
        "get_creative_growth_outcomes",
        lambda **_kwargs: current["snapshot"],
    )
    preview = app.preview_creative_winner(actor=actor, trial_id=plan.trial_id)
    current["snapshot"] = replace(
        snapshot,
        variants=(replace(snapshot.variants[0], bookings=36), snapshot.variants[1]),
    )
    writes = {"called": False}

    def fail_if_written(**_kwargs):
        writes["called"] = True
        raise AssertionError("stale evidence must be rejected before CAS")

    monkeypatch.setattr(app, "apply_creative_growth_recommendation", fail_if_written)
    with pytest.raises(app.CreativeWinnerApplyError, match="evidence changed"):
        app.apply_creative_winner(
            actor=actor,
            trial_id=plan.trial_id,
            expected_revision=preview.recommendation.trial_revision,
            expected_fingerprint=preview.fingerprint,
            confirmed=True,
        )
    assert writes["called"] is False


def test_confirmation_refuses_wrong_revision_before_write(monkeypatch) -> None:
    actor, plan, snapshot = _fixture()
    monkeypatch.setattr(app, "get_creative_growth_outcomes", lambda **_kwargs: snapshot)
    preview = app.preview_creative_winner(actor=actor, trial_id=plan.trial_id)
    writes = {"called": False}

    def fail_if_written(**_kwargs):
        writes["called"] = True
        raise AssertionError("stale revision must be rejected before CAS")

    monkeypatch.setattr(app, "apply_creative_growth_recommendation", fail_if_written)
    with pytest.raises(app.CreativeWinnerApplyError, match="stale"):
        app.apply_creative_winner(
            actor=actor,
            trial_id=plan.trial_id,
            expected_revision=plan.revision - 1,
            expected_fingerprint=preview.fingerprint,
            confirmed=True,
        )
    assert writes["called"] is False


def test_fresh_confirmation_delegates_to_canonical_cas(monkeypatch) -> None:
    actor, plan, snapshot = _fixture()
    monkeypatch.setattr(app, "get_creative_growth_outcomes", lambda **_kwargs: snapshot)
    preview = app.preview_creative_winner(actor=actor, trial_id=plan.trial_id)
    captured: dict[str, object] = {}
    proposed = {
        item.publication_job_id: item.proposed_allocation_bps
        for item in preview.recommendation.evidence
    }
    updated = replace(
        plan,
        revision=plan.revision + 1,
        arms=tuple(
            replace(arm, allocation_bps=proposed[arm.publication_job_id])
            for arm in plan.arms
        ),
    ).normalized()

    def apply(**kwargs):
        captured.update(kwargs)
        return updated

    monkeypatch.setattr(app, "apply_creative_growth_recommendation", apply)
    result = app.apply_creative_winner(
        actor=actor,
        trial_id=plan.trial_id,
        expected_revision=preview.recommendation.trial_revision,
        expected_fingerprint=preview.fingerprint,
        confirmed=True,
    )

    assert captured["actor"] == actor
    assert captured["recommendation"] == preview.recommendation
    assert captured["confirmed"] is True
    assert result.preview == preview
    assert result.updated_plan == updated


def test_canonical_stale_error_is_mapped(monkeypatch) -> None:
    actor, plan, snapshot = _fixture()
    monkeypatch.setattr(app, "get_creative_growth_outcomes", lambda **_kwargs: snapshot)
    preview = app.preview_creative_winner(actor=actor, trial_id=plan.trial_id)

    def stale(**_kwargs):
        raise StaleCreativeOptimizationError("changed during CAS")

    monkeypatch.setattr(app, "apply_creative_growth_recommendation", stale)
    with pytest.raises(app.CreativeWinnerApplyError, match="stale"):
        app.apply_creative_winner(
            actor=actor,
            trial_id=plan.trial_id,
            expected_revision=preview.recommendation.trial_revision,
            expected_fingerprint=preview.fingerprint,
            confirmed=True,
        )
