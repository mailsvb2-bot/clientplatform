from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.customers import CustomerIdentityStatus, CustomerPlatform
from clientplatform.domain.sales import (
    SalesActionKind,
    SalesInvariantViolation,
    can_contact,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


_OUTBOUND_ACTIONS = frozenset(
    {
        SalesActionKind.RESPOND,
        SalesActionKind.ASK_QUALIFICATION,
        SalesActionKind.PRESENT_OFFER,
        SalesActionKind.CHECKOUT_FOLLOWUP,
    }
)
_MACHINE_OUTBOUND_PLATFORMS = (
    CustomerPlatform.TELEGRAM,
    CustomerPlatform.VK,
    CustomerPlatform.MAX,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rowdict(row: Any) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return {str(key): row[key] for key in row.keys()}
    return dict(row)


class SalesActionRepository:
    """Approval/authorization boundary for persisted sales action plans.

    Planning is intentionally separate from dispatch. A plan may become outbound-
    eligible only after an authorized business member explicitly approves it.
    This repository never calls Telegram, VK, MAX or another external provider.
    """

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)
        self._sales = SalesRepository(conn)
        self._customers = CustomerRepository(conn)

    def _current(self, actor: TenantContext, *, manage: bool) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        if manage:
            current.assert_can_manage_customer_records()
        else:
            current.assert_can_view_customer_records()
        return current

    def get(self, *, actor: TenantContext, plan_id: str) -> dict[str, Any]:
        current = self._current(actor, manage=False)
        normalized = normalize_uuid(plan_id, field_name="sales_action_plan_id")
        row = self._conn.execute(
            """
            SELECT id, business_id, lead_id, action_kind, rationale,
                   requires_approval, status, created_by_member_id,
                   created_at, updated_at
            FROM clientplatform_sales_action_plans
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise ValueError("sales action plan was not found in the active business")
        return _rowdict(row)

    def approve(
        self,
        *,
        actor: TenantContext,
        plan_id: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        current = self._current(actor, manage=True)
        item = self.get(actor=current, plan_id=plan_id)
        status = str(item["status"])
        if status == "approved":
            return item
        if status != "planned":
            raise SalesInvariantViolation(
                f"sales action plan cannot be approved from status {status}"
            )
        action = SalesActionKind(str(item["action_kind"]))
        if action not in _OUTBOUND_ACTIONS:
            raise SalesInvariantViolation(
                "only an outward sales action may be approved for outbound"
            )
        lead = self._sales.get_lead(actor=current, lead_id=str(item["lead_id"]))
        if not can_contact(lead.contact_basis):
            raise SalesInvariantViolation(
                "sales outbound is forbidden without an active contact basis"
            )
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_action_plans
            SET status='approved', updated_at=?
            WHERE id=? AND business_id=? AND status='planned'
            """,
            (timestamp, item["id"], current.business_id),
        )
        if int(getattr(cursor, "rowcount", 1) or 0) != 1:
            latest = self.get(actor=current, plan_id=str(item["id"]))
            if str(latest["status"]) == "approved":
                return latest
            raise SalesInvariantViolation("sales action plan changed concurrently")
        self._sales.record_event(
            actor=current,
            lead_id=lead.id,
            event_type="sales_action_approved",
            dedupe_key=f"sales-action-approved:{item['id']}",
            payload={
                "plan_id": str(item["id"]),
                "action_kind": action.value,
                "approved_by_member_id": current.membership_id,
            },
            now=timestamp,
        )
        return self.get(actor=current, plan_id=str(item["id"]))

    def authorize_outbound(
        self,
        *,
        actor: TenantContext,
        plan_id: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Return a provider target only for an explicitly approved safe plan.

        The returned ``dispatch_allowed`` flag is an internal authorization, not
        an instruction to send automatically. Provider dispatch stays a separate
        explicit boundary and can consume this authorization later.
        """

        current = self._current(actor, manage=True)
        item = self.get(actor=current, plan_id=plan_id)
        if str(item["status"]) != "approved":
            raise SalesInvariantViolation(
                "sales outbound requires an explicitly approved action plan"
            )
        action = SalesActionKind(str(item["action_kind"]))
        if action not in _OUTBOUND_ACTIONS:
            raise SalesInvariantViolation("sales action is not outbound-capable")
        lead = self._sales.get_lead(actor=current, lead_id=str(item["lead_id"]))
        if not can_contact(lead.contact_basis):
            raise SalesInvariantViolation(
                "sales outbound is forbidden without an active contact basis"
            )
        customer = self._customers.get_customer(
            actor=current,
            customer_id=lead.customer_id,
        )
        active_identities = [
            identity
            for identity in customer.identities
            if identity.status == CustomerIdentityStatus.ACTIVE
            and identity.platform in _MACHINE_OUTBOUND_PLATFORMS
        ]
        preferred_platform = (
            CustomerPlatform(lead.source_kind)
            if lead.source_kind in {item.value for item in _MACHINE_OUTBOUND_PLATFORMS}
            else None
        )
        target = next(
            (
                identity
                for identity in active_identities
                if preferred_platform is not None
                and identity.platform == preferred_platform
            ),
            None,
        )
        if target is None:
            target = next(
                (
                    identity
                    for platform in _MACHINE_OUTBOUND_PLATFORMS
                    for identity in active_identities
                    if identity.platform == platform
                ),
                None,
            )
        if target is None:
            raise SalesInvariantViolation(
                "approved sales action has no active supported outbound identity"
            )

        timestamp = str(now or _utc_now())
        self._sales.record_event(
            actor=current,
            lead_id=lead.id,
            event_type="sales_outbound_authorized",
            dedupe_key=f"sales-outbound-authorized:{item['id']}",
            payload={
                "plan_id": str(item["id"]),
                "action_kind": action.value,
                "platform": target.platform.value,
                "authorized_by_member_id": current.membership_id,
                "dispatch_allowed": True,
            },
            now=timestamp,
        )
        return {
            "plan_id": str(item["id"]),
            "lead_id": lead.id,
            "customer_id": lead.customer_id,
            "action_kind": action.value,
            "platform": target.platform.value,
            "external_subject": target.external_subject,
            "username": target.username,
            "dispatch_allowed": True,
        }


__all__ = ["SalesActionRepository"]
