from __future__ import annotations

"""Transport-neutral sales workspace over the canonical sales use cases.

Adapters for Telegram, VK, MAX and future channels should call this module
instead of re-implementing sales mutations. The canonical invariants remain in
``sales_operations`` and the repositories they invoke.
"""

from typing import Any

from clientplatform.application.sales_operations import (
    add_sales_note,
    assign_sales_lead,
    clear_sales_next_action,
    set_sales_next_action,
    transition_sales_lead,
    unassign_sales_lead,
)
from clientplatform.application.sales_ui import (
    list_recent_closed_sales_work,
    list_sales_work,
)
from clientplatform.domain.sales import SalesLead, SalesLeadStage
from clientplatform.domain.tenancy import TenantContext


def list_sales_workspace(
    *,
    actor: TenantContext,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Return the active tenant-scoped sales backlog for any staff adapter."""

    return list_sales_work(actor=actor, limit=limit)


def get_sales_workspace_item(
    *,
    actor: TenantContext,
    lead_id: str,
) -> dict[str, Any] | None:
    """Resolve one open or recently closed lead without bypassing tenant scope."""

    for item in (
        *list_sales_work(actor=actor, limit=50),
        *list_recent_closed_sales_work(actor=actor, limit=50),
    ):
        if str(item.get("id") or "") == str(lead_id):
            return item
    return None


def assign_sales_workspace_to_actor(
    *,
    actor: TenantContext,
    lead_id: str,
) -> SalesLead:
    """Assign a lead to the current business member, independent of transport."""

    return assign_sales_lead(
        actor=actor,
        lead_id=lead_id,
        member_id=actor.membership_id,
    )


def unassign_sales_workspace(
    *,
    actor: TenantContext,
    lead_id: str,
) -> SalesLead:
    return unassign_sales_lead(actor=actor, lead_id=lead_id)


def set_sales_workspace_next_action(
    *,
    actor: TenantContext,
    lead_id: str,
    next_action: str | None,
    due_at: str | None = None,
) -> SalesLead:
    return set_sales_next_action(
        actor=actor,
        lead_id=lead_id,
        next_action=next_action,
        due_at=due_at,
    )


def clear_sales_workspace_next_action(
    *,
    actor: TenantContext,
    lead_id: str,
) -> SalesLead:
    return clear_sales_next_action(actor=actor, lead_id=lead_id)


def add_sales_workspace_note(
    *,
    actor: TenantContext,
    lead_id: str,
    note: str,
    interaction_key: str,
) -> bool:
    """Persist a note with an adapter-supplied idempotency key."""

    return add_sales_note(
        actor=actor,
        lead_id=lead_id,
        note=note,
        dedupe_key=interaction_key,
    )


def transition_sales_workspace(
    *,
    actor: TenantContext,
    lead_id: str,
    stage: SalesLeadStage | str,
    reason: str | None = None,
) -> SalesLead:
    return transition_sales_lead(
        actor=actor,
        lead_id=lead_id,
        stage=stage,
        reason=reason,
    )


def reopen_sales_workspace(
    *,
    actor: TenantContext,
    lead_id: str,
    reason: str = "reopened_by_member",
) -> SalesLead:
    return transition_sales_lead(
        actor=actor,
        lead_id=lead_id,
        stage=SalesLeadStage.NEW,
        reason=reason,
    )


__all__ = [
    "add_sales_workspace_note",
    "assign_sales_workspace_to_actor",
    "clear_sales_workspace_next_action",
    "get_sales_workspace_item",
    "list_sales_workspace",
    "reopen_sales_workspace",
    "set_sales_workspace_next_action",
    "transition_sales_workspace",
    "unassign_sales_workspace",
]
