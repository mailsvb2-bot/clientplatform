from __future__ import annotations

"""Transactional production proof for the canonical U-008/U-009 sales contour."""

import json
import sys
from pathlib import Path
from typing import Any, Callable, ContextManager
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

from clientplatform.domain.sales import (
    ContactBasis,
    SalesInvariantViolation,
    SalesLeadNotFound,
    SalesLeadStage,
)
from clientplatform.domain.tenancy import PlatformRole
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.connection_repository import ConnectionRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.safe_unified_dispatch_outbox import DispatchOutboxRepository
from clientplatform.infrastructure.sales_followup_repository import SalesFollowupRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.sales_ui_repository import SalesUiRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db, get_db_ro
from services.db.runtime import is_postgres_enabled

CONTRACT_VERSION = "u008-u009-sales-operations-v2"
SUCCESS_MARKER = "CLIENTPLATFORM_SALES_PRODUCTION_SMOKE_OK:"
FAILURE_MARKER = "CLIENTPLATFORM_SALES_PRODUCTION_SMOKE_FAILED:"


class ProductionSalesSmokeError(RuntimeError):
    """Operator-safe failure code for the synthetic sales proof."""


class _RollbackSuccess(RuntimeError):
    def __init__(self, payload: dict[str, Any]):
        super().__init__("rollback_success")
        self.payload = payload


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ProductionSalesSmokeError(code)


def _item_for_lead(items: list[dict[str, Any]], lead_id: str) -> dict[str, Any]:
    for item in items:
        if str(item.get("id") or "") == lead_id:
            return item
    raise ProductionSalesSmokeError("lead_missing_from_projection")


def _residue_counts(conn: Any, business_ids: tuple[str, str]) -> dict[str, int]:
    primary, secondary = business_ids
    queries = {
        "businesses": "SELECT COUNT(*) AS c FROM businesses WHERE id IN (?, ?)",
        "business_members": (
            "SELECT COUNT(*) AS c FROM business_members WHERE business_id IN (?, ?)"
        ),
        "customers": "SELECT COUNT(*) AS c FROM customers WHERE business_id IN (?, ?)",
        "clientplatform_sales_leads": (
            "SELECT COUNT(*) AS c FROM clientplatform_sales_leads "
            "WHERE business_id IN (?, ?)"
        ),
        "clientplatform_sales_events": (
            "SELECT COUNT(*) AS c FROM clientplatform_sales_events "
            "WHERE business_id IN (?, ?)"
        ),
        "customer_identities": "SELECT COUNT(*) AS c FROM customer_identities WHERE business_id IN (?, ?)",
        "business_profiles": "SELECT COUNT(*) AS c FROM business_profiles WHERE business_id IN (?, ?)",
        "connections": "SELECT COUNT(*) AS c FROM connections WHERE business_id IN (?, ?)",
        "clientplatform_sales_followups": (
            "SELECT COUNT(*) AS c FROM clientplatform_sales_followups WHERE business_id IN (?, ?)"
        ),
        "clientplatform_sales_contact_suppressions": (
            "SELECT COUNT(*) AS c FROM clientplatform_sales_contact_suppressions WHERE business_id IN (?, ?)"
        ),
        "provider_dispatch_outbox": (
            "SELECT COUNT(*) AS c FROM provider_dispatch_outbox WHERE business_id IN (?, ?)"
        ),
    }
    counts: dict[str, int] = {}
    for name, query in queries.items():
        row = conn.execute(query, (primary, secondary)).fetchone()
        counts[name] = int(row["c"] if hasattr(row, "keys") else row[0])
    return counts


