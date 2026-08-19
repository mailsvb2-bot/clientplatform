from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.customers import CustomerStatus
from clientplatform.domain.sales import (
    ContactBasis,
    SalesActionPlan,
    SalesLead,
    SalesInvariantViolation,
    SalesLeadNotFound,
    SalesLeadStage,
    normalize_closure_reason,
    normalize_due_at,
    normalize_next_action,
    normalize_opportunity_key,
    normalize_source_kind,
    normalize_source_ref,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


_MAX_EVENT_PAYLOAD_BYTES = 32 * 1024


def _event_payload_json(payload: dict[str, Any] | None) -> str:
    if payload is not None and not isinstance(payload, dict):
        raise ValueError("sales event payload must be an object")
    try:
        encoded = json.dumps(
            dict(payload or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("sales event payload must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > _MAX_EVENT_PAYLOAD_BYTES:
        raise ValueError("sales event payload is too large")
    return encoded


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _operation_dedupe(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part or "") for part in parts).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    return f"{prefix}:{digest}"


def _normalize_note(value: object) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
    if not normalized:
        raise ValueError("note must not be empty")
    if len(normalized) > 4000:
        raise ValueError("note must be at most 4000 characters")
    return normalized


def _lead_from_row(row: Any) -> SalesLead:
    offering_id = _value(row, "offering_id", 4)
    assigned_member_id = _value(row, "assigned_member_id", 9)
    return SalesLead(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        opportunity_key=str(_value(row, "opportunity_key", 2)),
        customer_id=str(_value(row, "customer_id", 3)),
        offering_id=None if offering_id is None else str(offering_id),
        source_kind=str(_value(row, "source_kind", 5)),
        source_ref=_value(row, "source_ref", 6),
        contact_basis=ContactBasis(str(_value(row, "contact_basis", 7))),
        stage=SalesLeadStage(str(_value(row, "stage", 8))),
        assigned_member_id=(
            None if assigned_member_id is None else str(assigned_member_id)
        ),
        next_action=_value(row, "next_action", 10),
        due_at=_value(row, "due_at", 11),
        closure_reason=_value(row, "closure_reason", 12),
        last_signal_at=str(_value(row, "last_signal_at", 13)),
        created_at=str(_value(row, "created_at", 14)),
        updated_at=str(_value(row, "updated_at", 15)),
    )


_LEAD_SELECT = """
    SELECT id, business_id, opportunity_key, customer_id, offering_id,
           source_kind, source_ref, contact_basis, stage, assigned_member_id,
           next_action, due_at, closure_reason,
           last_signal_at, created_at, updated_at
    FROM clientplatform_sales_leads
"""


class SalesRepository:
    """Tenant-safe sales projection. It never sends a message or calls a provider."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)
        self._customers = CustomerRepository(conn)

    def _current(self, actor: TenantContext, *, manage: bool) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id, business_id=actor.business_id
        )
        if manage:
            current.assert_can_manage_customer_records()
        else:
            current.assert_can_view_customer_records()
        return current

    def create_or_refresh_lead(
        self,
        *,
        actor: TenantContext,
        opportunity_key: str,
        customer_id: str,
        source_kind: str,
        contact_basis: ContactBasis | str,
        offering_id: str | None = None,
        source_ref: str | None = None,
        now: str | None = None,
    ) -> SalesLead:
        current = self._current(actor, manage=True)
        customer = self._customers.get_customer(
            actor=current, customer_id=customer_id
        ).customer
        current.assert_business(customer.business_id)
        if customer.status != CustomerStatus.ACTIVE:
            raise SalesInvariantViolation("sales opportunity requires an active customer")
        normalized_key = normalize_opportunity_key(opportunity_key)
        normalized_source = normalize_source_kind(source_kind)
        normalized_ref = normalize_source_ref(source_ref)
        basis = (
            contact_basis
            if isinstance(contact_basis, ContactBasis)
            else ContactBasis(str(contact_basis))
        )
        normalized_offering = None
        if offering_id is not None:
            normalized_offering = normalize_uuid(
                offering_id, field_name="offering_id"
            )
            found = self._conn.execute(
                """
                SELECT 1 FROM business_offerings
                WHERE id=? AND business_id=? AND status='active'
                LIMIT 1
                """,
                (normalized_offering, current.business_id),
            ).fetchone()
            if found is None:
                raise ValueError("offering was not found in the active business")
        existing = self._conn.execute(
            _LEAD_SELECT + " WHERE business_id=? AND opportunity_key=? LIMIT 1",
            (current.business_id, normalized_key),
        ).fetchone()
        if existing is not None:
            existing_lead = _lead_from_row(existing)
            if existing_lead.customer_id != customer.id:
                raise SalesInvariantViolation(
                    "sales opportunity key already belongs to another customer"
                )

        timestamp = str(now or _utc_now())
        lead_id = str(uuid4())
        self._conn.execute(
            """
            INSERT INTO clientplatform_sales_leads(
                id, business_id, opportunity_key, customer_id, offering_id,
                source_kind, source_ref, contact_basis, stage, assigned_member_id,
                next_action, due_at, closure_reason,
                last_signal_at, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,'new',NULL,NULL,NULL,NULL,?,?,?)
            ON CONFLICT(business_id, opportunity_key) DO UPDATE SET
                source_kind=excluded.source_kind,
                source_ref=COALESCE(
                    excluded.source_ref, clientplatform_sales_leads.source_ref
                ),
                contact_basis=CASE
                    WHEN clientplatform_sales_leads.contact_basis='none'
                    THEN excluded.contact_basis
                    ELSE clientplatform_sales_leads.contact_basis
                END,
                offering_id=COALESCE(
                    excluded.offering_id, clientplatform_sales_leads.offering_id
                ),
                last_signal_at=excluded.last_signal_at,
                updated_at=excluded.updated_at
            """,
            (
                lead_id,
                current.business_id,
                normalized_key,
                customer.id,
                normalized_offering,
                normalized_source,
                normalized_ref,
                basis.value,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        row = self._conn.execute(
            _LEAD_SELECT + " WHERE business_id=? AND opportunity_key=? LIMIT 1",
            (current.business_id, normalized_key),
        ).fetchone()
        if row is None:
            raise RuntimeError("sales lead upsert failed")
        return _lead_from_row(row)

    def get_lead(self, *, actor: TenantContext, lead_id: str) -> SalesLead:
        current = self._current(actor, manage=False)
        normalized = normalize_uuid(lead_id, field_name="sales_lead_id")
        row = self._conn.execute(
            _LEAD_SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise SalesLeadNotFound("sales lead was not found in the active business")
        return _lead_from_row(row)

    def set_stage(
        self,
        *,
        actor: TenantContext,
        lead_id: str,
        stage: SalesLeadStage | str,
        reason: str | None = None,
        now: str | None = None,
    ) -> SalesLead:
        current = self._current(actor, manage=True)
        lead = self.get_lead(actor=current, lead_id=lead_id)
        selected = (
            stage if isinstance(stage, SalesLeadStage) else SalesLeadStage(str(stage))
        )
        if lead.stage == selected:
            return lead
        if lead.stage == SalesLeadStage.WON and selected != SalesLeadStage.WON:
            raise SalesInvariantViolation("won sales lead cannot regress")
        if (
            lead.stage == SalesLeadStage.LOST
            and selected not in {SalesLeadStage.NEW, SalesLeadStage.WON}
        ):
            raise SalesInvariantViolation(
                "lost sales lead must be reopened before progressing"
            )
        timestamp = str(now or _utc_now())
        normalized_reason = normalize_closure_reason(reason)
        if selected in {SalesLeadStage.WON, SalesLeadStage.LOST}:
            durable_reason = normalized_reason or selected.value
            self._conn.execute(
                """
                UPDATE clientplatform_sales_leads
                SET stage=?, closure_reason=?, next_action=NULL, due_at=NULL,
                    last_signal_at=?, updated_at=?
                WHERE id=? AND business_id=?
                """,
                (
                    selected.value,
                    durable_reason,
                    timestamp,
                    timestamp,
                    lead.id,
                    current.business_id,
                ),
            )
        else:
            durable_reason = None
            self._conn.execute(
                """
                UPDATE clientplatform_sales_leads
                SET stage=?, closure_reason=NULL, last_signal_at=?, updated_at=?
                WHERE id=? AND business_id=?
                """,
                (
                    selected.value,
                    timestamp,
                    timestamp,
                    lead.id,
                    current.business_id,
                ),
            )
        self.record_event(
            actor=current,
            lead_id=lead.id,
            event_type="stage_changed",
            dedupe_key=_operation_dedupe(
                "stage",
                lead.stage.value,
                selected.value,
                durable_reason or normalized_reason,
                timestamp,
            ),
            payload={
                "actor_member_id": current.membership_id,
                "from_stage": lead.stage.value,
                "to_stage": selected.value,
                "reason": durable_reason or normalized_reason,
            },
            now=timestamp,
        )
        return self.get_lead(actor=current, lead_id=lead.id)

    def assign_member(
        self,
        *,
        actor: TenantContext,
        lead_id: str,
        member_id: str,
        now: str | None = None,
    ) -> SalesLead:
        current = self._current(actor, manage=True)
        lead = self.get_lead(actor=current, lead_id=lead_id)
        normalized_member = normalize_uuid(member_id, field_name="member_id")
        row = self._conn.execute(
            """
            SELECT 1 FROM business_members
            WHERE id=? AND business_id=? AND status='active' LIMIT 1
            """,
            (normalized_member, current.business_id),
        ).fetchone()
        if row is None:
            raise ValueError("sales assignee was not found in the active business")
        if lead.assigned_member_id == normalized_member:
            return lead
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            UPDATE clientplatform_sales_leads
            SET assigned_member_id=?, updated_at=?
            WHERE id=? AND business_id=?
            """,
            (normalized_member, timestamp, lead.id, current.business_id),
        )
        self.record_event(
            actor=current,
            lead_id=lead.id,
            event_type="assignee_changed",
            dedupe_key=_operation_dedupe(
                "assignee",
                lead.assigned_member_id,
                normalized_member,
                timestamp,
            ),
            payload={
                "actor_member_id": current.membership_id,
                "from_member_id": lead.assigned_member_id,
                "to_member_id": normalized_member,
            },
            now=timestamp,
        )
        return self.get_lead(actor=current, lead_id=lead.id)

    def unassign_member(
        self,
        *,
        actor: TenantContext,
        lead_id: str,
        now: str | None = None,
    ) -> SalesLead:
        current = self._current(actor, manage=True)
        lead = self.get_lead(actor=current, lead_id=lead_id)
        if lead.assigned_member_id is None:
            return lead
        timestamp = str(now or _utc_now())
        previous_member_id = lead.assigned_member_id
        self._conn.execute(
            """
            UPDATE clientplatform_sales_leads
            SET assigned_member_id=NULL, updated_at=?
            WHERE id=? AND business_id=?
            """,
            (timestamp, lead.id, current.business_id),
        )
        self.record_event(
            actor=current,
            lead_id=lead.id,
            event_type="assignee_changed",
            dedupe_key=_operation_dedupe(
                "assignee", previous_member_id, "unassigned", timestamp
            ),
            payload={
                "actor_member_id": current.membership_id,
                "from_member_id": previous_member_id,
                "to_member_id": None,
            },
            now=timestamp,
        )
        return self.get_lead(actor=current, lead_id=lead.id)

    def set_next_action(
        self,
        *,
        actor: TenantContext,
        lead_id: str,
        next_action: str | None,
        due_at: str | None = None,
        now: str | None = None,
    ) -> SalesLead:
        current = self._current(actor, manage=True)
        lead = self.get_lead(actor=current, lead_id=lead_id)
        if lead.stage in {SalesLeadStage.WON, SalesLeadStage.LOST}:
            raise SalesInvariantViolation("closed sales lead cannot receive a next action")
        normalized_action = normalize_next_action(next_action)
        normalized_due = normalize_due_at(due_at)
        if normalized_due is not None and normalized_action is None:
            raise SalesInvariantViolation("due_at requires a durable next action")
        if lead.next_action == normalized_action and lead.due_at == normalized_due:
            return lead
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            UPDATE clientplatform_sales_leads
            SET next_action=?, due_at=?, updated_at=?
            WHERE id=? AND business_id=?
            """,
            (
                normalized_action,
                normalized_due,
                timestamp,
                lead.id,
                current.business_id,
            ),
        )
        self.record_event(
            actor=current,
            lead_id=lead.id,
            event_type="next_action_changed",
            dedupe_key=_operation_dedupe(
                "next_action", normalized_action, normalized_due, timestamp
            ),
            payload={
                "actor_member_id": current.membership_id,
                "from_next_action": lead.next_action,
                "from_due_at": lead.due_at,
                "next_action": normalized_action,
                "due_at": normalized_due,
            },
            now=timestamp,
        )
        return self.get_lead(actor=current, lead_id=lead.id)

    def clear_next_action(
        self,
        *,
        actor: TenantContext,
        lead_id: str,
        now: str | None = None,
    ) -> SalesLead:
        return self.set_next_action(
            actor=actor,
            lead_id=lead_id,
            next_action=None,
            due_at=None,
            now=now,
        )

    def add_note(
        self,
        *,
        actor: TenantContext,
        lead_id: str,
        note: str,
        dedupe_key: str,
        now: str | None = None,
    ) -> bool:
        current = self._current(actor, manage=True)
        normalized_note = _normalize_note(note)
        normalized_dedupe = str(dedupe_key or "").strip()
        if not normalized_dedupe:
            raise ValueError("dedupe_key must not be empty")
        return self.record_event(
            actor=current,
            lead_id=lead_id,
            event_type="note_added",
            dedupe_key=_operation_dedupe("note", normalized_dedupe),
            payload={
                "actor_member_id": current.membership_id,
                "note": normalized_note,
            },
            now=now,
        )

    def record_event(
        self,
        *,
        actor: TenantContext,
        lead_id: str,
        event_type: str,
        dedupe_key: str,
        payload: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> bool:
        current = self._current(actor, manage=True)
        lead = self.get_lead(actor=current, lead_id=lead_id)
        normalized_type = str(event_type or "").strip()
        normalized_dedupe = str(dedupe_key or "").strip()
        if not normalized_type or len(normalized_type) > 100:
            raise ValueError("event_type must be 1..100 characters")
        if not normalized_dedupe or len(normalized_dedupe) > 240:
            raise ValueError("dedupe_key must be 1..240 characters")
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            INSERT INTO clientplatform_sales_events(
                id, business_id, lead_id, event_type, dedupe_key,
                payload_json, occurred_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(business_id, lead_id, dedupe_key) DO NOTHING
            """,
            (
                str(uuid4()),
                current.business_id,
                lead.id,
                normalized_type,
                normalized_dedupe,
                _event_payload_json(payload),
                timestamp,
            ),
        )
        return int(getattr(cursor, "rowcount", 1) or 0) == 1

    def save_plan(
        self,
        *,
        actor: TenantContext,
        plan: SalesActionPlan,
        now: str | None = None,
    ) -> str:
        current = self._current(actor, manage=True)
        lead = self.get_lead(actor=current, lead_id=plan.lead_id)
        timestamp = str(now or _utc_now())
        plan_id = str(uuid4())
        # Serialize replans on the lead row before touching active plans. This
        # no-op UPDATE is deliberately cross-dialect: PostgreSQL takes a row
        # write lock until transaction end, while SQLite takes its normal write
        # lock. A second concurrent replan therefore cannot dismiss-and-insert
        # against the same stale snapshot; after the first commits it observes
        # and dismisses that first replacement, preserving latest-plan-wins.
        lock_cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_leads
            SET updated_at=updated_at
            WHERE id=? AND business_id=?
            """,
            (lead.id, current.business_id),
        )
        if int(getattr(lock_cursor, "rowcount", 1) or 0) != 1:
            raise SalesLeadNotFound("sales lead was not found in the active business")
        # A recommendation is a snapshot of the latest known conversation state.
        # Replanning atomically invalidates any older unsent recommendation, even
        # one the owner approved earlier, so stale callbacks cannot authorize it.
        self._conn.execute(
            """
            UPDATE clientplatform_sales_action_plans
            SET status='dismissed', updated_at=?
            WHERE business_id=? AND lead_id=? AND status IN ('planned','approved')
            """,
            (timestamp, current.business_id, lead.id),
        )
        self._conn.execute(
            """
            INSERT INTO clientplatform_sales_action_plans(
                id, business_id, lead_id, action_kind, rationale,
                requires_approval, status, created_by_member_id,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,'planned',?,?,?)
            """,
            (
                plan_id,
                current.business_id,
                lead.id,
                plan.action_kind.value,
                plan.rationale,
                1 if plan.requires_approval else 0,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        return plan_id

    def list_events(
        self, *, actor: TenantContext, lead_id: str
    ) -> list[dict[str, Any]]:
        current = self._current(actor, manage=False)
        lead = self.get_lead(actor=current, lead_id=lead_id)
        rows = self._conn.execute(
            """
            SELECT event_type, dedupe_key, payload_json, occurred_at
            FROM clientplatform_sales_events
            WHERE business_id=? AND lead_id=?
            ORDER BY occurred_at, id
            """,
            (current.business_id, lead.id),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload_raw = _value(row, "payload_json", 2)
            try:
                payload = json.loads(str(payload_raw or "{}"))
            except (TypeError, ValueError):
                payload = {}
            result.append(
                {
                    "event_type": str(_value(row, "event_type", 0)),
                    "dedupe_key": str(_value(row, "dedupe_key", 1)),
                    "payload": payload if isinstance(payload, dict) else {},
                    "occurred_at": str(_value(row, "occurred_at", 3)),
                }
            )
        return result
