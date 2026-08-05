from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from clientplatform.domain.ad_connections import AdProvider, normalize_region_ids
from clientplatform.domain.ad_spend import AdSpendInvariantViolation
from clientplatform.domain.tenancy import PlatformRole, TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


@dataclass(frozen=True, slots=True)
class AdSpendPreparationTarget:
    business_id: str
    publication_job_id: str
    connection_id: str
    external_account_id: str
    external_login: str
    external_campaign_id: str
    region_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "publication_job_id",
            normalize_uuid(
                self.publication_job_id,
                field_name="publication_job_id",
            ),
        )
        object.__setattr__(
            self,
            "connection_id",
            normalize_uuid(self.connection_id, field_name="connection_id"),
        )
        account_id = str(self.external_account_id or "").strip()
        login = " ".join(str(self.external_login or "").split())
        campaign_id = str(self.external_campaign_id or "").strip()
        if not account_id or len(account_id) > 255 or "\x00" in account_id:
            raise AdSpendInvariantViolation("advertising account identity is invalid")
        if not login or len(login) > 255 or "\x00" in login:
            raise AdSpendInvariantViolation("advertising account login is invalid")
        if not campaign_id.isdigit() or int(campaign_id) <= 0:
            raise AdSpendInvariantViolation("advertising campaign identity is invalid")
        object.__setattr__(self, "external_account_id", account_id)
        object.__setattr__(self, "external_login", login)
        object.__setattr__(self, "external_campaign_id", campaign_id)
        object.__setattr__(self, "region_ids", normalize_region_ids(self.region_ids))


class AdSpendPreparationRepository:
    """Read-only tenant boundary for preparing a spend authorization."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def load_submitted_target(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
    ) -> tuple[TenantContext, AdSpendPreparationTarget]:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        if current.role != PlatformRole.OWNER:
            raise AdSpendInvariantViolation(
                "advertising spend preparation requires business owner role"
            )
        job_id = normalize_uuid(
            publication_job_id,
            field_name="publication_job_id",
        )
        row = self._conn.execute(
            """
            SELECT j.business_id,
                   j.id AS publication_job_id,
                   j.connection_id,
                   c.external_account_id,
                   c.external_login,
                   j.external_campaign_id,
                   j.region_ids_json,
                   j.status AS job_status,
                   c.status AS connection_status,
                   c.provider
            FROM ad_publication_jobs AS j
            JOIN ad_connections AS c
              ON c.id=j.connection_id AND c.business_id=j.business_id
            WHERE j.id=? AND j.business_id=?
            LIMIT 1
            """,
            (job_id, current.business_id),
        ).fetchone()
        if row is None:
            raise AdSpendInvariantViolation(
                "submitted advertising draft was not found for business"
            )
        if str(_value(row, "job_status", 7)) != "submitted":
            raise AdSpendInvariantViolation(
                "advertising spend requires a provider-created DRAFT"
            )
        if str(_value(row, "connection_status", 8)) != "active":
            raise AdSpendInvariantViolation("advertising connection is not active")
        if AdProvider(str(_value(row, "provider", 9))) != AdProvider.YANDEX_DIRECT:
            raise AdSpendInvariantViolation("advertising provider is unsupported")
        try:
            regions = tuple(json.loads(str(_value(row, "region_ids_json", 6))))
        except (json.JSONDecodeError, TypeError) as exc:
            raise AdSpendInvariantViolation(
                "stored advertising regions are invalid"
            ) from exc
        return current, AdSpendPreparationTarget(
            business_id=str(_value(row, "business_id", 0)),
            publication_job_id=str(_value(row, "publication_job_id", 1)),
            connection_id=str(_value(row, "connection_id", 2)),
            external_account_id=str(_value(row, "external_account_id", 3)),
            external_login=str(_value(row, "external_login", 4)),
            external_campaign_id=str(_value(row, "external_campaign_id", 5)),
            region_ids=regions,
        )


__all__ = ["AdSpendPreparationRepository", "AdSpendPreparationTarget"]