def _exercise(
    conn: Any,
    *,
    business_ids: tuple[str, str],
    user_ids: tuple[int, int, int],
) -> dict[str, Any]:
    primary_business_id, other_business_id = business_ids
    owner_user_id, manager_user_id, other_owner_user_id = user_ids
    tenancy = TenancyRepository(conn)
    primary_access = tenancy.create_business(
        owner_user_id=owner_user_id,
        name="Synthetic U008 Production Smoke",
        business_id=primary_business_id,
        now="2026-08-20T00:00:00+00:00",
    )
    owner = tenancy.resolve_context(
        user_id=owner_user_id,
        business_id=primary_access.business.id,
    )
    manager = tenancy.grant_member(
        actor=owner,
        user_id=manager_user_id,
        role=PlatformRole.MANAGER,
        now="2026-08-20T00:00:01+00:00",
    )
    other_access = tenancy.create_business(
        owner_user_id=other_owner_user_id,
        name="Synthetic U008 Isolation Smoke",
        business_id=other_business_id,
        now="2026-08-20T00:00:02+00:00",
    )
    other_owner = tenancy.resolve_context(
        user_id=other_owner_user_id,
        business_id=other_access.business.id,
    )
    customer = CustomerRepository(conn).create_customer(
        actor=owner,
        display_name="Synthetic Customer",
        now="2026-08-20T00:00:03+00:00",
    )
    sales = SalesRepository(conn)
    ui = SalesUiRepository(conn)
    lead = sales.create_or_refresh_lead(
        actor=owner,
        opportunity_key="production-smoke:u008",
        customer_id=customer.id,
        source_kind="website",
        source_ref="synthetic-u008-production-smoke",
        contact_basis=ContactBasis.INBOUND,
        now="2026-08-20T00:00:04+00:00",
    )

    checks: dict[str, bool] = {}
    projected = _item_for_lead(ui.list_open_work(actor=owner), lead.id)
    _require(projected["source_kind"] == "website", "owner_projection_source_kind")
    _require(
        projected["source_ref"] == "synthetic-u008-production-smoke",
        "owner_projection_source_ref",
    )
    checks["owner_projection"] = True

    assigned = sales.assign_member(
        actor=owner,
        lead_id=lead.id,
        member_id=manager.id,
        now="2026-08-20T00:00:05+00:00",
    )
    _require(assigned.assigned_member_id == manager.id, "assignment_failed")
    projected = _item_for_lead(ui.list_open_work(actor=owner), lead.id)
    _require(projected["assigned_member_id"] == manager.id, "assignment_projection_member")
    _require(projected["assigned_user_id"] == manager_user_id, "assignment_projection_user")
    checks["assignment_projection"] = True

    try:
        sales.assign_member(
            actor=owner,
            lead_id=lead.id,
            member_id=other_access.membership.id,
        )
    except ValueError:
        checks["cross_tenant_assignee_blocked"] = True
    else:
        raise ProductionSalesSmokeError("cross_tenant_assignee_allowed")

    unassigned = sales.unassign_member(
        actor=owner,
        lead_id=lead.id,
        now="2026-08-20T00:00:06+00:00",
    )
    _require(unassigned.assigned_member_id is None, "unassignment_failed")
    checks["unassignment"] = True

    updated = sales.set_next_action(
        actor=owner,
        lead_id=lead.id,
        next_action="  Позвонить   клиенту  ",
        due_at="2026-08-20T10:00:00+03:00",
        now="2026-08-20T00:00:07+00:00",
    )
    _require(updated.next_action == "Позвонить клиенту", "next_action_normalization")
    _require(updated.due_at == "2026-08-20T07:00:00+00:00", "due_at_normalization")
    projected = _item_for_lead(ui.list_open_work(actor=owner), lead.id)
    _require(projected["next_action"] == updated.next_action, "next_action_projection")
    _require(projected["due_at"] == updated.due_at, "due_at_projection")
    checks["next_action_due"] = True

    first_note = sales.add_note(
        actor=owner,
        lead_id=lead.id,
        note="  Клиент   попросил перезвонить после 18:00. ",
        dedupe_key="production-smoke-note",
        now="2026-08-20T00:00:08+00:00",
    )
    replay_note = sales.add_note(
        actor=owner,
        lead_id=lead.id,
        note="duplicate should not persist",
        dedupe_key="production-smoke-note",
        now="2026-08-20T00:00:09+00:00",
    )
    _require(first_note is True and replay_note is False, "note_dedupe_failed")
    note_events = [
        event
        for event in sales.list_events(actor=owner, lead_id=lead.id)
        if event["event_type"] == "note_added"
    ]
    _require(len(note_events) == 1, "note_event_count")
    _require(
        note_events[0]["payload"].get("note") == "Клиент попросил перезвонить после 18:00.",
        "note_normalization",
    )
    checks["note_dedupe"] = True
    lost = sales.set_stage(
        actor=owner,
        lead_id=lead.id,
        stage=SalesLeadStage.LOST,
        reason="  бюджет   заморожен  ",
        now="2026-08-20T00:00:10+00:00",
    )
    _require(lost.closure_reason == "бюджет заморожен", "lost_reason_normalization")
    _require(lost.next_action is None and lost.due_at is None, "lost_followup_not_cleared")
    _require(
        all(str(item.get("id") or "") != lead.id for item in ui.list_open_work(actor=owner)),
        "lost_still_open",
    )
    closed = _item_for_lead(ui.list_recent_closed(actor=owner), lead.id)
    _require(closed["closure_reason"] == "бюджет заморожен", "lost_projection_reason")
    checks["lost_closure"] = True

    reopened = sales.set_stage(
        actor=owner,
        lead_id=lead.id,
        stage=SalesLeadStage.NEW,
        reason="клиент вернулся",
        now="2026-08-20T00:00:11+00:00",
    )
    _require(reopened.stage == SalesLeadStage.NEW, "lost_reopen_stage")
    _require(reopened.closure_reason is None, "lost_reopen_reason")
    checks["lost_reopen"] = True

    won = sales.set_stage(
        actor=owner,
        lead_id=lead.id,
        stage=SalesLeadStage.WON,
        reason="  Оплата   получена  ",
        now="2026-08-20T00:00:12+00:00",
    )
    _require(won.closure_reason == "Оплата получена", "won_reason_normalization")
    try:
        sales.set_stage(actor=owner, lead_id=lead.id, stage=SalesLeadStage.NEW)
    except SalesInvariantViolation:
        checks["won_terminal"] = True
    else:
        raise ProductionSalesSmokeError("won_regression_allowed")

    blocked = 0
    operations = (
        lambda: sales.get_lead(actor=other_owner, lead_id=lead.id),
        lambda: sales.set_next_action(
            actor=other_owner,
            lead_id=lead.id,
            next_action="Чужое действие",
        ),
        lambda: sales.set_stage(
            actor=other_owner,
            lead_id=lead.id,
            stage=SalesLeadStage.LOST,
            reason="чужая причина",
        ),
        lambda: sales.add_note(
            actor=other_owner,
            lead_id=lead.id,
            note="чужая заметка",
            dedupe_key="cross-tenant-production-smoke",
        ),
        lambda: sales.unassign_member(actor=other_owner, lead_id=lead.id),
    )
    for operation in operations:
        try:
            operation()
        except SalesLeadNotFound:
            blocked += 1
    _require(blocked == len(operations), "cross_tenant_mutation_allowed")
    checks["cross_tenant_fail_closed"] = True
    events = sales.list_events(actor=owner, lead_id=lead.id)
    event_types = [event["event_type"] for event in events]
    _require(event_types.count("assignee_changed") == 2, "assignee_audit_count")
    _require(event_types.count("next_action_changed") == 1, "next_action_audit_count")
    _require(event_types.count("note_added") == 1, "note_audit_count")
    _require(event_types.count("stage_changed") == 3, "stage_audit_count")
    checks["audit_events"] = True

    ActivityRepository(conn).upsert_profile(
        actor=owner,
        activity_description="Synthetic U009 service business",
        timezone_name="Europe/Moscow",
        now="2026-08-20T00:00:13+00:00",
    )
    followup_customer = CustomerRepository(conn).create_customer(
        actor=owner,
        display_name="Synthetic Follow-up Customer",
        now="2026-08-20T00:00:14+00:00",
    )
    identity = CustomerRepository(conn).attach_identity(
        actor=owner,
        customer_id=followup_customer.id,
        platform="telegram",
        external_subject=str(owner_user_id + 100_000_000),
        now="2026-08-20T00:00:15+00:00",
    )
    connections = ConnectionRepository(conn)
    connection = connections.create_connection(
        actor=owner,
        platform="telegram",
        connection_type="telegram_shared_bot",
        external_account_id="synthetic-u009-bot",
        credential_reference="secret://clientplatform/production-smoke/u009",
        permissions=("send_messages",),
        now="2026-08-20T00:00:16+00:00",
    )
    connection = connections.activate_connection(
        actor=owner,
        connection_id=connection.id,
        now="2026-08-20T00:00:17+00:00",
    )
    followup_lead = sales.create_or_refresh_lead(
        actor=owner,
        opportunity_key="production-smoke:u009",
        customer_id=followup_customer.id,
        source_kind="telegram",
        source_ref="synthetic-u009-production-smoke",
        contact_basis=ContactBasis.INBOUND,
        now="2026-08-20T00:00:18+00:00",
    )
    followups = SalesFollowupRepository(conn)
    followup = followups.schedule(
        actor=owner,
        lead_id=followup_lead.id,
        message_text="  Добрый день!   Подсказать по вашему вопросу? ",
        scheduled_at="2026-08-20T10:00:00+00:00",
        request_key="production-smoke-u009-followup",
        now="2026-08-20T08:00:00+00:00",
    )
    dispatches = DispatchOutboxRepository(conn)
    dispatch = dispatches.materialize_sales_followup(
        actor=owner,
        followup_id=followup.id,
        now="2026-08-20T08:00:01+00:00",
    )
    queued = followups.get(actor=owner, followup_id=followup.id)
    _require(queued.status.value == "queued", "u009_followup_not_queued")
    _require(queued.platform == "telegram", "u009_wrong_channel")
    _require(queued.customer_identity_id == identity.id, "u009_wrong_identity")
    _require(queued.connection_id == connection.id, "u009_wrong_connection")
    _require(
        queued.message_text == "Добрый день! Подсказать по вашему вопросу?",
        "u009_message_normalization",
    )
    _require(dispatch.source_kind == "sales_followup", "u009_wrong_outbox_source")
    _require(dispatch.source_id == queued.id, "u009_wrong_outbox_binding")
    _require(dispatch.platform.value == "telegram", "u009_wrong_outbox_channel")
    checks["u009_owner_approved_same_channel"] = True

    replay = dispatches.materialize_sales_followup(
        actor=owner,
        followup_id=queued.id,
        now="2026-08-20T08:00:02+00:00",
    )
    _require(replay.id == dispatch.id, "u009_dispatch_replay_created_duplicate")
    checks["u009_outbox_idempotency"] = True

    decision = followups.decision_for_send(
        business_id=owner.business_id,
        followup_id=queued.id,
        now="2026-08-20T10:00:00+00:00",
    )
    _require(decision.allowed, "u009_send_eligibility_failed")
    checks["u009_send_eligibility"] = True

    stopped = followups.suppress_channel(
        actor=owner,
        lead_id=followup_lead.id,
        now="2026-08-20T08:05:00+00:00",
    )
    _require(stopped == 1, "u009_opt_out_stop_count")
    stopped_followup = followups.get(actor=owner, followup_id=queued.id)
    _require(stopped_followup.status.value == "stopped", "u009_opt_out_status")
    _require(stopped_followup.stop_reason == "opt_out", "u009_opt_out_reason")
    dispatch_row = conn.execute(
        "SELECT status FROM provider_dispatch_outbox WHERE id=? AND business_id=?",
        (dispatch.id, owner.business_id),
    ).fetchone()
    _require(dispatch_row is not None, "u009_dispatch_missing_after_opt_out")
    dispatch_status = dispatch_row["status"] if hasattr(dispatch_row, "keys") else dispatch_row[0]
    _require(str(dispatch_status) == "cancelled", "u009_opt_out_dispatch_not_cancelled")
    try:
        followups.schedule(
            actor=owner,
            lead_id=followup_lead.id,
            message_text="Повтор после запрета",
            scheduled_at="2026-08-21T10:00:00+00:00",
            request_key="production-smoke-u009-after-opt-out",
            now="2026-08-20T08:06:00+00:00",
        )
    except SalesInvariantViolation:
        checks["u009_opt_out_suppression"] = True
    else:
        raise ProductionSalesSmokeError("u009_followup_allowed_after_opt_out")

    followup_events = sales.list_events(actor=owner, lead_id=followup_lead.id)
    followup_event_types = [event["event_type"] for event in followup_events]
    _require(followup_event_types.count("followup_scheduled") == 1, "u009_schedule_audit_count")
    _require(followup_event_types.count("followup_stopped") == 1, "u009_stop_audit_count")
    _require(followup_event_types.count("followup_opt_out") == 1, "u009_opt_out_audit_count")
    checks["u009_audit_events"] = True

    return {
        "contract_version": CONTRACT_VERSION,
        "ok": True,
        "checks": checks,
    }


