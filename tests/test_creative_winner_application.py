from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest

from clientplatform.application import creative_winner as app
from clientplatform.application.creative_growth_analytics import CreativeGrowthOutcomeSnapshot
from clientplatform.domain.creative_growth import (
    CreativeAttributionScope,
    CreativeTrafficPlan,
    CreativeTrialArm,
    CreativeTrialStatus,
    CreativeVariantOutcome,
)
from clientplatform.domain.creative_winner_policy import CreativeWinnerDecision
from clientplatform.domain.tenancy import PlatformRole, TenantContext


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
            bookings=30,
            won=0,
        ),
        CreativeVariantOutcome(
            variant_id=arms[1].variant_id,
            publication_job_id=arms[1].publication_job_id,
            promotion_campaign_id=arms[1].promotion_campaign_id,
            attribution_scope=CreativeAttributionScope.VARIANT,
            leads=100,
            bookings=5,
            won=0,
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
    monkeypatch.setattr(app, "get_creative_growth_outcomes", lambda **kwargs: snapshot)

    first = app.preview_creative_winner(
        actor=actor,
        trial_id=plan.trial_id,
        now=date(2026, 8, 12),
    )
    second = app.preview_creative_winner(
        actor=actor,
        trial_id=plan.trial_id,
        now=date(2026, 8, 12),
    )
    assert first == second
    assert first.recommendation.decision == CreativeWinnerDecision.SHIFT
    assert len(first.fingerprint) == 16

    changed = replace(
        snapshot,
        variants=(
            replace(snapshot.variants[0], bookings=31),
            snapshot.variants[1],
        ),
    )
    monkeypatch.setattr(app, "get_creative_growth_outcomes", lambda **kwargs: changed)
    newer = app.preview_creative_winner(
        actor=actor,
        trial_id=plan.trial_id,
        now=date(2026, 8, 12),
    )
    assert newer.fingerprint != first.fingerprint


def test_confirmation_refuses_changed_evidence_before_write(monkeypatch) -> None:
    actor, plan, snapshot = _fixture()
    current = {"snapshot": snapshot}
    monkeypatch.setattr(
        app,
        "get_creative_growth_outcomes",
        lambda **kwargs: current["snapshot"],
    )
    preview = app.preview_creative_winner(actor=actor, trial_id=plan.trial_id)
    current["snapshot"] = replace(
        snapshot,
        variants=(replace(snapshot.variants[0], bookings=31), snapshot.variants[1]),
    )

    writes = {"opened": False}

    @contextmanager
    def fail_if_opened():
        writes["opened"] = True
        yield object()

    monkeypatch.setattr(app, "get_db", fail_if_opened)
    with pytest.raises(app.CreativeWinnerApplyError, match="evidence changed"):
        app.apply_creative_winner(
            actor=actor,
            trial_id=plan.trial_id,
            expected_revision=preview.recommendation.expected_revision,
            expected_fingerprint=preview.fingerprint,
        )
    assert writes["opened"] is False


def test_confirmation_passes_revision_to_repository_cas(monkeypatch) -> None:
    actor, plan, snapshot = _fixture()
    monkeypatch.setattr(app, "get_creative_growth_outcomes", lambda **kwargs: snapshot)
    preview = app.preview_creative_winner(actor=actor, trial_id=plan.trial_id)
    captured: dict[str, object] = {}

    @contextmanager
    def write_db():
        yield object()

    class Tenancy:
        def __init__(self, _conn):
            pass

        def resolve_context(self, *, user_id, business_id):
            assert user_id == actor.user_id
            assert business_id == actor.business_id
            return actor

    class Repository:
        def __init__(self, _conn):
            pass

        def replace_allocations(
            self,
            *,
            actor,
            trial_id,
            allocations,
            expected_revision,
        ):
            captured.update(
                actor=actor,
                trial_id=trial_id,
                allocations=tuple(allocations),
                expected_revision=expected_revision,
            )
            allocation_map = dict(captured["allocations"])
            return CreativeTrafficPlan(
                trial_id=plan.trial_id,
                business_id=plan.business_id,
                status=plan.status,
                revision=plan.revision + 1,
                arms=tuple(
                    replace(arm, allocation_bps=allocation_map[arm.publication_job_id])
                    for arm in plan.arms
                ),
            ).normalized()

    monkeypatch.setattr(app, "get_db", write_db)
    monkeypatch.setattr(app, "TenancyRepository", Tenancy)
    monkeypatch.setattr(app, "CreativeGrowthRepository", Repository)
    result = app.apply_creative_winner(
        actor=actor,
        trial_id=plan.trial_id,
        expected_revision=preview.recommendation.expected_revision,
        expected_fingerprint=preview.fingerprint,
    )

    assert captured["expected_revision"] == plan.revision
    assert captured["allocations"] == preview.recommendation.recommended_allocations
    assert result.updated_plan.revision == plan.revision + 1


def test_confirmation_refuses_wrong_revision_even_with_valid_fingerprint(monkeypatch) -> None:
    actor, plan, snapshot = _fixture()
    monkeypatch.setattr(app, "get_creative_growth_outcomes", lambda **kwargs: snapshot)
    preview = app.preview_creative_winner(actor=actor, trial_id=plan.trial_id)

    with pytest.raises(app.CreativeWinnerApplyError, match="stale"):
        app.apply_creative_winner(
            actor=actor,
            trial_id=plan.trial_id,
            expected_revision=plan.revision - 1,
            expected_fingerprint=preview.fingerprint,
        )
