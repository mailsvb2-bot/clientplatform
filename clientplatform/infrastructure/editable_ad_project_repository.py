from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.editable_advertising import (
    EditableAdProject,
    EditableAdProjectStatus,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _project(row: Any) -> EditableAdProject:
    return EditableAdProject(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        created_by_member_id=str(_value(row, "created_by_member_id", 2)),
        publication_job_id=str(_value(row, "publication_job_id", 3)),
        kind=str(_value(row, "kind", 4)),
        headline=str(_value(row, "headline", 5)),
        body=str(_value(row, "body", 6)),
        cta=str(_value(row, "cta", 7)),
        layout=str(_value(row, "layout", 8)),
        brand_json=str(_value(row, "brand_json", 9)),
        source_job_id=str(_value(row, "source_job_id", 10)),
        status=EditableAdProjectStatus(str(_value(row, "status", 11))),
        revision=int(_value(row, "revision", 12)),
        created_at=str(_value(row, "created_at", 13)),
        updated_at=str(_value(row, "updated_at", 14)),
    )


_SELECT = """
SELECT id,business_id,created_by_member_id,publication_job_id,kind,
       headline,body,cta,layout,brand_json,source_job_id,
       status,revision,created_at,updated_at
FROM editable_ad_projects
"""


def _clean(value: object, limit: int, *, required: bool = False) -> str:
    token = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if required and not token:
        raise ValueError("editable_ad_value_required")
    if len(token) > limit or any(ord(char) < 32 for char in token):
        raise ValueError("editable_ad_value_invalid")
    return token


def _brand_json(brand: dict[str, str]) -> str:
    defaults = {
        "primary_color": "#172033",
        "accent_color": "#E9C46A",
        "text_color": "#FFFFFF",
    }
    normalized: dict[str, str] = {}
    for key, default in defaults.items():
        token = str(brand.get(key) or default).strip().upper()
        if _COLOR_RE.fullmatch(token) is None:
            raise ValueError("editable_ad_brand_invalid")
        normalized[key] = token
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class EditableAdProjectRepository:
    """Tenant-scoped durable editable composition with no persisted media bytes."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _actor(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        return current

    def _assert_editable_publication(self, *, business_id: str, publication_job_id: str) -> None:
        row = self._conn.execute(
            """
            SELECT status FROM ad_publication_jobs
            WHERE id=? AND business_id=? LIMIT 1
            """,
            (publication_job_id, business_id),
        ).fetchone()
        if row is None:
            raise ValueError("advertising publication draft was not found")
        status = str(_value(row, "status", 0))
        if status not in {"draft", "failed", "submitted"}:
            raise ValueError("advertising publication can no longer be edited")

    def get(self, *, actor: TenantContext, project_id: str) -> EditableAdProject:
        current = self._actor(actor)
        normalized = normalize_uuid(project_id, field_name="editable_ad_project_id")
        row = self._conn.execute(
            _SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise LookupError("editable advertising project was not found")
        value = _project(row)
        if value.created_by_member_id != current.membership_id:
            raise LookupError("editable advertising project was not found")
        return value

    def get_for_publication(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
        kind: str,
    ) -> EditableAdProject | None:
        current = self._actor(actor)
        publication = normalize_uuid(publication_job_id, field_name="publication_job_id")
        visual_kind = str(kind or "").strip().lower()
        if visual_kind not in {"image", "video"}:
            raise ValueError("editable_ad_kind_invalid")
        row = self._conn.execute(
            _SELECT
            + " WHERE business_id=? AND created_by_member_id=? "
            + "AND publication_job_id=? AND kind=? LIMIT 1",
            (current.business_id, current.membership_id, publication, visual_kind),
        ).fetchone()
        return None if row is None else _project(row)

    def create_or_get(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
        kind: str,
        headline: str,
        body: str,
        cta: str,
        layout: str,
        brand: dict[str, str],
        now: str | None = None,
    ) -> EditableAdProject:
        current = self._actor(actor)
        publication = normalize_uuid(publication_job_id, field_name="publication_job_id")
        self._assert_editable_publication(
            business_id=current.business_id,
            publication_job_id=publication,
        )
        visual_kind = str(kind or "").strip().lower()
        if visual_kind not in {"image", "video"}:
            raise ValueError("editable_ad_kind_invalid")
        existing = self.get_for_publication(
            actor=current,
            publication_job_id=publication,
            kind=visual_kind,
        )
        if existing is not None:
            return existing

        clean_headline = _clean(headline, 160, required=True)
        clean_body = _clean(body, 500, required=True)
        clean_cta = _clean(cta, 80)
        clean_layout = str(layout or "lower_card").strip().lower()
        if clean_layout not in {"lower_card", "top_card"}:
            raise ValueError("editable_ad_layout_invalid")
        brand_json = _brand_json(brand)
        timestamp = str(now or _iso_now())
        project_id = str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO editable_ad_projects(
                id,business_id,created_by_member_id,publication_job_id,kind,
                headline,body,cta,layout,brand_json,source_job_id,
                status,revision,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'','draft',1,?,?)
            ON CONFLICT(business_id, created_by_member_id, publication_job_id, kind)
            DO NOTHING
            """,
            (
                project_id,
                current.business_id,
                current.membership_id,
                publication,
                visual_kind,
                clean_headline,
                clean_body,
                clean_cta,
                clean_layout,
                brand_json,
                timestamp,
                timestamp,
            ),
        )
        created = self.get_for_publication(
            actor=current,
            publication_job_id=publication,
            kind=visual_kind,
        )
        if created is None:
            raise RuntimeError("editable advertising project creation disappeared")
        return created

    def prepare_new_source(
        self,
        *,
        actor: TenantContext,
        project_id: str,
        now: str | None = None,
    ) -> EditableAdProject:
        current = self._actor(actor)
        value = self.get(actor=current, project_id=project_id)
        self._assert_editable_publication(
            business_id=current.business_id,
            publication_job_id=value.publication_job_id,
        )
        if value.status == EditableAdProjectStatus.DRAFT and not value.source_job_id:
            return value
        if value.status != EditableAdProjectStatus.SOURCE_EXPIRED:
            raise ValueError("editable_ad_source_rotation_not_allowed")
        cursor = self._conn.execute(
            """
            UPDATE editable_ad_projects
            SET source_job_id='', status='draft', revision=revision+1, updated_at=?
            WHERE id=? AND business_id=? AND status='source_expired'
            """,
            (str(now or _iso_now()), value.id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ValueError("editable_ad_source_state_changed")
        return self.get(actor=current, project_id=value.id)

    def advance_failed_source_revision(
        self,
        *,
        actor: TenantContext,
        project_id: str,
        now: str | None = None,
    ) -> EditableAdProject:
        """Rotate only after a definitive failed provider job.

        Ambiguous submit/poll failures must keep the same revision so the next
        explicit retry reuses the same generation idempotency key.
        """

        current = self._actor(actor)
        value = self.get(actor=current, project_id=project_id)
        if value.status != EditableAdProjectStatus.DRAFT or value.source_job_id:
            return value
        cursor = self._conn.execute(
            """
            UPDATE editable_ad_projects
            SET revision=revision+1, updated_at=?
            WHERE id=? AND business_id=? AND status='draft' AND source_job_id=''
              AND revision=?
            """,
            (
                str(now or _iso_now()),
                value.id,
                current.business_id,
                value.revision,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) not in {0, 1}:
            raise RuntimeError("editable advertising source revision transition failed")
        return self.get(actor=current, project_id=value.id)

    def bind_source(
        self,
        *,
        actor: TenantContext,
        project_id: str,
        source_job_id: str,
        now: str | None = None,
    ) -> EditableAdProject:
        current = self._actor(actor)
        value = self.get(actor=current, project_id=project_id)
        self._assert_editable_publication(
            business_id=current.business_id,
            publication_job_id=value.publication_job_id,
        )
        job_id = _clean(source_job_id, 128, required=True)
        if value.source_job_id:
            if value.source_job_id == job_id and value.status in {
                EditableAdProjectStatus.SOURCE_READY,
                EditableAdProjectStatus.FINISHED,
            }:
                return value
            raise ValueError("editable_ad_source_rebind_forbidden")
        if value.status != EditableAdProjectStatus.DRAFT:
            raise ValueError("editable_ad_source_state_invalid")
        cursor = self._conn.execute(
            """
            UPDATE editable_ad_projects
            SET source_job_id=?, status='source_ready', updated_at=?
            WHERE id=? AND business_id=? AND status='draft' AND source_job_id=''
            """,
            (job_id, str(now or _iso_now()), value.id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ValueError("editable_ad_source_state_changed")
        return self.get(actor=current, project_id=value.id)

    def update_composition(
        self,
        *,
        actor: TenantContext,
        project_id: str,
        headline: str | None = None,
        body: str | None = None,
        cta: str | None = None,
        layout: str | None = None,
        now: str | None = None,
    ) -> EditableAdProject:
        current = self._actor(actor)
        value = self.get(actor=current, project_id=project_id)
        self._assert_editable_publication(
            business_id=current.business_id,
            publication_job_id=value.publication_job_id,
        )
        next_headline = value.headline if headline is None else _clean(headline, 160, required=True)
        next_body = value.body if body is None else _clean(body, 500, required=True)
        next_cta = value.cta if cta is None else _clean(cta, 80)
        next_layout = value.layout if layout is None else str(layout).strip().lower()
        if next_layout not in {"lower_card", "top_card"}:
            raise ValueError("editable_ad_layout_invalid")
        next_status = value.status
        if value.status in {
            EditableAdProjectStatus.SOURCE_READY,
            EditableAdProjectStatus.FINISHED,
        } and value.source_job_id:
            next_status = EditableAdProjectStatus.SOURCE_READY
        self._conn.execute(
            """
            UPDATE editable_ad_projects
            SET headline=?, body=?, cta=?, layout=?, status=?,
                revision=revision+1, updated_at=?
            WHERE id=? AND business_id=?
            """,
            (
                next_headline,
                next_body,
                next_cta,
                next_layout,
                next_status.value,
                str(now or _iso_now()),
                value.id,
                current.business_id,
            ),
        )
        return self.get(actor=current, project_id=value.id)

    def mark_source_expired(
        self,
        *,
        actor: TenantContext,
        project_id: str,
        expected_source_job_id: str,
        now: str | None = None,
    ) -> EditableAdProject:
        current = self._actor(actor)
        value = self.get(actor=current, project_id=project_id)
        expected = _clean(expected_source_job_id, 128, required=True)
        if value.source_job_id != expected:
            return value
        if value.status == EditableAdProjectStatus.SOURCE_EXPIRED:
            return value
        cursor = self._conn.execute(
            """
            UPDATE editable_ad_projects
            SET status='source_expired', updated_at=?
            WHERE id=? AND business_id=? AND source_job_id=?
              AND status IN ('source_ready','finished')
            """,
            (
                str(now or _iso_now()),
                value.id,
                current.business_id,
                expected,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) not in {0, 1}:
            raise RuntimeError("editable advertising source transition failed")
        return self.get(actor=current, project_id=value.id)

    def finish(
        self,
        *,
        actor: TenantContext,
        project_id: str,
        now: str | None = None,
    ) -> EditableAdProject:
        current = self._actor(actor)
        value = self.get(actor=current, project_id=project_id)
        self._assert_editable_publication(
            business_id=current.business_id,
            publication_job_id=value.publication_job_id,
        )
        if value.status == EditableAdProjectStatus.FINISHED and value.source_job_id:
            return value
        if value.status != EditableAdProjectStatus.SOURCE_READY or not value.source_job_id:
            raise ValueError("editable_ad_project_not_ready")
        cursor = self._conn.execute(
            """
            UPDATE editable_ad_projects
            SET status='finished', updated_at=?
            WHERE id=? AND business_id=? AND status='source_ready' AND source_job_id<>''
            """,
            (str(now or _iso_now()), value.id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ValueError("editable_ad_project_state_changed")
        return self.get(actor=current, project_id=value.id)


__all__ = ["EditableAdProjectRepository"]
