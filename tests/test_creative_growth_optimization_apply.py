from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from clientplatform.application.creative_growth_optimization import (
    apply_creative_growth_recommendation,
)
from clientplatform.application.creative_growth_optimizer import (
    CreativeOptimizationArmEvidence,
    CreativeOptimizationMetric,
    CreativeOptimizationRecommendation,
    CreativeOptimizationStatus,
)
from clientplatform.domain.creative_growth import CreativeTrafficPlan, CreativeTrialStatus
from clientplatform.domain.creative_variant_bindings import CreativeVariantBindingStatus
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied
from clientplatform.infrastructure.creative_growth_optimization_repository import (
    CreativeGrowthOptimizationRepository,
    StaleCreativeOptimizationError,
)
from clientplatform.infrastructure.creative_growth_repository import CreativeGrowthRepository
from clientplatform.infrastructure.creative_variant_binding_repository import (
    CreativeVariantBindingRepository,
)
from services.db.schema import clientplatform_creative_experiments, clientplatform_creative_growth


def _running_trial() -> tuple[
    sqlite3.Connection,
    TenantContext,
    TenantContext,
    tuple[str, str],
    CreativeTrafficPlan,
]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE businesses(id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE business_members(id TEXT NOT NULL, business_id TEXT NOT NULL, "
        "user_id INTEGER NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL, "
        "PRIMARY KEY(id, business_id))"
    )
    conn.execute(
        "CREATE TABLE promotion_campaigns(id TEXT NOT NULL, business_id TEXT NOT NULL, "
        "source_token TEXT NOT NULL UNIQUE, PRIMARY KEY(id, business_id))"
    )
    conn.execute(
        "CREATE TABLE promotion_source_aliases(source_token TEXT PRIMARY KEY, "
        "business_id TEXT NOT NULL, campaign_id TEXT NOT NULL, source_kind TEXT NOT NULL, "
        "source_key TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, UNIQUE(business_id, campaign_id, source_kind, source_key))"
    )
    conn.execute(
        "CREATE TABLE ad_publication_jobs(id TEXT NOT NULL, business_id TEXT NOT NULL, "
        "status TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, "
        "source_url TEXT NOT NULL, external_ad_id TEXT, promotion_campaign_id TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, PRIMARY KEY(id, business_id))"
    )
    conn.execute(
        "CREATE TABLE ad_publication_assets(publication_job_id TEXT NOT NULL, "
        "business_id TEXT NOT NULL, source TEXT NOT NULL, "
        "PRIMARY KEY(publication_job_id, business_id))"
    )
    clientplatform_creative_experiments.ensure(conn)
    clientplatform_creative_growth.ensure(conn)

    business_id = str(uuid4())
    owner_member = str(uuid4())
    analyst_member = str(uuid4())
    conn.execute("INSERT INTO businesses VALUES(?, 'active')", (business_id,))
    conn.execute(
        "INSERT INTO business_members VALUES(?, ?, 1, 'owner', 'active')",
        (owner_member, business_id),
    )
    conn.execute(
        "INSERT INTO business_members VALUES(?, ?, 2, 'analyst', 'active')",
        (analyst_member, business_id),
    )
    owner = TenantContext(
        business_id=business_id,
        user_id=1,
        membership_id=owner_member,
        role=PlatformRole.OWNER,
    )
    analyst = TenantContext(
        business_id=business_id,
        user_id=2,
        membership_id=analyst_member,
        role=PlatformRole.ANALYST,
    )

    jobs = (str(uuid4()), str(uuid4()))
    bindings = CreativeVariantBindingRepository(conn)
    for index, job_id in enumerate(jobs):
        campaign_id = str(uuid4())
        source_token = f"campaignSource{index}XYZ"
        conn.execute(
            "INSERT INTO promotion_campaigns VALUES(?, ?, ?)",
            (campaign_id, business_id, source_token),
        )
        conn.execute(
            "INSERT INTO ad_publication_jobs VALUES(?, ?, 'draft', ?, ?, ?, '', ?, ?)",
            (
                job_id,
                business_id,
                f"Title {index}",
                f"Body {index}",
                f"https://t.me/clientplatformbot?start=cpa_{source_token}",
                campaign_id,
                "2026-08-12T10:00:00+00:00",
            ),
        )
        binding = bindings.select(
            actor=owner,
            publication_job_id=job_id,
            experiment_id=f"experiment-{index}",
            variant_id=f"variant-{index}",
            angle_id="human_trust",
            country_code="RU",
        )
        bindings.remember_progress(
            actor=owner,
            publication_job_id=job_id,
            variant_id=binding.variant_id,
            source_job_id=f"source-{index}",
            render_pack_id=f"render-{index}",
            status=CreativeVariantBindingStatus.ATTACHED,
        )
        conn.execute(
            "INSERT INTO ad_publication_assets VALUES(?, ?, 'generated')",
            (job_id, business_id),
        )

    growth = CreativeGrowthRepository(conn)
    draft = growth.create(
        actor=owner,
        name="Optimizer apply",
        allocations=((jobs[0], 5000), (jobs[1], 5000)),
    )
    running = growth.set_status(
        actor=owner,
        trial_id=draft.trial_id,
        status=CreativeTrialStatus.RUNNING,
    )
    return conn, owner, analyst, jobs, running


def _recommendation(
    plan: CreativeTrafficPlan,
    jobs: tuple[str, str],
) -> CreativeOptimizationRecommendation:
    return CreativeOptimizationRecommendation(
        trial_id=plan.trial_id,
        trial_revision=plan.revision,
        metric=CreativeOptimizationMetric.BOOKINGS,
        status=CreativeOptimizationStatus.READY,
        reason="clear winner",
        winner_variant_id=plan.arms[0].variant_id,
        evidence=(
            CreativeOptimizationArmEvidence(
                variant_id=plan.arms[0].variant_id,
                publication_job_id=jobs[0],
                leads=100,
                successes=45,
                rate=0.45,
                confidence_low=0.35,
                confidence_high=0.55,
                current_allocation_bps=5000,
                proposed_allocation_bps=6000,
            ),
            CreativeOptimizationArmEvidence(
                variant_id=plan.arms[1].variant_id,
                publication_job_id=jobs[1],
                leads=100,
                successes=10,
                rate=0.10,
                confidence_low=0.05,
                confidence_high=0.17,
                current_allocation_bps=5000,
                proposed_allocation_bps=4000,
            ),
        ),
    )


def _cas_rows(
    recommendation: CreativeOptimizationRecommendation,
) -> tuple[tuple[str, str, int, int], ...]:
    return tuple(
        (
            item.variant_id,
            item.publication_job_id,
            item.current_allocation_bps,
            item.proposed_allocation_bps,
        )
        for item in recommendation.evidence
    )


def test_apply_uses_revision_cas_and_preserves_total_allocation() -> None:
    conn, owner, _analyst, jobs, running = _running_trial()
    recommendation = _recommendation(running, jobs)
    repository = CreativeGrowthOptimizationRepository(conn)

    changed = repository.apply(
        actor=owner,
        trial_id=recommendation.trial_id,
        expected_revision=recommendation.trial_revision,
        allocations=_cas_rows(recommendation),
    )

    assert changed.revision == running.revision + 1
    assert [arm.allocation_bps for arm in changed.arms] == [6000, 4000]
    assert sum(arm.allocation_bps for arm in changed.arms) == 10_000

    with pytest.raises(StaleCreativeOptimizationError, match="changed after recommendation"):
        repository.apply(
            actor=owner,
            trial_id=recommendation.trial_id,
            expected_revision=recommendation.trial_revision,
            allocations=_cas_rows(recommendation),
        )
    current = CreativeGrowthRepository(conn).get(actor=owner, trial_id=running.trial_id)
    assert [arm.allocation_bps for arm in current.arms] == [6000, 4000]


def test_apply_refuses_non_manager() -> None:
    conn, _owner, analyst, jobs, running = _running_trial()
    recommendation = _recommendation(running, jobs)

    with pytest.raises(TenantPermissionDenied):
        CreativeGrowthOptimizationRepository(conn).apply(
            actor=analyst,
            trial_id=recommendation.trial_id,
            expected_revision=recommendation.trial_revision,
            allocations=_cas_rows(recommendation),
        )


def test_application_service_requires_explicit_confirmation_before_db_access() -> None:
    _conn, owner, _analyst, jobs, running = _running_trial()
    recommendation = _recommendation(running, jobs)

    with pytest.raises(ValueError, match="explicit confirmation"):
        apply_creative_growth_recommendation(
            actor=owner,
            recommendation=recommendation,
            confirmed=False,
        )
