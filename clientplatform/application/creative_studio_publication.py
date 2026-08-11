from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from clientplatform.application.ad_publication_assets import attach_image_file
from clientplatform.application.creative_studio import (
    StudioVariant,
    build_ad_studio_variants,
    submit_studio_variant,
)
from clientplatform.domain.ad_publication_assets import (
    AdPublicationAsset,
    AdPublicationAssetError,
    AdPublicationAssetSource,
)
from clientplatform.domain.creative_variant_bindings import (
    CreativeVariantBinding,
    CreativeVariantBindingStatus,
    ObservableCreativeVariant,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.domain.visual_brand import TenantBrandDNA
from clientplatform.infrastructure.creative_variant_binding_repository import (
    CreativeVariantBindingRepository,
)
from services.db import get_db, get_db_ro
from services.visual_creative_gateway import (
    VisualCreativeGatewayError,
    VisualCreativeJob,
    VisualRenderPack,
    download_render_asset,
    poll_visual,
    render_visual_pack,
)

_GOAL_FORMATS = ("square", "feed", "story", "landscape")
_GOAL_LABELS = (
    "🤝 Доверие и живой человек",
    "🧭 Понятный процесс",
    "✨ Спокойная ценность",
)


class CreativeStudioPublicationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GoalStudioPublicationResult:
    job: VisualCreativeJob
    render: VisualRenderPack | None
    binding: CreativeVariantBinding
    asset: AdPublicationAsset | None = None

    @property
    def attached(self) -> bool:
        return (
            self.asset is not None
            and self.binding.status == CreativeVariantBindingStatus.ATTACHED
        )


def build_goal_image_variants(
    *,
    business_id: str,
    publication_job_id: str,
    title: str,
    body: str,
    country_code: str = "",
) -> tuple[StudioVariant, ...]:
    return build_ad_studio_variants(
        business_id=business_id,
        publication_job_id=publication_job_id,
        title=title,
        body=body,
        kind="image",
        brand=TenantBrandDNA(business_id=business_id),
        formats=_GOAL_FORMATS,
        country_code=country_code,
    )


def goal_variant_labels(variants: tuple[StudioVariant, ...]) -> tuple[str, ...]:
    if len(variants) != len(_GOAL_LABELS):
        raise ValueError("unexpected_goal_studio_variant_count")
    return _GOAL_LABELS


def selected_goal_variant(
    *,
    business_id: str,
    publication_job_id: str,
    title: str,
    body: str,
    country_code: str,
    index: int,
) -> StudioVariant:
    variants = build_goal_image_variants(
        business_id=business_id,
        publication_job_id=publication_job_id,
        title=title,
        body=body,
        country_code=country_code,
    )
    selected = int(index)
    if selected < 0 or selected >= len(variants):
        raise ValueError("creative_variant_index_out_of_range")
    return variants[selected]


def _select_binding(
    *,
    actor: TenantContext,
    publication_job_id: str,
    variant: StudioVariant,
) -> CreativeVariantBinding:
    if variant.business_id != actor.business_id:
        raise ValueError("creative_variant_business_mismatch")
    with get_db() as conn:
        return CreativeVariantBindingRepository(conn).select(
            actor=actor,
            publication_job_id=publication_job_id,
            experiment_id=variant.experiment_id,
            variant_id=variant.variant_id,
            angle_id=variant.angle_id,
            country_code=variant.country_code,
        )


def _current_binding(*, actor: TenantContext, publication_job_id: str) -> CreativeVariantBinding:
    with get_db_ro() as conn:
        return CreativeVariantBindingRepository(conn).get_current(
            actor=actor,
            publication_job_id=publication_job_id,
        )


def _remember(
    *,
    actor: TenantContext,
    publication_job_id: str,
    variant: StudioVariant,
    source_job_id: str,
    render_pack_id: str,
    status: CreativeVariantBindingStatus,
) -> CreativeVariantBinding:
    with get_db() as conn:
        return CreativeVariantBindingRepository(conn).remember_progress(
            actor=actor,
            publication_job_id=publication_job_id,
            variant_id=variant.variant_id,
            source_job_id=source_job_id,
            render_pack_id=render_pack_id,
            status=status,
        )


def _render_current(job: VisualCreativeJob, variant: StudioVariant) -> VisualRenderPack | None:
    if job.status != "succeeded" or not job.asset_ready:
        return None
    return render_visual_pack(
        job,
        formats=variant.formats,
        composition=variant.composition,
        idempotency_key=f"clientplatform:{variant.variant_id}:render",
    )


def _attach_square(
    *,
    actor: TenantContext,
    publication_job_id: str,
    variant: StudioVariant,
    job: VisualCreativeJob,
    render: VisualRenderPack,
) -> tuple[AdPublicationAsset, CreativeVariantBinding]:
    if render.status != "succeeded":
        raise CreativeStudioPublicationError("creative_render_not_ready")
    if render.scope_id != actor.business_id or render.source_job_id != job.id:
        raise CreativeStudioPublicationError("creative_render_binding_mismatch")
    temporary: Path | None = None
    try:
        temporary = download_render_asset(render, "square")
        binding = _current_binding(actor=actor, publication_job_id=publication_job_id)
        if binding.variant_id != variant.variant_id:
            raise ValueError("creative_variant_binding_changed")
        if binding.source_job_id and binding.source_job_id != job.id:
            raise ValueError("creative_source_job_binding_changed")
        asset = attach_image_file(
            actor=actor,
            publication_job_id=publication_job_id,
            path=temporary,
            source=AdPublicationAssetSource.GENERATED,
        )
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    binding = _remember(
        actor=actor,
        publication_job_id=publication_job_id,
        variant=variant,
        source_job_id=job.id,
        render_pack_id=render.id,
        status=CreativeVariantBindingStatus.ATTACHED,
    )
    return asset, binding


def _progress_result(
    *,
    actor: TenantContext,
    publication_job_id: str,
    variant: StudioVariant,
    job: VisualCreativeJob,
    render: VisualRenderPack | None,
) -> GoalStudioPublicationResult:
    if job.status == "failed":
        status = CreativeVariantBindingStatus.FAILED
    elif job.status in {"queued", "running"}:
        status = CreativeVariantBindingStatus.GENERATING
    elif render is None or render.status == "running":
        status = CreativeVariantBindingStatus.RENDERING
    elif render.status == "failed":
        status = CreativeVariantBindingStatus.FAILED
    else:
        try:
            asset, binding = _attach_square(
                actor=actor,
                publication_job_id=publication_job_id,
                variant=variant,
                job=job,
                render=render,
            )
        except AdPublicationAssetError as exc:
            raise CreativeStudioPublicationError("creative_asset_attachment_failed") from exc
        except VisualCreativeGatewayError as exc:
            raise CreativeStudioPublicationError("creative_asset_attachment_failed") from exc
        except OSError as exc:
            raise CreativeStudioPublicationError("creative_asset_attachment_failed") from exc
        except ValueError as exc:
            raise CreativeStudioPublicationError("creative_asset_attachment_failed") from exc
        return GoalStudioPublicationResult(job=job, render=render, binding=binding, asset=asset)
    binding = _remember(
        actor=actor,
        publication_job_id=publication_job_id,
        variant=variant,
        source_job_id=job.id,
        render_pack_id="" if render is None else render.id,
        status=status,
    )
    return GoalStudioPublicationResult(job=job, render=render, binding=binding)


def start_goal_image_variant(
    *,
    actor: TenantContext,
    publication_job_id: str,
    variant: StudioVariant,
    wait_seconds: int = 20,
) -> GoalStudioPublicationResult:
    _select_binding(actor=actor, publication_job_id=publication_job_id, variant=variant)
    try:
        job, render = submit_studio_variant(variant, wait_seconds=wait_seconds)
        return _progress_result(
            actor=actor,
            publication_job_id=publication_job_id,
            variant=variant,
            job=job,
            render=render,
        )
    except CreativeStudioPublicationError:
        raise
    except VisualCreativeGatewayError as exc:
        # Selection is deliberately durable before the external call. A retry uses
        # the same variant-derived gateway idempotency key instead of minting a
        # second paid generation after an ambiguous transport failure.
        raise CreativeStudioPublicationError("creative_generation_submission_failed") from exc
    except ValueError as exc:
        raise CreativeStudioPublicationError("creative_generation_submission_failed") from exc


def poll_goal_image_variant(
    *,
    actor: TenantContext,
    publication_job_id: str,
    job_id: str,
    variant: StudioVariant,
) -> GoalStudioPublicationResult:
    try:
        binding = _current_binding(actor=actor, publication_job_id=publication_job_id)
        if binding.variant_id != variant.variant_id:
            raise ValueError("creative_variant_binding_changed")
        if binding.source_job_id and binding.source_job_id != str(job_id or "").strip():
            raise ValueError("creative_source_job_binding_changed")
        job = poll_visual(job_id, scope_id=actor.business_id)
        render = _render_current(job, variant)
        return _progress_result(
            actor=actor,
            publication_job_id=publication_job_id,
            variant=variant,
            job=job,
            render=render,
        )
    except CreativeStudioPublicationError:
        raise
    except VisualCreativeGatewayError as exc:
        raise CreativeStudioPublicationError("creative_generation_poll_failed") from exc
    except ValueError as exc:
        raise CreativeStudioPublicationError("creative_generation_poll_failed") from exc


def list_observable_creative_variants(
    *, actor: TenantContext
) -> tuple[ObservableCreativeVariant, ...]:
    with get_db_ro() as conn:
        return CreativeVariantBindingRepository(conn).list_observable(actor=actor)


__all__ = [
    "CreativeStudioPublicationError",
    "GoalStudioPublicationResult",
    "build_goal_image_variants",
    "goal_variant_labels",
    "list_observable_creative_variants",
    "poll_goal_image_variant",
    "selected_goal_variant",
    "start_goal_image_variant",
]
