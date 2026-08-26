from __future__ import annotations

from typing import Any

from clientplatform.application.sales_operations import (
    add_sales_note,
    assign_sales_lead,
    clear_sales_next_action,
    set_sales_next_action,
    transition_sales_lead,
    unassign_sales_lead,
)
from clientplatform.domain.sales import SalesLead, SalesLeadStage
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.sales_ui_repository import SalesUiRepository
from services.db import get_db_ro


def list_sales_work(
    *,
    actor: TenantContext,
    limit: int = 12,
) -> list[dict[str, Any]]:
    with get_db_ro() as conn:
        return SalesUiRepository(conn).list_open_work(actor=actor, limit=limit)


def list_recent_closed_sales_work(
    *,
    actor: TenantContext,
    limit: int = 12,
) -> list[dict[str, Any]]:
    with get_db_ro() as conn:
        return SalesUiRepository(conn).list_recent_closed(actor=actor, limit=limit)


def count_sales_handoff_work(*, actor: TenantContext) -> int:
    with get_db_ro() as conn:
        return SalesUiRepository(conn).count_handoff_work(actor=actor)


def list_sales_handoff_work(
    *,
    actor: TenantContext,
    limit: int = 12,
) -> list[dict[str, Any]]:
    with get_db_ro() as conn:
        return SalesUiRepository(conn).list_handoff_work(actor=actor, limit=limit)


def list_commercial_ladders(*, actor: TenantContext) -> list[dict[str, Any]]:
    with get_db_ro() as conn:
        return SalesUiRepository(conn).list_ladders(actor=actor)


def list_commercial_ladder_steps(
    *,
    actor: TenantContext,
    ladder_id: str,
) -> list[dict[str, Any]]:
    with get_db_ro() as conn:
        return SalesUiRepository(conn).list_ladder_steps(
            actor=actor,
            ladder_id=ladder_id,
        )


def assign_sales_work(
    *,
    actor: TenantContext,
    lead_id: str,
    member_id: str,
) -> SalesLead:
    """Assign a lead through the canonical tenant-scoped sales mutation."""

    return assign_sales_lead(actor=actor, lead_id=lead_id, member_id=member_id)


def unassign_sales_work(*, actor: TenantContext, lead_id: str) -> SalesLead:
    """Clear a lead owner through the canonical sales mutation."""

    return unassign_sales_lead(actor=actor, lead_id=lead_id)


def set_sales_work_next_action(
    *,
    actor: TenantContext,
    lead_id: str,
    next_action: str | None,
    due_at: str | None = None,
) -> SalesLead:
    """Persist the durable next action shared by every staff transport."""

    return set_sales_next_action(
        actor=actor,
        lead_id=lead_id,
        next_action=next_action,
        due_at=due_at,
    )


def clear_sales_work_next_action(*, actor: TenantContext, lead_id: str) -> SalesLead:
    """Clear the durable next action shared by every staff transport."""

    return clear_sales_next_action(actor=actor, lead_id=lead_id)


def add_sales_work_note(
    *,
    actor: TenantContext,
    lead_id: str,
    note: str,
    dedupe_key: str,
) -> bool:
    """Append one canonical sales event note without transport-owned state."""

    return add_sales_note(
        actor=actor,
        lead_id=lead_id,
        note=note,
        dedupe_key=dedupe_key,
    )


def transition_sales_work(
    *,
    actor: TenantContext,
    lead_id: str,
    stage: SalesLeadStage | str,
    reason: str | None = None,
) -> SalesLead:
    """Move a lead through the canonical funnel shared by Telegram/VK/MAX."""

    return transition_sales_lead(
        actor=actor,
        lead_id=lead_id,
        stage=stage,
        reason=reason,
    )
