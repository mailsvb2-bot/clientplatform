from __future__ import annotations

"""Transport-neutral sales workspace over the canonical sales use cases.

Adapters for Telegram, VK, MAX and future channels should call this module
instead of re-implementing sales mutations. The canonical invariants remain in
``sales_operations`` and the repositories they invoke.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from clientplatform.application.sales_followups import (
    cancel_sales_followup,
    schedule_sales_followup,
    suppress_sales_followup_channel,
)
from clientplatform.application.sales_handoff import (
    claim_sales_handoff,
    resolve_sales_handoff,
)
from clientplatform.application.sales_operations import (
    add_sales_note,
    assign_sales_lead,
    clear_sales_next_action,
    set_sales_next_action,
    transition_sales_lead,
    unassign_sales_lead,
)
from clientplatform.application.sales_ui import (
    count_sales_handoff_work,
    list_commercial_ladder_steps,
    list_commercial_ladders,
    list_recent_closed_sales_work,
    list_sales_handoff_work,
    list_sales_work,
)
from clientplatform.domain.sales import SalesLead, SalesLeadStage
from clientplatform.domain.tenancy import TenantContext


@dataclass(frozen=True, slots=True)
class SalesWorkspaceSnapshot:
    """Business-level sales state shared by every staff transport adapter."""

    open_work: tuple[dict[str, Any], ...]
    handoff_work: tuple[dict[str, Any], ...]
    recent_closed: tuple[dict[str, Any], ...]
    handoff_count: int


def sales_workspace_snapshot(
    *,
    actor: TenantContext,
    limit: int = 12,
) -> SalesWorkspaceSnapshot:
    return SalesWorkspaceSnapshot(
        open_work=tuple(list_sales_work(actor=actor, limit=limit)),
        handoff_work=tuple(list_sales_handoff_work(actor=actor, limit=limit)),
        recent_closed=tuple(list_recent_closed_sales_work(actor=actor, limit=limit)),
        handoff_count=count_sales_handoff_work(actor=actor),
    )


def list_sales_workspace(
    *,
    actor: TenantContext,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Return the active tenant-scoped sales backlog for any staff adapter."""

    return list_sales_work(actor=actor, limit=limit)


def list_sales_workspace_handoffs(
    *,
    actor: TenantContext,
    limit: int = 12,
) -> list[dict[str, Any]]:
    return list_sales_handoff_work(actor=actor, limit=limit)


def list_sales_workspace_recent_closed(
    *,
    actor: TenantContext,
    limit: int = 12,
) -> list[dict[str, Any]]:
    return list_recent_closed_sales_work(actor=actor, limit=limit)


def list_sales_workspace_ladders(*, actor: TenantContext) -> list[dict[str, Any]]:
    return list_commercial_ladders(actor=actor)


def list_sales_workspace_ladder_steps(
    *,
    actor: TenantContext,
    ladder_id: str,
) -> list[dict[str, Any]]:
    return list_commercial_ladder_steps(actor=actor, ladder_id=ladder_id)


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


def claim_sales_workspace_handoff(*, actor: TenantContext, handoff_id: str) -> dict[str, object]:
    return claim_sales_handoff(actor=actor, handoff_id=handoff_id)


def resolve_sales_workspace_handoff(*, actor: TenantContext, handoff_id: str) -> dict[str, object]:
    return resolve_sales_handoff(actor=actor, handoff_id=handoff_id)


def schedule_sales_workspace_followup(
    *,
    actor: TenantContext,
    lead_id: str,
    message_text: str,
    hours_from_now: int,
    interaction_key: str,
) -> object:
    if hours_from_now not in {1, 24, 72}:
        raise ValueError("follow-up delay must be 1, 24 or 72 hours")
    due_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=hours_from_now)
    return schedule_sales_followup(
        actor=actor,
        lead_id=lead_id,
        message_text=message_text,
        scheduled_at=due_at,
        request_key=interaction_key,
    )


def cancel_sales_workspace_followup(*, actor: TenantContext, lead_id: str) -> int:
    return cancel_sales_followup(actor=actor, lead_id=lead_id)


def suppress_sales_workspace_followup(*, actor: TenantContext, lead_id: str) -> int:
    return suppress_sales_followup_channel(actor=actor, lead_id=lead_id, reason="opt_out")


__all__ = [
    "SalesWorkspaceSnapshot",
    "add_sales_workspace_note",
    "assign_sales_workspace_to_actor",
    "cancel_sales_workspace_followup",
    "clear_sales_workspace_next_action",
    "claim_sales_workspace_handoff",
    "get_sales_workspace_item",
    "list_sales_workspace",
    "list_sales_workspace_handoffs",
    "list_sales_workspace_ladder_steps",
    "list_sales_workspace_ladders",
    "list_sales_workspace_recent_closed",
    "reopen_sales_workspace",
    "resolve_sales_workspace_handoff",
    "sales_workspace_snapshot",
    "schedule_sales_workspace_followup",
    "set_sales_workspace_next_action",
    "suppress_sales_workspace_followup",
    "transition_sales_workspace",
    "unassign_sales_workspace",
]
