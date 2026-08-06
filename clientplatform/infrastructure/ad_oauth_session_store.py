from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.ad_connections import AdProvider, oauth_state_hash
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class AdOAuthSessionStore:
    """Tenant-scoped lifecycle operations for one-time advertising OAuth states."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def cancel(
        self,
        *,
        actor: TenantContext,
        provider: AdProvider,
        state: str,
        now: datetime | None = None,
    ) -> bool:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_ad_connections()
        timestamp = _iso(now or _utc_now())
        state_digest = oauth_state_hash(state)
        cursor = self._conn.execute(
            """
            UPDATE ad_oauth_sessions
            SET consumed_at=?
            WHERE state_hash=?
              AND business_id=?
              AND user_id=?
              AND membership_id=?
              AND provider=?
              AND consumed_at IS NULL
            """,
            (
                timestamp,
                state_digest,
                current.business_id,
                current.user_id,
                current.membership_id,
                provider.value,
            ),
        )
        cancelled = int(getattr(cursor, "rowcount", 0) or 0) == 1
        if cancelled:
            self._conn.execute(
                """
                INSERT INTO ad_audit_events(
                    id, business_id, actor_member_id, action, subject_type,
                    subject_id, details_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    current.business_id,
                    current.membership_id,
                    "ad_oauth_cancelled",
                    "provider",
                    provider.value,
                    json.dumps(
                        {"reason": "actor_cancelled"},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    timestamp,
                ),
            )
        return cancelled


__all__ = ["AdOAuthSessionStore"]
