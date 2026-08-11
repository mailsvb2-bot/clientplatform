from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.ad_publication_assets import (
    AdPublicationAsset,
    AdPublicationAssetKind,
    AdPublicationAssetSource,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _optional(row: Any, key: str, position: int) -> str | None:
    value = _value(row, key, position)
    return None if value is None else str(value)


_SELECT = """
    SELECT publication_job_id, business_id, kind, source, storage_path,
           content_type, original_name, sha256, size_bytes, duration_seconds,
           provider_image_hash, provider_video_id, provider_creative_id,
           provider_error_code, created_by_member_id, created_at, updated_at
    FROM ad_publication_assets
"""


def _asset(row: Any) -> AdPublicationAsset:
    duration_raw = _value(row, "duration_seconds", 9)
    return AdPublicationAsset(
        publication_job_id=str(_value(row, "publication_job_id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        kind=AdPublicationAssetKind(str(_value(row, "kind", 2))),
        source=AdPublicationAssetSource(str(_value(row, "source", 3))),
        storage_path=str(_value(row, "storage_path", 4)),
        content_type=str(_value(row, "content_type", 5)),
        original_name=str(_value(row, "original_name", 6)),
        sha256=str(_value(row, "sha256", 7)),
        size_bytes=int(_value(row, "size_bytes", 8)),
        duration_seconds=None if duration_raw is None else int(duration_raw),
        provider_image_hash=_optional(row, "provider_image_hash", 10),
        provider_video_id=_optional(row, "provider_video_id", 11),
        provider_creative_id=_optional(row, "provider_creative_id", 12),
        provider_error_code=_optional(row, "provider_error_code", 13),
        created_by_member_id=str(_value(row, "created_by_member_id", 14)),
        created_at=str(_value(row, "created_at", 15)),
        updated_at=str(_value(row, "updated_at", 16)),
    )


class AdPublicationAssetRepository:
    """One replaceable, tenant-scoped media asset for an advertising draft."""

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

    def _assert_editable_job(self, *, business_id: str, job_id: str) -> None:
        row = self._conn.execute(
            """
            SELECT status FROM ad_publication_jobs
            WHERE id=? AND business_id=? LIMIT 1
            """,
            (job_id, business_id),
        ).fetchone()
        if row is None:
            raise ValueError("advertising publication draft was not found")
        status = str(_value(row, "status", 0))
        # `submitted` means the provider-side object is still a DRAFT. The
        # goal-first UI may safely replace/remove media before spend consent;
        # stale customization callbacks are separately guarded by the FSM layer.
        if status not in {"draft", "failed", "submitted"}:
            raise ValueError("advertising publication media can no longer be changed")

    def replace(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
        kind: AdPublicationAssetKind,
        source: AdPublicationAssetSource,
        storage_path: str,
        content_type: str,
        original_name: str,
        sha256: str,
        size_bytes: int,
        duration_seconds: int | None,
    ) -> tuple[AdPublicationAsset, str | None]:
        current = self._actor(actor)
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        self._assert_editable_job(business_id=current.business_id, job_id=job_id)
        previous = self._conn.execute(
            _SELECT + " WHERE publication_job_id=? AND business_id=? LIMIT 1",
            (job_id, current.business_id),
        ).fetchone()
        previous_path = None if previous is None else str(_value(previous, "storage_path", 4))
        now = _iso_now()
        created_at = now if previous is None else str(_value(previous, "created_at", 15))
        self._conn.execute(
            """
            INSERT INTO ad_publication_assets(
                publication_job_id, business_id, kind, source, storage_path,
                content_type, original_name, sha256, size_bytes, duration_seconds,
                provider_image_hash, provider_video_id, provider_creative_id,
                provider_error_code, created_by_member_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?)
            ON CONFLICT(publication_job_id, business_id) DO UPDATE SET
                kind=excluded.kind,
                source=excluded.source,
                storage_path=excluded.storage_path,
                content_type=excluded.content_type,
                original_name=excluded.original_name,
                sha256=excluded.sha256,
                size_bytes=excluded.size_bytes,
                duration_seconds=excluded.duration_seconds,
                provider_image_hash=NULL,
                provider_video_id=NULL,
                provider_creative_id=NULL,
                provider_error_code=NULL,
                created_by_member_id=excluded.created_by_member_id,
                updated_at=excluded.updated_at
            """,
            (
                job_id,
                current.business_id,
                AdPublicationAssetKind(kind).value,
                AdPublicationAssetSource(source).value,
                storage_path,
                content_type,
                original_name,
                sha256,
                int(size_bytes),
                duration_seconds,
                current.membership_id,
                created_at,
                now,
            ),
        )
        return self.get(actor=current, publication_job_id=job_id), previous_path

    def get(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
    ) -> AdPublicationAsset:
        current = self._actor(actor)
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        row = self._conn.execute(
            _SELECT + " WHERE publication_job_id=? AND business_id=? LIMIT 1",
            (job_id, current.business_id),
        ).fetchone()
        if row is None:
            raise LookupError("advertising publication media was not found")
        return _asset(row)

    def get_for_worker(
        self,
        *,
        business_id: str,
        publication_job_id: str,
    ) -> AdPublicationAsset | None:
        business = normalize_uuid(business_id, field_name="business_id")
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        row = self._conn.execute(
            _SELECT + " WHERE publication_job_id=? AND business_id=? LIMIT 1",
            (job_id, business),
        ).fetchone()
        return None if row is None else _asset(row)

    def remove(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
    ) -> str | None:
        current = self._actor(actor)
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        self._assert_editable_job(business_id=current.business_id, job_id=job_id)
        row = self._conn.execute(
            """
            SELECT storage_path FROM ad_publication_assets
            WHERE publication_job_id=? AND business_id=? LIMIT 1
            """,
            (job_id, current.business_id),
        ).fetchone()
        if row is None:
            return None
        path = str(_value(row, "storage_path", 0))
        self._conn.execute(
            "DELETE FROM ad_publication_assets WHERE publication_job_id=? AND business_id=?",
            (job_id, current.business_id),
        )
        return path

    def remember_provider_ids(
        self,
        *,
        business_id: str,
        publication_job_id: str,
        provider_image_hash: str | None = None,
        provider_video_id: str | None = None,
        provider_creative_id: str | None = None,
    ) -> AdPublicationAsset | None:
        business = normalize_uuid(business_id, field_name="business_id")
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        current = self.get_for_worker(
            business_id=business,
            publication_job_id=job_id,
        )
        if current is None:
            return None
        image_hash = provider_image_hash or current.provider_image_hash
        video_id = provider_video_id or current.provider_video_id
        creative_id = provider_creative_id or current.provider_creative_id
        self._conn.execute(
            """
            UPDATE ad_publication_assets
            SET provider_image_hash=?, provider_video_id=?, provider_creative_id=?,
                provider_error_code=NULL, updated_at=?
            WHERE publication_job_id=? AND business_id=?
            """,
            (image_hash, video_id, creative_id, _iso_now(), job_id, business),
        )
        return self.get_for_worker(
            business_id=business,
            publication_job_id=job_id,
        )

    def remember_provider_error(
        self,
        *,
        business_id: str,
        publication_job_id: str,
        error_code: str,
    ) -> AdPublicationAsset | None:
        business = normalize_uuid(business_id, field_name="business_id")
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        safe_error = str(error_code or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_.:-]{1,120}", safe_error):
            raise ValueError("provider media error code is invalid")
        self._conn.execute(
            """
            UPDATE ad_publication_assets
            SET provider_error_code=?, updated_at=?
            WHERE publication_job_id=? AND business_id=?
            """,
            (safe_error, _iso_now(), job_id, business),
        )
        return self.get_for_worker(
            business_id=business,
            publication_job_id=job_id,
        )


__all__ = ["AdPublicationAssetRepository"]
