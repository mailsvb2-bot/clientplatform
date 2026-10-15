from __future__ import annotations

import dataclasses

from clientplatform.application import visual_creatives as vc
from services.visual_creative_gateway import VisualCreativeBrief, VisualCreativeJob, VisualSemanticQA


def _job(job_id: str, **kw) -> VisualCreativeJob:
    base = {
        field.name: (field.default if field.default is not dataclasses.MISSING else "")
        for field in dataclasses.fields(VisualCreativeJob)
    }
    base.update(id=job_id, kind="image", scope_id="s1", status="succeeded", asset_ready=True)
    base.update(kw)
    return VisualCreativeJob(**base)


def test_best_variant_prefers_pass_and_preserves_idempotency(monkeypatch):
    submitted = []
    verdicts = {"j0": "needs_review", "j1": "pass", "j2": "unavailable"}

    def submit(brief, *, scope_id, idempotency_key, wait_seconds=0):
        submitted.append((idempotency_key, brief.seed))
        return _job(f"j{len(submitted) - 1}")

    monkeypatch.setattr(vc, "submit_visual", submit)
    monkeypatch.setattr(vc, "wait_visual", lambda job, wait_seconds=0: job)
    monkeypatch.setattr(vc, "review_visual_semantics", lambda job, contract: VisualSemanticQA(status=verdicts[job.id]))
    job = vc._best_image_variant(
        VisualCreativeBrief(kind="image", prompt="чашка кофе"),
        contract={},
        variants=3,
        scope_id="s1",
        idempotency_key="idem-0001",
    )
    assert job.id == "j1"
    assert [key for key, _ in submitted] == ["idem-0001", "idem-0001:v1", "idem-0001:v2"]
    assert len({seed for _, seed in submitted}) == 3


def test_variant_count_bounded(monkeypatch):
    monkeypatch.setenv("VISUAL_IMAGE_VARIANTS", "999")
    assert vc._image_variant_count() == 4
    monkeypatch.setenv("VISUAL_IMAGE_VARIANTS", "invalid")
    assert vc._image_variant_count() == 3
