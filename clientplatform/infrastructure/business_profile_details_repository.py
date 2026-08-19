from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.activity import ActivityNotFound
from clientplatform.domain.business_profile import (
    BusinessProfileDetails,
    business_profile_details_from_json,
    business_profile_details_to_json,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class StoredBusinessProfileDetails:
    details: BusinessProfileDetails
    confirmed_at: str | None

    @property
    def confirmed(self) -> bool:
        return self.confirmed_at is not None


class BusinessProfileDetailsRepository:
    """Structured BusinessProfile fields stored in the canonical profile row."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _current_actor(self, actor: TenantContext) -> TenantContext:
        return self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )

    def get(self, *, actor: TenantContext) -> StoredBusinessProfileDetails:
        current = self._current_actor(actor)
        row = self._conn.execute(
            """
            SELECT profile_details_json, profile_confirmed_at
            FROM business_profiles
            WHERE business_id=?
            LIMIT 1
            """,
            (current.business_id,),
        ).fetchone()
        if row is None:
            raise ActivityNotFound("business activity profile was not found")
        if hasattr(row, "keys"):
            raw_details = row["profile_details_json"]
            confirmed_at = row["profile_confirmed_at"]
        else:
            raw_details, confirmed_at = row[0], row[1]
        return StoredBusinessProfileDetails(
            details=business_profile_details_from_json(raw_details),
            confirmed_at=None if confirmed_at is None else str(confirmed_at),
        )

    def save(
        self,
        *,
        actor: TenantContext,
        details: BusinessProfileDetails,
        reset_confirmation: bool = True,
        now: str | None = None,
    ) -> StoredBusinessProfileDetails:
        current = self._current_actor(actor)
        current.assert_can_manage_business()
        timestamp = str(now or _utc_now())
        if reset_confirmation:
            cursor = self._conn.execute(
                """
                UPDATE business_profiles
                SET profile_details_json=?, profile_confirmed_at=NULL, updated_at=?
                WHERE business_id=?
                """,
                (
                    business_profile_details_to_json(details),
                    timestamp,
                    current.business_id,
                ),
            )
        else:
            cursor = self._conn.execute(
                """
                UPDATE business_profiles
                SET profile_details_json=?, updated_at=?
                WHERE business_id=?
                """,
                (
                    business_profile_details_to_json(details),
                    timestamp,
                    current.business_id,
                ),
            )
        if int(cursor.rowcount or 0) != 1:
            raise ActivityNotFound("business activity profile was not found")
        return self.get(actor=current)

    def confirm(
        self,
        *,
        actor: TenantContext,
        now: str | None = None,
    ) -> StoredBusinessProfileDetails:
        current = self._current_actor(actor)
        current.assert_can_manage_business()
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE business_profiles
            SET profile_confirmed_at=COALESCE(profile_confirmed_at, ?), updated_at=?
            WHERE business_id=?
            """,
            (timestamp, timestamp, current.business_id),
        )
        if int(cursor.rowcount or 0) != 1:
            raise ActivityNotFound("business activity profile was not found")
        return self.get(actor=current)


__all__ = [
    "BusinessProfileDetailsRepository",
    "StoredBusinessProfileDetails",
]
