from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from uuid import uuid4

from clientplatform.application import creative_studio_publication as publication
from clientplatform.domain.creative_variant_bindings import CreativeVariantBindingStatus
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.infrastructure.creative_variant_binding_repository import (
    CreativeVariantBindingRepository,
)
from services.db.schema import clientplatform_creative_experiments
from services.visual_creative_gateway import VisualCreativeJob, VisualRenderAsset, VisualRenderPack


def _binding_db() -> tuple[sqlite3.Connection, TenantContext, str]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE businesses(id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE business_members(id TEXT NOT NULL, business_id TEXT NOT NULL, "
        "user_id INTEGER NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL, "
        "PRIMARY KEY(id, business_id))"
    )
    conn.execute(
        "CREATE TABLE ad_publication_jobs(id TEXT NOT NULL, business_id TEXT NOT NULL, "
        "status TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, "
        "external_ad_id TEXT, promotion_campaign_id TEXT NOT NULL, "
        "PRIMARY KEY(id, business_id))"
    )
    conn.execute(
        "CREATE TABLE ad_publication_assets(publication_job_id TEXT NOT NULL, "
        "business_id TEXT NOT NULL, source TEXT NOT NULL, "
        "PRIMARY KEY(publication_job_id, business_id))"
    )
    clientplatform_creative_experiments.ensure(conn)
    business_id, membership_id, job_id = str(uuid4()), str(uuid4()), str(uuid4())
    conn.execute("INSERT INTO businesses VALUES(?, 'active')", (business_id,))
    conn.execute(
        "INSERT INTO business_members VALUES(?, ?, 1, 'owner', 'active')",
        (membership_id, business_id),
    )
    conn.execute(
        "INSERT INTO ad_publication_jobs VALUES(?, ?, 'draft', 'Title', 'Body', '', 'promo-1')",
        (job_id, business_id),
    )
    return (
        conn,
        TenantContext(
            business_id=business_id,
            user_id=1,
            membership_id=membership_id,
            role=PlatformRole.OWNER,
        ),
        job_id,
    )


def test_variant_binding_is_durable_and_invalidates_when_copy_or_media_changes() -> None:
    conn, actor, job_id = _binding_db()
    repo = CreativeVariantBindingRepository(conn)
    selected = repo.select(
        actor=actor,
        publication_job_id=job_id,
        experiment_id="cpexp_test",
        variant_id="cpv_test",
        angle_id="human_trust",
        country_code="RU",
    )
    assert selected.status == CreativeVariantBindingStatus.SELECTED
    assert selected.copy_digest == hashlib.sha256(b"Title\nBody").hexdigest()
    repo.remember_progress(
        actor=actor,
        publication_job_id=job_id,
        variant_id="cpv_test",
        source_job_id="job1",
        render_pack_id="pack1",
        status=CreativeVariantBindingStatus.ATTACHED,
    )
    conn.execute(
        "UPDATE ad_publication_jobs SET status='submitted', external_ad_id='123' "
        "WHERE id=? AND business_id=?",
        (job_id, actor.business_id),
    )
    conn.execute(
        "INSERT INTO ad_publication_assets VALUES(?, ?, 'generated')",
        (job_id, actor.business_id),
    )
    observed = repo.list_observable(actor=actor)
    assert len(observed) == 1
    assert observed[0].external_ad_id == "123"

    conn.execute(
        "UPDATE ad_publication_jobs SET text='Changed' WHERE id=? AND business_id=?",
        (job_id, actor.business_id),
    )
    assert repo.list_observable(actor=actor) == ()
    try:
        repo.remember_progress(
            actor=actor,
            publication_job_id=job_id,
            variant_id="cpv_test",
            source_job_id="job1",
            render_pack_id="pack1",
            status=CreativeVariantBindingStatus.ATTACHED,
        )
    except ValueError as exc:
        assert str(exc) == "creative_copy_binding_changed"
    else:
        raise AssertionError("stale creative progress was accepted after ad copy changed")

    conn.execute(
        "UPDATE ad_publication_jobs SET text='Body' WHERE id=? AND business_id=?",
        (job_id, actor.business_id),
    )
    conn.execute(
        "UPDATE ad_publication_assets SET source='upload' "
        "WHERE publication_job_id=? AND business_id=?",
        (job_id, actor.business_id),
    )
    assert repo.list_observable(actor=actor) == ()


def test_start_persists_selection_before_paid_generation(monkeypatch) -> None:
    business_id = str(uuid4())
    actor = TenantContext(
        business_id=business_id,
        user_id=1,
        membership_id=str(uuid4()),
        role=PlatformRole.OWNER,
    )
    variant = publication.build_goal_image_variants(
        business_id=business_id,
        publication_job_id=str(uuid4()),
        title="Консультация",
        body="Понятный следующий шаг",
        country_code="RU",
    )[0]
    calls: list[str] = []
    binding = type("Binding", (), {"status": CreativeVariantBindingStatus.SELECTED})()
    monkeypatch.setattr(
        publication, "_select_binding", lambda **kwargs: calls.append("select") or binding
    )
    monkeypatch.setattr(
        publication,
        "submit_studio_variant",
        lambda *args, **kwargs: (
            VisualCreativeJob("job1", "fake", business_id, "image", "queued"),
            None,
        ),
    )
    progress = type("Binding", (), {"status": CreativeVariantBindingStatus.GENERATING})()
    monkeypatch.setattr(
        publication, "_remember", lambda **kwargs: calls.append("remember") or progress
    )
    result = publication.start_goal_image_variant(
        actor=actor,
        publication_job_id=str(uuid4()),
        variant=variant,
    )
    assert calls == ["select", "remember"]
    assert result.job.id == "job1"


def test_ready_render_uses_square_and_canonical_generated_asset(
    monkeypatch, tmp_path: Path
) -> None:
    business_id = str(uuid4())
    actor = TenantContext(
        business_id=business_id,
        user_id=1,
        membership_id=str(uuid4()),
        role=PlatformRole.OWNER,
    )
    publication_job_id = str(uuid4())
    variant = publication.build_goal_image_variants(
        business_id=business_id,
        publication_job_id=publication_job_id,
        title="Консультация",
        body="Понятный следующий шаг",
        country_code="RU",
    )[0]
    job = VisualCreativeJob("job1", "fake", business_id, "image", "succeeded", asset_ready=True)
    asset_meta = VisualRenderAsset(
        format_id="square",
        kind="image",
        width=1080,
        height=1080,
        mime_type="image/jpeg",
        sha256="0" * 64,
        asset_ready=True,
        quality={},
    )
    pack = VisualRenderPack("pack1", business_id, "job1", "succeeded", "", (asset_meta,))
    rendered = tmp_path / "square.jpg"
    rendered.write_bytes(b"render")
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        publication,
        "download_render_asset",
        lambda p, f: seen.update(format=f) or rendered,
    )
    current = type(
        "Binding",
        (),
        {"variant_id": variant.variant_id, "source_job_id": "job1"},
    )()
    monkeypatch.setattr(publication, "_current_binding", lambda **kwargs: current)

    fake_asset = type("Asset", (), {"storage_path": str(tmp_path / "persisted.jpg")})()

    def attach(**kwargs):
        seen["source"] = kwargs["source"]
        seen["path"] = kwargs["path"]
        return fake_asset

    monkeypatch.setattr(publication, "attach_image_file", attach)
    binding = type("Binding", (), {"status": CreativeVariantBindingStatus.ATTACHED})()
    monkeypatch.setattr(publication, "_remember", lambda **kwargs: binding)
    asset, returned_binding = publication._attach_square(
        actor=actor,
        publication_job_id=publication_job_id,
        variant=variant,
        job=job,
        render=pack,
    )
    assert asset is fake_asset
    assert returned_binding is binding
    assert seen["format"] == "square"
    assert str(seen["source"]) == "generated"
    assert seen["path"] == rendered
    assert not rendered.exists()
