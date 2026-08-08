from __future__ import annotations

from clientplatform.domain.sales import (
    ContactBasis,
    SalesActionPlan,
    SalesLead,
    plan_sales_action,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.sales_repository import SalesRepository
from services.db import get_db, get_db_ro


def create_or_refresh_sales_lead(
    *,
    actor: TenantContext,
    opportunity_key: str,
    customer_id: str,
    source_kind: str,
    contact_basis: ContactBasis | str,
    offering_id: str | None = None,
    source_ref: str | None = None,
) -> SalesLead:
    with get_db() as conn:
        return SalesRepository(conn).create_or_refresh_lead(
            actor=actor,
            opportunity_key=opportunity_key,
            customer_id=customer_id,
            source_kind=source_kind,
            contact_basis=contact_basis,
            offering_id=offering_id,
            source_ref=source_ref,
        )


def get_sales_lead(*, actor: TenantContext, lead_id: str) -> SalesLead:
    with get_db_ro() as conn:
        return SalesRepository(conn).get_lead(actor=actor, lead_id=lead_id)


def build_next_sales_plan(
    *,
    actor: TenantContext,
    lead_id: str,
    model_confidence: float,
    unanswered_inbound: bool = False,
    explicit_human_request: bool = False,
    sensitive_context: bool = False,
) -> SalesActionPlan:
    """Plan and persist the next action; never perform the external action."""

    with get_db() as conn:
        repository = SalesRepository(conn)
        lead = repository.get_lead(actor=actor, lead_id=lead_id)
        plan = plan_sales_action(
            lead,
            model_confidence=model_confidence,
            unanswered_inbound=unanswered_inbound,
            explicit_human_request=explicit_human_request,
            sensitive_context=sensitive_context,
        )
        repository.save_plan(actor=actor, plan=plan)
        return plan
