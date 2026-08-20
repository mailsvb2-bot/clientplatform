from __future__ import annotations

from datetime import datetime, timezone

from clientplatform.domain.retention import RetentionCandidate
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.retention_repository import RetentionRepository
from services.db import get_db_ro


def list_retention_candidates(
    *,
    actor: TenantContext,
    now: datetime | None = None,
    limit: int = 100,
) -> list[RetentionCandidate]:
    """Read deterministic U-010 candidates without sending or mutating customer state."""

    stamp = now or datetime.now(timezone.utc)
    with get_db_ro() as conn:
        return RetentionRepository(conn).list_candidates(actor=actor, now=stamp, limit=limit)


__all__ = ["list_retention_candidates"]
