from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.ad_connections import AdPublicationJob, AdPublicationStatus
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
    SELECT id, business_id, promotion_campaign_id, connection_id,
           external_campaign_id, external_campaign_name, region_ids_json,
           source_url, title, text, status, idempotency_key,
           external_ad_group_id, external_ad_id, attempts, last_error_code,
           created_by_member_id, created_at, updated_at, submitted_at
    FROM ad_publication_jobs
"""


def _job(row: Any) -> AdPublicationJob:
    return AdPublicationJob(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        promotion_campaign_id=str(_value(row, "promotion_campaign_id", 2)),
        connection_id=str(_value(row, "connection_id", 3)),
        external_campaign_id=str(_value(row, "external_campaign_id", 4)),
        external_campaign_name=str(_value(row, "external_campaign_name", 5)),
        region_ids=tuple(json.loads(str(_value(row, "region_ids_json", 6)))),
        source_url=str(_value(row, "source_url", 7)),
        title=str(_value(row, "title", 8)),
        text=str(_value(row, "text", 9)),
        status=AdPublicationStatus(str(_value(row, "status", 10))),
        idempotency_key=str(_value(row, "idempotency_key", 11)),
        external_ad_group_id=_optional(row, "external_ad_group_id", 12),
        external_ad_id=_optional(row, "external_ad_id", 13),
        attempts=int(_value(row, "attempts", 14) or 0),
        last_error_code=_optional(row, "last_error_code", 15),
        created_by_member_id=str(_value(row, "created_by_member_id", 16)),
        created_at=str(_value(row, "created_at", 17)),
        updated_at=str(_value(row, "updated_at", 18)),
        submitted_at=_optional(row, "submitted_at", 19),
    )


@dataclass(frozen=True, slots=True)
class ExactPublicationClaim:
    job: AdPublicationJob
    lock_token: str


class AdGoalPublicationRepository:
    """Claim exactly the draft the owner confirmed, never another tenant/job."""

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

    def get(self, *, actor: TenantContext, job_id: str) -> AdPublicationJob:
        current = self._actor(actor)
        normalized = normalize_uuid(job_id, field_name="ad_publication_job_id")
        row = self._conn.execute(
            _SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise ValueError("advertising publication draft was not found")
        return _job(row)

    def claim(self, *, actor: TenantContext, job_id: str) -> ExactPublicationClaim | None:
        current = self._actor(actor)
        normalized = normalize_uuid(job_id, field_name="ad_publication_job_id")
        now = _iso_now()
        token = str(uuid4())
        cursor = self._conn.execute(
            """
            UPDATE ad_publication_jobs
            SET status='publishing', attempts=attempts+1, locked_at=?, lock_token=?,
                last_error_code=NULL, updated_at=?
            WHERE id=? AND business_id=?
              AND status IN ('draft', 'queued', 'retry', 'failed')
            """,
            (now, token, now, normalized, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            return None
        row = self._conn.execute(
            _SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise ValueError("advertising publication claim disappeared")
        return ExactPublicationClaim(job=_job(row), lock_token=token)


__all__ = ["AdGoalPublicationRepository", "ExactPublicationClaim"]
