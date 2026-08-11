from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db


@dataclass(frozen=True, slots=True)
class CustomizedAdCopy:
    publication_job_id: str
    title: str
    text: str


def _normalize_title(value: object) -> str:
    normalized = " ".join(str(value or "").split()).strip()
    if not normalized or len(normalized) > 56:
        raise ValueError("advertising title must contain from 1 to 56 characters")
    if any(len(word) > 22 for word in normalized.split()):
        raise ValueError("advertising title contains a word longer than 22 characters")
    return normalized


def _normalize_text(value: object) -> str:
    normalized = " ".join(str(value or "").split()).strip()
    if not normalized or len(normalized) > 81:
        raise ValueError("advertising text must contain from 1 to 81 characters")
    if any(len(word) > 23 for word in normalized.split()):
        raise ValueError("advertising text contains a word longer than 23 characters")
    return normalized


def update_ad_publication_copy(
    *,
    actor: TenantContext,
    publication_job_id: str,
    title: str,
    text: str,
) -> CustomizedAdCopy:
    job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
    normalized_title = _normalize_title(title)
    normalized_text = _normalize_text(text)
    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        cursor = conn.execute(
            """
            UPDATE ad_publication_jobs
            SET title=?, text=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND business_id=? AND status IN ('draft', 'failed')
            """,
            (normalized_title, normalized_text, job_id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            row: Any = conn.execute(
                "SELECT status FROM ad_publication_jobs WHERE id=? AND business_id=? LIMIT 1",
                (job_id, current.business_id),
            ).fetchone()
            if row is None:
                raise ValueError("advertising publication draft was not found")
            raise ValueError("advertising publication draft can no longer be edited")
    return CustomizedAdCopy(
        publication_job_id=job_id,
        title=normalized_title,
        text=normalized_text,
    )


__all__ = ["CustomizedAdCopy", "update_ad_publication_copy"]
