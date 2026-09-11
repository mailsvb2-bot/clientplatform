from __future__ import annotations

import unittest
from uuid import uuid4

from clientplatform.ai_commerce.domain import (
    CommerceOperation,
    ProposalKind,
    UsageSnapshot,
)
from clientplatform.ai_commerce.service import build_proposal
from clientplatform.application.ai_commerce import (
    get_commerce_overview,
    request_subscription_expansion,
)
from clientplatform.application.admin_ops import get_subscription_state
from clientplatform.application.customers import create_customer
from clientplatform.application.tenancy import (
    create_business,
    grant_business_member,
    resolve_tenant_context,
)
from clientplatform.domain.support_cases import SupportCaseCategory
from clientplatform.domain.tenancy import PlatformRole
from services.db import get_db


class CoreTests(unittest.TestCase):
    def operation(self, operation_id: str = "operation-a", units: int = 1) -> CommerceOperation:
        return CommerceOperation(
            operation_id=operation_id,
            business_id=str(uuid4()),
            member_id=str(uuid4()),
            sku="staff_seat",
            requested_units=units,
        )

    def test_no_parallel_ledger_exists_in_domain(self) -> None:
        import clientplatform.ai_commerce.domain as domain

        self.assertFalse(hasattr(domain, "UsageLedger"))
        self.assertFalse(hasattr(domain, "OutcomeLedger"))

    def test_operation_id_is_business_operation_boundary(self) -> None:
        business_id = str(uuid4())
        member_id = str(uuid4())
        first = build_proposal(
            CommerceOperation("a", business_id, member_id, "staff_seat", 1),
            UsageSnapshot(0, 10),
        )
        second = build_proposal(
            CommerceOperation("b", business_id, member_id, "staff_seat", 1),
            UsageSnapshot(0, 10),
        )
        self.assertNotEqual(first.proposal_id, second.proposal_id)

    def test_upgrade_when_canonical_allowance_exceeded(self) -> None:
        proposal = build_proposal(self.operation(units=2), UsageSnapshot(9, 10))
        self.assertEqual(proposal.kind, ProposalKind.UPGRADE)
        self.assertEqual(proposal.projected_total, 11)

    def test_zero_allowance_is_fail_closed_not_unlimited(self) -> None:
        proposal = build_proposal(self.operation(), UsageSnapshot(0, 0))
        self.assertTrue(proposal.requires_upgrade)

    def test_usage_rejects_negative_or_boolean_values(self) -> None:
        with self.assertRaises(ValueError):
            UsageSnapshot(-1, 10)
        with self.assertRaises(ValueError):
            UsageSnapshot(True, 10)


def test_canonical_subscription_usage_drives_real_proposals() -> None:
    access = create_business(owner_user_id=930001, name="AI Commerce usage")
    actor = resolve_tenant_context(user_id=930001, business_id=access.business.id)
    get_subscription_state(actor=actor)
    with get_db() as conn:
        conn.execute(
            "UPDATE business_subscription_state SET included_staff=2, included_customers=1 WHERE business_id=?",
            (actor.business_id,),
        )
    create_customer(actor=actor, display_name="Первый клиент")

    first = get_commerce_overview(actor=actor)
    assert first.staff.kind is ProposalKind.INCLUDED
    assert first.staff.used == 1
    assert first.staff.projected_total == 2
    assert first.customers.kind is ProposalKind.UPGRADE
    assert first.customers.used == 1

    grant_business_member(actor=actor, user_id=930002, role=PlatformRole.MANAGER)
    second = get_commerce_overview(actor=actor)
    assert second.staff.kind is ProposalKind.UPGRADE
    assert second.staff.used == 2


def test_expansion_request_is_billing_scoped_and_idempotent() -> None:
    access = create_business(owner_user_id=930011, name="AI Commerce support")
    actor = resolve_tenant_context(user_id=930011, business_id=access.business.id)
    get_subscription_state(actor=actor)
    with get_db() as conn:
        conn.execute(
            "UPDATE business_subscription_state SET included_staff=1, included_customers=0 WHERE business_id=?",
            (actor.business_id,),
        )

    first = request_subscription_expansion(actor=actor)
    replay = request_subscription_expansion(actor=actor)
    assert first.id == replay.id
    assert first.category is SupportCaseCategory.BILLING
    assert "расширение тарифа" in first.summary.casefold()
