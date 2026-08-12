from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from uuid import uuid4

from clientplatform.application import creative_growth_analytics as analytics
from clientplatform.application.promotion_attribution import PromotionAttribution
from clientplatform.domain.creative_growth import (
    CreativeAttributionScope,
    CreativeTrafficPlan,
    CreativeTrialArm,
    CreativeTrialStatus,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext


def _actor() -> TenantContext:
    return TenantContext(
        business_id=str(uuid4()),
        user_id=1,
        membership_id=str(uuid4()),
        role=PlatformRole.OWNER,
    )


def _install(monkeypatch, *, plan: CreativeTrafficPlan, attribution: PromotionAttribution) -> None:
    @contextmanager
    def ro():
        yield object()

    class Repo:
        def __init__(self, _conn):
            pass

        def get(self, *, actor, trial_id):
            assert actor.business_id == plan.business_id
            assert trial_id == plan.trial_id
            return plan

    monkeypatch.setattr(analytics, "get_db_ro", ro)
    monkeypatch.setattr(analytics, "CreativeGrowthRepository", Repo)
    monkeypatch.setattr(
        analytics,
        "load_promotion_attribution",
        lambda *args, **kwargs: attribution,
    )


def test_shared_campaign_outcomes_are_not_duplicated_across_variants(monkeypatch) -> None:
    actor = _actor()
    campaign_id = str(uuid4())
    plan = CreativeTrafficPlan(
        trial_id=str(uuid4()),
        business_id=actor.business_id,
        status=CreativeTrialStatus.RUNNING,
        revision=1,
        arms=(
            CreativeTrialArm("variant-a", str(uuid4()), 5000, campaign_id),
            CreativeTrialArm("variant-b", str(uuid4()), 5000, campaign_id),
        ),
    ).normalized()
    _install(
        monkeypatch,
        plan=plan,
        attribution=PromotionAttribution(
            leads={campaign_id: frozenset({"customer-a-one", "customer-a-two"})},
            bookings={campaign_id: frozenset({"customer-a-two"})},
            won={campaign_id: frozenset({"customer-a-two"})},
        ),
    )

    snapshot = analytics.get_creative_growth_outcomes(
        actor=actor,
        trial_id=plan.trial_id,
        days=7,
        now=date(2026, 8, 12),
    )

    assert snapshot.variant_level_ready is False
    assert {item.attribution_scope for item in snapshot.variants} == {
        CreativeAttributionScope.SHARED_CAMPAIGN
    }
    assert all((item.leads, item.bookings, item.won) == (0, 0, 0) for item in snapshot.variants)
    assert len(snapshot.shared_campaigns) == 1
    shared = snapshot.shared_campaigns[0]
    assert shared.variant_ids == ("variant-a", "variant-b")
    assert (shared.leads, shared.bookings, shared.won) == (2, 1, 1)


def test_unique_campaigns_receive_variant_level_downstream_outcomes(monkeypatch) -> None:
    actor = _actor()
    campaign_a = str(uuid4())
    campaign_b = str(uuid4())
    plan = CreativeTrafficPlan(
        trial_id=str(uuid4()),
        business_id=actor.business_id,
        status=CreativeTrialStatus.RUNNING,
        revision=1,
        arms=(
            CreativeTrialArm("variant-a", str(uuid4()), 5000, campaign_a),
            CreativeTrialArm("variant-b", str(uuid4()), 5000, campaign_b),
        ),
    ).normalized()
    _install(
        monkeypatch,
        plan=plan,
        attribution=PromotionAttribution(
            leads={
                campaign_a: frozenset({"customer-a-one", "customer-a-two"}),
                campaign_b: frozenset({"customer-b-one"}),
            },
            bookings={campaign_a: frozenset({"customer-a-two"})},
            won={},
        ),
    )

    snapshot = analytics.get_creative_growth_outcomes(
        actor=actor,
        trial_id=plan.trial_id,
        now=date(2026, 8, 12),
    )

    assert snapshot.variant_level_ready is True
    assert snapshot.shared_campaigns == ()
    first, second = snapshot.variants
    assert first.attribution_scope == CreativeAttributionScope.VARIANT
    assert (first.leads, first.bookings, first.won) == (2, 1, 0)
    assert (second.leads, second.bookings, second.won) == (1, 0, 0)
