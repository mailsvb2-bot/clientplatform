from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from clientplatform.domain.creative_growth import CreativeTrialStatus
from clientplatform.domain.creative_variant_bindings import CreativeVariantBindingStatus
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied
from clientplatform.infrastructure.creative_growth_repository import CreativeGrowthRepository
from clientplatform.infrastructure.creative_variant_binding_repository import (
    CreativeVariantBindingRepository,
)
from services.db.schema import clientplatform_creative_experiments, clientplatform_creative_growth


def _db() -> tuple[sqlite3.Connection, TenantContext, TenantContext, tuple[str, str]]:
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
    binding_repo = CreativeVariantBindingRepository(conn)
    for index, job_id in enumerate(jobs):
        campaign_id = str(uuid4())
        base_source = f"campaignSrc{index}XYZ"
        conn.execute(
            "INSERT INTO promotion_campaigns VALUES(?, ?, ?)",
            (campaign_id, business_id, base_source),
        )
        conn.execute(
            "INSERT INTO ad_publication_jobs VALUES(?, ?, 'draft', ?, ?, ?, '', ?, ?)",
            (
                job_id,
                business_id,
                f"Title {index}",
                f"Body {index}",
                f"https://t.me/clientplatformbot?start=cpa_{base_source}",
                campaign_id,
                "2026-08-12T10:00:00+00:00",
            ),
        )
        selected = binding_repo.select(
            actor=owner,
            publication_job_id=job_id,
            experiment_id=f"experiment-{index}",
            variant_id=f"variant-{index}",
            angle_id="human_trust",
            country_code="RU",
        )
        binding_repo.remember_progress(
            actor=owner,
            publication_job_id=job_id,
            variant_id=selected.variant_id,
            source_job_id=f"source-{index}",
            render_pack_id=f"render-{index}",
            status=CreativeVariantBindingStatus.ATTACHED,
        )
        conn.execute(
            "INSERT INTO ad_publication_assets VALUES(?, ?, 'generated')",
            (job_id, business_id),
        )
    return conn, owner, analyst, jobs


def test_creative_growth_trial_persists_allocation_assignment_and_source_urls() -> None:
    conn, owner, analyst, jobs = _db()
    repo = CreativeGrowthRepository(conn)

    plan = repo.create(
        actor=owner,
        name="Trust vs process",
        allocations=((jobs[0], 5000), (jobs[1], 5000)),
    )
    assert plan.status == CreativeTrialStatus.DRAFT
    assert [arm.allocation_bps for arm in plan.arms] == [5000, 5000]
    assert all(not arm.promotion_source_token for arm in plan.arms)
    assert repo.get(actor=analyst, trial_id=plan.trial_id) == plan

    running = repo.set_status(
        actor=owner,
        trial_id=plan.trial_id,
        status=CreativeTrialStatus.RUNNING,
    )
    assert running.status == CreativeTrialStatus.RUNNING
    tokens = [arm.promotion_source_token for arm in running.arms]
    assert all(tokens)
    assert len(set(tokens)) == 2
    for arm in running.arms:
        row = conn.execute(
            "SELECT source_url FROM ad_publication_jobs WHERE id=?",
            (arm.publication_job_id,),
        ).fetchone()
        assert row is not None
        assert f"start=cpa_{arm.promotion_source_token}" in str(row[0])
    assert conn.execute("SELECT COUNT(*) FROM promotion_source_aliases").fetchone()[0] == 2

    first = repo.assign(actor=analyst, trial_id=running.trial_id, subject_key="visitor:42")
    assert repo.assign(actor=analyst, trial_id=running.trial_id, subject_key="visitor:42") == first

    paused = repo.set_status(
        actor=owner,
        trial_id=running.trial_id,
        status=CreativeTrialStatus.PAUSED,
    )
    resumed = repo.set_status(
        actor=owner,
        trial_id=paused.trial_id,
        status=CreativeTrialStatus.RUNNING,
    )
    assert [arm.promotion_source_token for arm in resumed.arms] == tokens
    assert conn.execute("SELECT COUNT(*) FROM promotion_source_aliases").fetchone()[0] == 2


def test_reallocation_requires_same_arms_and_full_basis_points() -> None:
    conn, owner, _analyst, jobs = _db()
    repo = CreativeGrowthRepository(conn)
    plan = repo.create(
        actor=owner,
        name="Allocation",
        allocations=((jobs[0], 5000), (jobs[1], 5000)),
    )

    changed = repo.replace_allocations(
        actor=owner,
        trial_id=plan.trial_id,
        allocations=((jobs[0], 7000), (jobs[1], 3000)),
    )
    assert changed.revision == plan.revision + 1
    assert [arm.allocation_bps for arm in changed.arms] == [7000, 3000]

    with pytest.raises(ValueError, match="total 10000"):
        repo.replace_allocations(
            actor=owner,
            trial_id=plan.trial_id,
            allocations=((jobs[0], 7000), (jobs[1], 2000)),
        )


