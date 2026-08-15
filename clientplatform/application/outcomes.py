from __future__ import annotations

from datetime import datetime

from clientplatform.domain.outcomes import BusinessOutcomeEvent, OutcomeType
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db_ro


def list_business_outcomes(
    *,
    actor: TenantContext,
    outcome_type: OutcomeType | str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    customer_id: str | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    limit: int = 100,
) -> list[BusinessOutcomeEvent]:
    """Read outcomes only inside the caller's currently active business context."""
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        return OutcomeRepository(conn).list_events(
            business_id=current.business_id,
            outcome_type=outcome_type,
            source_type=source_type,
            source_id=source_id,
            customer_id=customer_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            limit=limit,
        )
