from __future__ import annotations

import sqlite3

import pytest

from clientplatform.domain.sales import (
    ContactBasis,
    SalesInvariantViolation,
    SalesLeadStage,
)
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_customers,
    clientplatform_sales,
    clientplatform_tenancy,
)


def test_lost_lead_must_reopen_before_won() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        clientplatform_tenancy.ensure(conn)
        clientplatform_customers.ensure(conn)
        clientplatform_activity.ensure(conn)
        clientplatform_sales.ensure(conn)

        tenancy = TenancyRepository(conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        owner = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        customer = CustomerRepository(conn).create_customer(
            actor=owner,
            display_name="Анна",
        )
        sales = SalesRepository(conn)
        lead = sales.create_or_refresh_lead(
            actor=owner,
            opportunity_key="web:lost-then-won",
            customer_id=customer.id,
            source_kind="website",
            source_ref="landing-main",
            contact_basis=ContactBasis.INBOUND,
        )
        lost = sales.set_stage(
            actor=owner,
            lead_id=lead.id,
            stage=SalesLeadStage.LOST,
            reason="нет бюджета",
        )

        with pytest.raises(
            SalesInvariantViolation,
            match="must be reopened before progressing",
        ):
            sales.set_stage(
                actor=owner,
                lead_id=lost.id,
                stage=SalesLeadStage.WON,
                reason="оплата пришла позднее",
            )

        reopened = sales.set_stage(
            actor=owner,
            lead_id=lost.id,
            stage=SalesLeadStage.NEW,
            reason="клиент вернулся",
        )
        won = sales.set_stage(
            actor=owner,
            lead_id=reopened.id,
            stage=SalesLeadStage.WON,
            reason="оплата получена",
        )
        assert won.stage is SalesLeadStage.WON
        assert won.closure_reason == "оплата получена"
    finally:
        conn.close()
