from __future__ import annotations

from clientplatform.domain.sales_state_machine import (
    SalesConversationEvent,
    SalesTransition,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.sales_state_repository import SalesStateRepository
from services.db import get_db


def apply_sales_conversation_event(
    *,
    actor: TenantContext,
    lead_id: str,
    event: SalesConversationEvent | str,
    dedupe_key: str,
    metadata: dict[str, object] | None = None,
) -> tuple[SalesTransition, bool]:
    with get_db() as conn:
        return SalesStateRepository(conn).apply(
            actor=actor,
            lead_id=lead_id,
            event=event,
            dedupe_key=dedupe_key,
            metadata=metadata,
        )