def test_analyst_can_read_but_cannot_manage_trial() -> None:
    conn, owner, analyst, jobs = _db()
    repo = CreativeGrowthRepository(conn)
    plan = repo.create(
        actor=owner,
        name="Permissions",
        allocations=((jobs[0], 5000), (jobs[1], 5000)),
    )

    assert repo.get(actor=analyst, trial_id=plan.trial_id).trial_id == plan.trial_id
    with pytest.raises(TenantPermissionDenied):
        repo.set_status(
            actor=analyst,
            trial_id=plan.trial_id,
            status=CreativeTrialStatus.RUNNING,
        )


def test_running_trial_requires_canonical_generated_assets() -> None:
    conn, owner, _analyst, jobs = _db()
    repo = CreativeGrowthRepository(conn)
    plan = repo.create(
        actor=owner,
        name="Media safety",
        allocations=((jobs[0], 5000), (jobs[1], 5000)),
    )
    conn.execute(
        "UPDATE ad_publication_assets SET source='upload' WHERE publication_job_id=?",
        (jobs[1],),
    )

    with pytest.raises(ValueError, match="attached generated variants"):
        repo.set_status(
            actor=owner,
            trial_id=plan.trial_id,
            status=CreativeTrialStatus.RUNNING,
        )


def test_first_activation_refuses_queued_job_and_rolls_back_aliases() -> None:
    conn, owner, _analyst, jobs = _db()
    repo = CreativeGrowthRepository(conn)
    plan = repo.create(
        actor=owner,
        name="Race safety",
        allocations=((jobs[0], 5000), (jobs[1], 5000)),
    )
    original_urls = {
        row[0]: row[1]
        for row in conn.execute("SELECT id, source_url FROM ad_publication_jobs").fetchall()
    }
    conn.execute("UPDATE ad_publication_jobs SET status='queued' WHERE id=?", (jobs[1],))

    with pytest.raises(ValueError, match="before advertising publication is queued"):
        repo.set_status(
            actor=owner,
            trial_id=plan.trial_id,
            status=CreativeTrialStatus.RUNNING,
        )

    assert repo.get(actor=owner, trial_id=plan.trial_id).status == CreativeTrialStatus.DRAFT
    assert all(not arm.promotion_source_token for arm in repo.get(actor=owner, trial_id=plan.trial_id).arms)
    assert conn.execute("SELECT COUNT(*) FROM promotion_source_aliases").fetchone()[0] == 0
    current_urls = {
        row[0]: row[1]
        for row in conn.execute("SELECT id, source_url FROM ad_publication_jobs").fetchall()
    }
    assert current_urls == original_urls


def test_creative_growth_bounded_page_and_reference_resolution() -> None:
    conn, owner, analyst, jobs = _db()
    repo = CreativeGrowthRepository(conn)
    created = [
        repo.create(
            actor=owner,
            name=f"Bounded {index}",
            allocations=((jobs[0], 5000), (jobs[1], 5000)),
        )
        for index in range(6)
    ]

    first, first_more = repo.list_page(actor=analyst, limit=4, offset=0)
    second, second_more = repo.list_page(actor=analyst, limit=4, offset=4)
    assert len(first) == 4
    assert first_more is True
    assert len(second) == 2
    assert second_more is False

    target = created[0].trial_id
    assert repo.resolve_reference(actor=analyst, reference=target[:8]) == target

    ambiguous_id = target[:8] + str(uuid4())[8:]
    conn.execute(
        "INSERT INTO creative_growth_trials("
        "id, business_id, name, status, revision, created_by_member_id, created_at, updated_at"
        ") VALUES(?, ?, 'Ambiguous', 'draft', 1, ?, ?, ?)",
        (
            ambiguous_id,
            owner.business_id,
            owner.membership_id,
            "2026-09-05T00:00:00+00:00",
            "2026-09-05T00:00:00+00:00",
        ),
    )
    with pytest.raises(ValueError, match="stale or ambiguous"):
        repo.resolve_reference(actor=analyst, reference=target[:8])


@pytest.mark.parametrize(
    ("limit", "offset"),
    [(0, 0), (51, 0), (True, 0), (4, -1), (4, 10001), (4, True)],
)
def test_creative_growth_page_rejects_unbounded_or_invalid_ranges(
    limit: int,
    offset: int,
) -> None:
    conn, _owner, analyst, _jobs = _db()
    repo = CreativeGrowthRepository(conn)
    with pytest.raises(ValueError):
        repo.list_page(actor=analyst, limit=limit, offset=offset)
