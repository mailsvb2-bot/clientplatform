from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.creative_variant_bindings import (
    CreativeVariantBinding,
    CreativeVariantBindingStatus,
    ObservableCreativeVariant,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


_SELECT = """
    SELECT publication_job_id, business_id, experiment_id, variant_id, angle_id,
           country_code, copy_digest, source_job_id, render_pack_id, status,
           created_by_member_id, created_at, updated_at
    FROM creative_variant_bindings
"""


def _binding(row: Any) -> CreativeVariantBinding:
    return CreativeVariantBinding(
        publication_job_id=str(_value(row, "publication_job_id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        experiment_id=str(_value(row, "experiment_id", 2)),
        variant_id=str(_value(row, "variant_id", 3)),
        angle_id=str(_value(row, "angle_id", 4)),
        country_code=str(_value(row, "country_code", 5)),
        copy_digest=str(_value(row, "copy_digest", 6)),
        source_job_id=str(_value(row, "source_job_id", 7)),
        render_pack_id=str(_value(row, "render_pack_id", 8)),
        status=CreativeVariantBindingStatus(str(_value(row, "status", 9))),
        created_by_member_id=str(_value(row, "created_by_member_id", 10)),
        created_at=str(_value(row, "created_at", 11)),
        updated_at=str(_value(row, "updated_at", 12)),
    ).normalized()


class CreativeVariantBindingRepository:
    """Durable tenant binding from a studio variant to its ad publication job."""

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

    def _job_copy_digest(self, *, business_id: str, publication_job_id: str) -> str:
        row = self._conn.execute(
            """
            SELECT status, title, text FROM ad_publication_jobs
            WHERE id=? AND business_id=? LIMIT 1
            """,
            (publication_job_id, business_id),
        ).fetchone()
        if row is None:
            raise ValueError("advertising publication draft was not found")
        if str(_value(row, "status", 0)) not in {"draft", "failed", "submitted"}:
            raise ValueError("advertising creative can no longer be changed")
        title = str(_value(row, "title", 1) or "")
        text = str(_value(row, "text", 2) or "")
        return hashlib.sha256(f"{title}\n{text}".encode("utf-8")).hexdigest()

    def select(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
        experiment_id: str,
        variant_id: str,
        angle_id: str,
        country_code: str,
    ) -> CreativeVariantBinding:
        current = self._actor(actor)
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        copy_digest = self._job_copy_digest(
            business_id=current.business_id,
            publication_job_id=job_id,
        )
        probe = CreativeVariantBinding(
            publication_job_id=job_id,
            business_id=current.business_id,
            experiment_id=experiment_id,
            variant_id=variant_id,
            angle_id=angle_id,
            country_code=country_code,
            copy_digest=copy_digest,
            source_job_id="",
            render_pack_id="",
            status=CreativeVariantBindingStatus.SELECTED,
            created_by_member_id=current.membership_id,
            created_at="now",
            updated_at="now",
        ).normalized()
        previous = self._conn.execute(
            _SELECT + " WHERE publication_job_id=? AND business_id=? LIMIT 1",
            (job_id, current.business_id),
        ).fetchone()
        now = _iso_now()
        created_at = now if previous is None else str(_value(previous, "created_at", 11))
        self._conn.execute(
            """
            INSERT INTO creative_variant_bindings(
                publication_job_id, business_id, experiment_id, variant_id,
                angle_id, country_code, copy_digest, source_job_id, render_pack_id, status,
                created_by_member_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, '', '', 'selected', ?, ?, ?)
            ON CONFLICT(publication_job_id, business_id) DO UPDATE SET
                experiment_id=excluded.experiment_id,
                variant_id=excluded.variant_id,
                angle_id=excluded.angle_id,
                country_code=excluded.country_code,
                copy_digest=excluded.copy_digest,
                source_job_id='',
                render_pack_id='',
                status='selected',
                created_by_member_id=excluded.created_by_member_id,
                updated_at=excluded.updated_at
            """,
            (
                job_id,
                current.business_id,
                probe.experiment_id,
                probe.variant_id,
                probe.angle_id,
                probe.country_code,
                probe.copy_digest,
                current.membership_id,
                created_at,
                now,
            ),
        )
        return self.get(actor=current, publication_job_id=job_id)

    def remember_progress(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
        variant_id: str,
        source_job_id: str,
        render_pack_id: str = "",
        status: CreativeVariantBindingStatus,
    ) -> CreativeVariantBinding:
        current = self._actor(actor)
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        existing = self.get_current(actor=current, publication_job_id=job_id)
        if existing.variant_id != str(variant_id or "").strip():
            raise ValueError("creative_variant_binding_changed")
        source = str(source_job_id or "").strip()
        render = str(render_pack_id or "").strip()
        if existing.source_job_id and source != existing.source_job_id:
            raise ValueError("creative_source_job_binding_changed")
        if existing.render_pack_id and render and render != existing.render_pack_id:
            raise ValueError("creative_render_pack_binding_changed")
        if (
            existing.status == CreativeVariantBindingStatus.ATTACHED
            and CreativeVariantBindingStatus(status) != CreativeVariantBindingStatus.ATTACHED
        ):
            raise ValueError("creative_variant_binding_already_attached")
        probe = replace(
            existing,
            source_job_id=source,
            render_pack_id=render,
            status=CreativeVariantBindingStatus(status),
        ).normalized()
        cursor = self._conn.execute(
            """
            UPDATE creative_variant_bindings
            SET source_job_id=?, render_pack_id=?, status=?, updated_at=?
            WHERE publication_job_id=? AND business_id=? AND variant_id=?
            """,
            (
                probe.source_job_id,
                probe.render_pack_id,
                probe.status.value,
                _iso_now(),
                job_id,
                current.business_id,
                existing.variant_id,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise RuntimeError("creative_variant_binding_update_lost")
        return self.get_current(actor=current, publication_job_id=job_id)

    def get(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
    ) -> CreativeVariantBinding:
        current = self._actor(actor)
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        row = self._conn.execute(
            _SELECT + " WHERE publication_job_id=? AND business_id=? LIMIT 1",
            (job_id, current.business_id),
        ).fetchone()
        if row is None:
            raise LookupError("creative variant binding was not found")
        return _binding(row)

    def get_current(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
    ) -> CreativeVariantBinding:
        current = self._actor(actor)
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        binding = self.get(actor=current, publication_job_id=job_id)
        copy_digest = self._job_copy_digest(
            business_id=current.business_id,
            publication_job_id=job_id,
        )
        if binding.copy_digest != copy_digest:
            raise ValueError("creative_copy_binding_changed")
        return binding

    def list_observable(self, *, actor: TenantContext) -> tuple[ObservableCreativeVariant, ...]:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_promotion_analytics()
        rows = self._conn.execute(
            """
            SELECT b.publication_job_id, b.business_id, b.experiment_id,
                   b.variant_id, b.angle_id, b.country_code, b.copy_digest,
                   b.source_job_id, b.render_pack_id, b.status, b.created_by_member_id,
                   b.created_at, b.updated_at,
                   COALESCE(j.external_ad_id, '') AS external_ad_id,
                   j.promotion_campaign_id, j.title, j.text,
                   COALESCE(a.source, '') AS asset_source
            FROM creative_variant_bindings b
            JOIN ad_publication_jobs j
              ON j.id=b.publication_job_id AND j.business_id=b.business_id
            LEFT JOIN ad_publication_assets a
              ON a.publication_job_id=b.publication_job_id AND a.business_id=b.business_id
            WHERE b.business_id=?
            ORDER BY b.updated_at DESC, b.publication_job_id
            """,
            (current.business_id,),
        ).fetchall()
        observed: list[ObservableCreativeVariant] = []
        for row in rows:
            binding = _binding(row)
            current_title = str(_value(row, "title", 15) or "")
            current_text = str(_value(row, "text", 16) or "")
            current_digest = hashlib.sha256(
                f"{current_title}\n{current_text}".encode("utf-8")
            ).hexdigest()
            if (
                binding.status != CreativeVariantBindingStatus.ATTACHED
                or binding.copy_digest != current_digest
                or str(_value(row, "asset_source", 17)) != "generated"
            ):
                continue
            observed.append(
                ObservableCreativeVariant(
                    binding=binding,
                    external_ad_id=str(_value(row, "external_ad_id", 13)),
                    promotion_campaign_id=str(_value(row, "promotion_campaign_id", 14)),
                )
            )
        return tuple(observed)


__all__ = ["CreativeVariantBindingRepository"]
