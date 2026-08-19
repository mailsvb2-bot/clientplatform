from __future__ import annotations

from clientplatform.domain.sales import SalesLead, SalesLeadStage
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.sales_repository import SalesRepository
from services.db import get_db


def transition_sales_lead(
    *,
    actor: TenantContext,
    lead_id: str,
    stage: SalesLeadStage | str,
    reason: str | None = None,
) -> SalesLead:
    with get_db() as conn:
        return SalesRepository(conn).set_stage(
            actor=actor,
            lead_id=lead_id,
            stage=stage,
            reason=reason,
        )


def assign_sales_lead(
    *,
    actor: TenantContext,
    lead_id: str,
    member_id: str,
) -> SalesLead:
    with get_db() as conn:
        return SalesRepository(conn).assign_member(
            actor=actor,
            lead_id=lead_id,
            member_id=member_id,
        )


def unassign_sales_lead(*, actor: TenantContext, lead_id: str) -> SalesLead:
    with get_db() as conn:
        return SalesRepository(conn).unassign_member(actor=actor, lead_id=lead_id)


def set_sales_next_action(
    *,
    actor: TenantContext,
    lead_id: str,
    next_action: str | None,
    due_at: str | None = None,
) -> SalesLead:
    with get_db() as conn:
        return SalesRepository(conn).set_next_action(
            actor=actor,
            lead_id=lead_id,
            next_action=next_action,
            due_at=due_at,
        )


def clear_sales_next_action(*, actor: TenantContext, lead_id: str) -> SalesLead:
    with get_db() as conn:
        return SalesRepository(conn).clear_next_action(actor=actor, lead_id=lead_id)


def add_sales_note(
    *,
    actor: TenantContext,
    lead_id: str,
    note: str,
    dedupe_key: str,
) -> bool:
    with get_db() as conn:
        return SalesRepository(conn).add_note(
            actor=actor,
            lead_id=lead_id,
            note=note,
            dedupe_key=dedupe_key,
        )
