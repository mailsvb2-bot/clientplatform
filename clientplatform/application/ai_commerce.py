from __future__ import annotations

from dataclasses import dataclass

from clientplatform.ai_commerce.domain import (
    CommerceOperation,
    CommerceProposal,
    UsageSnapshotProvider,
)
from clientplatform.ai_commerce.service import build_proposal
from clientplatform.application import admin_ops, support_cases
from clientplatform.domain.support_cases import SupportCase, SupportCaseCategory
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied
from clientplatform.infrastructure.ai_commerce_usage import (
    ACTIVE_CUSTOMER_SKU,
    STAFF_SEAT_SKU,
    CanonicalSubscriptionUsageProvider,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db_ro


@dataclass(frozen=True, slots=True)
class CommerceOverview:
    staff: CommerceProposal
    customers: CommerceProposal

    @property
    def expansion_recommended(self) -> bool:
        return self.staff.requires_upgrade or self.customers.requires_upgrade


def prepare_ai_commerce_proposal(
    *,
    actor: TenantContext,
    operation_id: str,
    sku: str,
    requested_units: int,
    usage_provider: UsageSnapshotProvider,
) -> CommerceProposal:
    """Prepare a tenant-scoped proposal without creating a parallel ledger."""

    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_outcome_ledger()
        usage = usage_provider.get_usage(business_id=current.business_id, sku=sku)
        return build_proposal(
            CommerceOperation(
                operation_id=operation_id,
                business_id=current.business_id,
                member_id=current.membership_id,
                sku=sku,
                requested_units=requested_units,
            ),
            usage,
        )


def get_commerce_overview(*, actor: TenantContext) -> CommerceOverview:
    if actor.role is not PlatformRole.OWNER:
        raise TenantPermissionDenied("tariff commerce overview is owner-only")
    subscription = admin_ops.get_subscription_state(actor=actor)
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        provider = CanonicalSubscriptionUsageProvider(
            conn,
            allowances={
                STAFF_SEAT_SKU: subscription.included_staff,
                ACTIVE_CUSTOMER_SKU: subscription.included_customers,
            },
        )
        staff_usage = provider.get_usage(business_id=current.business_id, sku=STAFF_SEAT_SKU)
        customer_usage = provider.get_usage(
            business_id=current.business_id, sku=ACTIVE_CUSTOMER_SKU
        )
        staff = build_proposal(
            CommerceOperation(
                operation_id=(
                    f"tariff-preview:staff:{staff_usage.used}:{staff_usage.allowance}"
                ),
                business_id=current.business_id,
                member_id=current.membership_id,
                sku=STAFF_SEAT_SKU,
                requested_units=1,
            ),
            staff_usage,
        )
        customers = build_proposal(
            CommerceOperation(
                operation_id=(
                    f"tariff-preview:customer:{customer_usage.used}:{customer_usage.allowance}"
                ),
                business_id=current.business_id,
                member_id=current.membership_id,
                sku=ACTIVE_CUSTOMER_SKU,
                requested_units=1,
            ),
            customer_usage,
        )
    return CommerceOverview(staff=staff, customers=customers)


def request_subscription_expansion(*, actor: TenantContext) -> SupportCase:
    overview = get_commerce_overview(actor=actor)
    reasons: list[str] = []
    if overview.staff.requires_upgrade:
        reasons.append(
            f"сотрудники {overview.staff.used}/{overview.staff.allowance}"
        )
    if overview.customers.requires_upgrade:
        reasons.append(
            f"клиенты {overview.customers.used}/{overview.customers.allowance}"
        )
    if not reasons:
        reasons.append(
            "владелец запросил информацию о расширении до исчерпания текущих лимитов"
        )
    fingerprint = (
        f"{overview.staff.used}:{overview.staff.allowance}:"
        f"{overview.customers.used}:{overview.customers.allowance}"
    )
    return support_cases.create_support_case(
        actor=actor,
        category=SupportCaseCategory.BILLING,
        summary=(
            "Запрос владельца на расширение тарифа ClientPlatform: " + "; ".join(reasons)
        ),
        idempotency_key=f"ai-commerce-expansion:{actor.business_id}:{fingerprint}",
    )


__all__ = [
    "ACTIVE_CUSTOMER_SKU",
    "CommerceOverview",
    "STAFF_SEAT_SKU",
    "get_commerce_overview",
    "prepare_ai_commerce_proposal",
    "request_subscription_expansion",
]