def run_production_smoke(
    *,
    db_factory: Callable[[], ContextManager[Any]] = get_db,
    ro_factory: Callable[[], ContextManager[Any]] = get_db_ro,
    require_postgres: bool = True,
) -> dict[str, Any]:
    if require_postgres and not is_postgres_enabled():
        raise ProductionSalesSmokeError("postgres_required")

    business_ids = (str(uuid4()), str(uuid4()))
    seed = int(uuid4().int % 100_000_000)
    user_ids = (
        8_000_000_000 + seed * 10 + 1,
        8_000_000_000 + seed * 10 + 2,
        8_000_000_000 + seed * 10 + 3,
    )
    try:
        with db_factory() as conn:
            payload = _exercise(
                conn,
                business_ids=business_ids,
                user_ids=user_ids,
            )
            raise _RollbackSuccess(payload)
    except _RollbackSuccess as success:
        payload = success.payload
    except Exception as exc:  # validator: allow-wide-except - every operation failure must prove rollback cleanliness
        try:
            with ro_factory() as conn:
                residue = _residue_counts(conn, business_ids)
        except Exception as residue_exc:  # validator: allow-wide-except - rollback verification itself must fail closed
            raise ProductionSalesSmokeError("rollback_verification_failed") from residue_exc
        if any(residue.values()):
            raise ProductionSalesSmokeError("rollback_residue_detected") from exc
        if isinstance(exc, ProductionSalesSmokeError):
            raise
        raise ProductionSalesSmokeError("sales_operations_failed") from exc

    try:
        with ro_factory() as conn:
            residue = _residue_counts(conn, business_ids)
    except Exception as exc:  # validator: allow-wide-except - final residue verification must fail closed
        raise ProductionSalesSmokeError("rollback_verification_failed") from exc
    if any(residue.values()):
        raise ProductionSalesSmokeError("rollback_residue_detected")

    payload["rollback_clean"] = True
    payload["residue"] = residue
    return payload


def main() -> int:
    try:
        payload = run_production_smoke()
    except ProductionSalesSmokeError as exc:
        print(f"{FAILURE_MARKER}{exc}", file=sys.stderr)
        return 1
    except Exception:  # validator: allow-wide-except - operator entrypoint must never leak a traceback
        print(f"{FAILURE_MARKER}unexpected_error", file=sys.stderr)
        return 1
    print(
        SUCCESS_MARKER
        + json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
