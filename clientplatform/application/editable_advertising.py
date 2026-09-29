from __future__ import annotations

import hashlib
import json

from clientplatform.domain.editable_advertising import EditableAdProject
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.editable_ad_project_repository import (
    EditableAdProjectRepository,
)
from services.db import get_db, get_db_ro
from services.visual_creative_gateway import (
    VisualCreativeGatewayError,
    VisualRenderPack,
    poll_visual,
    render_visual_pack,
)


class EditableAdvertisingError(RuntimeError):
    """Sanitized editable-ad orchestration failure."""


class EditableAdvertisingSourceExpired(EditableAdvertisingError):
    """The exact AI source completed earlier but its transient bytes are gone."""


_DEFAULT_FORMATS = ("square",)


def create_editable_ad_project(
    *,
    actor: TenantContext,
    publication_job_id: str,
    kind: str,
    headline: str,
    body: str,
    brand: dict[str, str],
    cta: str = "Записаться",
    layout: str = "lower_card",
) -> EditableAdProject:
    with get_db() as conn:
        return EditableAdProjectRepository(conn).create_or_get(
            actor=actor,
            publication_job_id=publication_job_id,
            kind=kind,
            headline=headline,
            body=body,
            cta=cta,
            layout=layout,
            brand=brand,
        )


def get_editable_ad_project(
    *,
    actor: TenantContext,
    project_id: str,
) -> EditableAdProject:
    with get_db_ro() as conn:
        return EditableAdProjectRepository(conn).get(
            actor=actor,
            project_id=project_id,
        )


def prepare_editable_ad_source(
    *,
    actor: TenantContext,
    project_id: str,
) -> EditableAdProject:
    with get_db() as conn:
        return EditableAdProjectRepository(conn).prepare_new_source(
            actor=actor,
            project_id=project_id,
        )


def update_editable_ad_composition(
    *,
    actor: TenantContext,
    project_id: str,
    headline: str | None = None,
    body: str | None = None,
    cta: str | None = None,
    layout: str | None = None,
) -> EditableAdProject:
    with get_db() as conn:
        return EditableAdProjectRepository(conn).update_composition(
            actor=actor,
            project_id=project_id,
            headline=headline,
            body=body,
            cta=cta,
            layout=layout,
        )


def bind_editable_ad_source(
    *,
    actor: TenantContext,
    project_id: str,
    source_job_id: str,
) -> EditableAdProject:
    with get_db() as conn:
        return EditableAdProjectRepository(conn).bind_source(
            actor=actor,
            project_id=project_id,
            source_job_id=source_job_id,
        )


def finish_editable_ad_project(
    *,
    actor: TenantContext,
    project_id: str,
) -> EditableAdProject:
    with get_db() as conn:
        return EditableAdProjectRepository(conn).finish(
            actor=actor,
            project_id=project_id,
        )


def _render_key(project: EditableAdProject, formats: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "project_id": project.id,
            "revision": project.revision,
            "source_job_id": project.source_job_id,
            "composition": project.composition(),
            "formats": list(formats),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"clientplatform:editable:{digest}"


def _mark_expired(
    *,
    actor: TenantContext,
    project: EditableAdProject,
) -> None:
    with get_db() as conn:
        EditableAdProjectRepository(conn).mark_source_expired(
            actor=actor,
            project_id=project.id,
            expected_source_job_id=project.source_job_id,
        )


def render_editable_ad_project(
    *,
    actor: TenantContext,
    project_id: str,
    formats: tuple[str, ...] = _DEFAULT_FORMATS,
) -> tuple[EditableAdProject, VisualRenderPack]:
    selected = tuple(dict.fromkeys(str(item or "").strip().lower() for item in formats))
    if not selected or len(selected) > 4:
        raise ValueError("editable_ad_formats_invalid")
    with get_db_ro() as conn:
        project = EditableAdProjectRepository(conn).get(
            actor=actor,
            project_id=project_id,
        )
    if not project.source_job_id:
        raise EditableAdvertisingSourceExpired("editable_ad_source_missing")
    try:
        job = poll_visual(project.source_job_id, scope_id=actor.business_id)
    except (VisualCreativeGatewayError, ValueError) as exc:
        raise EditableAdvertisingError("editable_ad_source_poll_failed") from exc
    if job.status == "succeeded" and not job.asset_ready:
        _mark_expired(actor=actor, project=project)
        raise EditableAdvertisingSourceExpired("editable_ad_source_expired")
    if job.status != "succeeded" or not job.asset_ready:
        raise EditableAdvertisingError("editable_ad_source_not_ready")
    if job.kind != project.kind:
        raise EditableAdvertisingError("editable_ad_source_kind_mismatch")
    try:
        pack = render_visual_pack(
            job,
            formats=selected,
            composition=project.composition(),
            idempotency_key=_render_key(project, selected),
        )
    except VisualCreativeGatewayError as exc:
        if str(exc) == "visual_source_not_ready":
            _mark_expired(actor=actor, project=project)
            raise EditableAdvertisingSourceExpired("editable_ad_source_expired") from exc
        raise EditableAdvertisingError("editable_ad_render_failed") from exc
    except ValueError as exc:
        raise EditableAdvertisingError("editable_ad_render_failed") from exc
    if pack.status != "succeeded":
        raise EditableAdvertisingError("editable_ad_render_not_ready")
    return project, pack


__all__ = [
    "EditableAdvertisingError",
    "EditableAdvertisingSourceExpired",
    "bind_editable_ad_source",
    "create_editable_ad_project",
    "finish_editable_ad_project",
    "get_editable_ad_project",
    "prepare_editable_ad_source",
    "render_editable_ad_project",
    "update_editable_ad_composition",
]
