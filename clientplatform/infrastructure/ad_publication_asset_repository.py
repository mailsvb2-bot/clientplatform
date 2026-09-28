from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.ad_publication_assets import (
    AdPublicationAsset,
    AdPublicationAssetKind,
    AdPublicationAssetSource,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


_SAFE_ERROR_RE = re.compile(r"^[a-z0-9_.:-]{1,120}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UPLOAD_STALE_SECONDS = 300


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
    SELECT publication_job_id, business_id, kind, source,
           content_type, original_name, sha256, size_bytes, duration_seconds,
           provider_image_hash, provider_video_id, provider_creative_id,
           provider_error_code, provider_upload_status,
           created_by_member_id, created_at, updated_at
    FROM ad_publication_assets
"""


def _asset(row: Any) -> AdPublicationAsset:
    duration_raw = _value(row, "duration_seconds", 8)
    return AdPublicationAsset(
        publication_job_id=str(_value(row, "publication_job_id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        kind=AdPublicationAssetKind(str(_value(row, "kind", 2))),
        source=AdPublicationAssetSource(str(_value(row, "source", 3))),
        content_type=str(_value(row, "content_type", 4)),
        original_name=str(_value(row, "original_name", 5)),
        sha256=str(_value(row, "sha256", 6)),
        size_bytes=int(_value(row, "size_bytes", 7)),
        duration_seconds=None if duration_raw is None else int(duration_raw),
        provider_image_hash=_optional(row, "provider_image_hash", 9),
        provider_video_id=_optional(row, "provider_video_id", 10),
        provider_creative_id=_optional(row, "provider_creative_id", 11),
        provider_error_code=_optional(row, "provider_error_code", 12),
        provider_upload_status=str(_value(row, "provider_upload_status", 13)),
        created_by_member_id=str(_value(row, "created_by_member_id", 14)),
        created_at=str(_value(row, "created_at", 15)),
        updated_at=str(_value(row, "updated_at", 16)),
    )


@dataclass(frozen=True, slots=True)
class AdMediaUploadReservation:
    state: str
    claim_token: str = ""
    asset: AdPublicationAsset | None = None

    @property
    def claimed(self) -> bool:
        return self.state == "claimed" and bool(self.claim_token)


class AdPublicationAssetRepository:
    """Provider-reference media receipt for one tenant-scoped advertising draft."""

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
        if status not in {"draft", "failed", "submitted"}:
            raise ValueError("advertising publication media can no longer be changed")

    @staticmethod
    def _metadata(
        *,
        kind: AdPublicationAssetKind,
        source: AdPublicationAssetSource,
        content_type: str,
        original_name: str,
        sha256: str,
        size_bytes: int,
        duration_seconds: int | None,
    ) -> tuple[str, str, str, str, str, int, int | None]:
        media_kind = AdPublicationAssetKind(kind).value
        media_source = AdPublicationAssetSource(source).value
        mime = str(content_type or "").strip().lower()
        name = " ".join(str(original_name or "media").split())[:255]
        digest = str(sha256 or "").strip().lower()
        size = int(size_bytes)
        duration = None if duration_seconds is None else int(duration_seconds)
        if not mime or len(mime) > 120 or "\x00" in mime:
            raise ValueError("advertising asset content type is invalid")
        if not name:
            raise ValueError("advertising asset original name is invalid")
        if _SHA256_RE.fullmatch(digest) is None:
            raise ValueError("advertising asset SHA-256 is invalid")
        if size <= 0 or size > 100_000_000:
            raise ValueError("advertising asset size is invalid")
        if duration is not None and (duration <= 0 or duration > 3600):
            raise ValueError("advertising asset duration is invalid")
        return media_kind, media_source, mime, name, digest, size, duration

    @staticmethod
    def _upload_is_stale(asset: AdPublicationAsset) -> bool:
        if asset.provider_upload_status != "uploading":
            return False
        try:
            updated = datetime.fromisoformat(asset.updated_at)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        age = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
        return age >= _UPLOAD_STALE_SECONDS

    def _mark_stale_upload_ambiguous(
        self,
        *,
        business_id: str,
        publication_job_id: str,
        observed: AdPublicationAsset,
    ) -> bool:
        if not self._upload_is_stale(observed):
            return False
        cursor = self._conn.execute(
            """
            UPDATE ad_publication_assets
            SET provider_upload_status='ambiguous',
                provider_upload_claim_token='',
                provider_error_code='ad_media_upload_stale_ambiguous',
                updated_at=?
            WHERE publication_job_id=? AND business_id=?
              AND provider_upload_status='uploading'
              AND updated_at=?
            """,
            (
                _iso_now(),
                publication_job_id,
                business_id,
                observed.updated_at,
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1

    def begin_upload(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
        kind: AdPublicationAssetKind,
        source: AdPublicationAssetSource,
        content_type: str,
        original_name: str,
        sha256: str,
        size_bytes: int,
        duration_seconds: int | None,
    ) -> AdMediaUploadReservation:
        current = self._actor(actor)
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        self._assert_editable_job(business_id=current.business_id, job_id=job_id)
        media_kind, media_source, mime, name, digest, size, duration = self._metadata(
            kind=kind,
            source=source,
            content_type=content_type,
            original_name=original_name,
            sha256=sha256,
            size_bytes=size_bytes,
            duration_seconds=duration_seconds,
        )
        row = self._conn.execute(
            _SELECT + " WHERE publication_job_id=? AND business_id=? LIMIT 1",
            (job_id, current.business_id),
        ).fetchone()
        if row is not None:
            observed = _asset(row)
            stale_upload = self._mark_stale_upload_ambiguous(
                business_id=current.business_id,
                publication_job_id=job_id,
                observed=observed,
            )
            if stale_upload:
                observed_row = self._conn.execute(
                    _SELECT + " WHERE publication_job_id=? AND business_id=? LIMIT 1",
                    (job_id, current.business_id),
                ).fetchone()
                if observed_row is None:
                    raise RuntimeError("advertising media upload receipt disappeared")
                observed = _asset(observed_row)
                row = observed_row
            if observed.sha256 == digest and observed.kind.value == media_kind:
                if observed.provider_upload_status == "ready":
                    return AdMediaUploadReservation("ready", asset=observed)
                if observed.provider_upload_status in {"uploading", "ambiguous"}:
                    return AdMediaUploadReservation(
                        observed.provider_upload_status,
                        asset=observed,
                    )

        claim_token = str(uuid4())
        now = _iso_now()
        if row is None:
            cursor = self._conn.execute(
                """
                INSERT INTO ad_publication_assets(
                    publication_job_id, business_id, kind, source,
                    content_type, original_name, sha256, size_bytes, duration_seconds,
                    provider_image_hash, provider_video_id, provider_creative_id,
                    provider_error_code, provider_upload_status,
                    provider_upload_claim_token, created_by_member_id,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,'uploading',?,?,?,?)
                ON CONFLICT(publication_job_id, business_id) DO NOTHING
                """,
                (
                    job_id,
                    current.business_id,
                    media_kind,
                    media_source,
                    mime,
                    name,
                    digest,
                    size,
                    duration,
                    claim_token,
                    current.membership_id,
                    now,
                    now,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) == 1:
                return AdMediaUploadReservation("claimed", claim_token=claim_token)
        else:
            cursor = self._conn.execute(
                """
                UPDATE ad_publication_assets
                SET kind=?, source=?, content_type=?, original_name=?, sha256=?,
                    size_bytes=?, duration_seconds=?, provider_image_hash=NULL,
                    provider_video_id=NULL, provider_creative_id=NULL,
                    provider_error_code=NULL, provider_upload_status='uploading',
                    provider_upload_claim_token=?, created_by_member_id=?, updated_at=?
                WHERE publication_job_id=? AND business_id=?
                  AND provider_upload_status IN ('ready','failed','ambiguous')
                """,
                (
                    media_kind,
                    media_source,
                    mime,
                    name,
                    digest,
                    size,
                    duration,
                    claim_token,
                    current.membership_id,
                    now,
                    job_id,
                    current.business_id,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) == 1:
                return AdMediaUploadReservation("claimed", claim_token=claim_token)

        observed_row = self._conn.execute(
            _SELECT + " WHERE publication_job_id=? AND business_id=? LIMIT 1",
            (job_id, current.business_id),
        ).fetchone()
        if observed_row is None:
            raise RuntimeError("advertising media upload reservation disappeared")
        observed = _asset(observed_row)
        if observed.sha256 == digest and observed.provider_upload_status == "ready":
            return AdMediaUploadReservation("ready", asset=observed)
        return AdMediaUploadReservation(
            observed.provider_upload_status,
            asset=observed,
        )

    def complete_upload(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
        claim_token: str,
        provider_image_hash: str | None = None,
        provider_video_id: str | None = None,
    ) -> AdPublicationAsset:
        current = self._actor(actor)
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        token = str(claim_token or "").strip()
        image_hash = None if provider_image_hash is None else str(provider_image_hash).strip()
        video_id = None if provider_video_id is None else str(provider_video_id).strip()
        if not token:
            raise ValueError("advertising media upload claim is required")
        if bool(image_hash) == bool(video_id):
            raise ValueError("exactly one advertising provider reference is required")
        cursor = self._conn.execute(
            """
            UPDATE ad_publication_assets
            SET provider_image_hash=?, provider_video_id=?,
                provider_upload_status='ready', provider_upload_claim_token='',
                provider_error_code=NULL, updated_at=?
            WHERE publication_job_id=? AND business_id=?
              AND provider_upload_status='uploading'
              AND provider_upload_claim_token=?
            """,
            (
                image_hash,
                video_id,
                _iso_now(),
                job_id,
                current.business_id,
                token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise RuntimeError("advertising media upload claim was lost")
        return self.get(actor=current, publication_job_id=job_id)

    def mark_upload_ambiguous(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
        claim_token: str,
        error_code: str,
    ) -> None:
        current = self._actor(actor)
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        token = str(claim_token or "").strip()
        safe_error = str(error_code or "").strip().lower()
        if not token:
            return
        if _SAFE_ERROR_RE.fullmatch(safe_error) is None:
            safe_error = "ad_media_upload_ambiguous"
        self._conn.execute(
            """
            UPDATE ad_publication_assets
            SET provider_upload_status='ambiguous', provider_upload_claim_token='',
                provider_error_code=?, updated_at=?
            WHERE publication_job_id=? AND business_id=?
              AND provider_upload_status='uploading'
              AND provider_upload_claim_token=?
            """,
            (safe_error, _iso_now(), job_id, current.business_id, token),
        )

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
    ) -> bool:
        current = self._actor(actor)
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        self._assert_editable_job(business_id=current.business_id, job_id=job_id)
        cursor = self._conn.execute(
            "DELETE FROM ad_publication_assets WHERE publication_job_id=? AND business_id=?",
            (job_id, current.business_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1

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
        if current is None or current.provider_upload_status != "ready":
            return current
        image_hash = provider_image_hash or current.provider_image_hash
        video_id = provider_video_id or current.provider_video_id
        creative_id = provider_creative_id or current.provider_creative_id
        self._conn.execute(
            """
            UPDATE ad_publication_assets
            SET provider_image_hash=?, provider_video_id=?, provider_creative_id=?,
                provider_error_code=NULL, updated_at=?
            WHERE publication_job_id=? AND business_id=?
              AND provider_upload_status='ready'
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
        if _SAFE_ERROR_RE.fullmatch(safe_error) is None:
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


__all__ = ["AdMediaUploadReservation", "AdPublicationAssetRepository"]
