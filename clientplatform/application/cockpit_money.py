from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID

from clientplatform.application import admin_ops
from clientplatform.application.activity import list_business_capabilities, list_business_offerings
from clientplatform.application.cockpit import resolve_cockpit_context
from clientplatform.application.customers import list_customers
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.activity import BusinessOffering, CapabilityStatus, OfferingStatus, resolve_activity_connector
from clientplatform.domain.money import normalize_settlement_currency, settlement_currency_minor_unit_exponent
from clientplatform.domain.tenancy import TenantAccessDenied, TenantContext, TenantPermissionDenied

_SCHEMA_VERSION = "2026-09-07.v1"


@dataclass(frozen=True, slots=True)
class CockpitMoneyTotal:
    currency: str
    amount_minor: int
    display: str
    paid_payments: int


@dataclass(frozen=True, slots=True)
class CockpitMoneyPayment:
    id: str
    amount_minor: int
    currency: str
    display: str
    status: str
    note: str
    provider: str
    created_at: str
    refundable: bool


@dataclass(frozen=True, slots=True)
class CockpitMoneyChoice:
    id: str
    title: str


@dataclass(frozen=True, slots=True)
class CockpitMoneySnapshot:
    schema_version: str
    business_id: str
    business_name: str
    totals: tuple[CockpitMoneyTotal, ...]
    paid_payments: int
    paid_customers: int
    recent: tuple[CockpitMoneyPayment, ...]
    can_write: bool
    customer_choices: tuple[CockpitMoneyChoice, ...]
    offering_choices: tuple[CockpitMoneyChoice, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _display(amount_minor: int, currency: str) -> str:
    exponent = settlement_currency_minor_unit_exponent(currency)
    amount = Decimal(int(amount_minor)) / (Decimal(10) ** exponent)
    rendered = f"{amount:,.{exponent}f}" if exponent else f"{amount:,.0f}"
    return f"{rendered.replace(',', ' ')} {currency}"


def _major_to_minor(value: str, currency: str) -> tuple[int, str]:
    normalized_currency = normalize_settlement_currency(currency)
    try:
        amount = Decimal(str(value or "").replace(",", ".").strip())
    except InvalidOperation as exc:
        raise ValueError("amount must be numeric") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be positive")
    exponent = settlement_currency_minor_unit_exponent(normalized_currency)
    scaled = amount * (Decimal(10) ** exponent)
    rounded = scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if scaled != rounded:
        raise ValueError("amount has too many decimal places for currency")
    return int(rounded), normalized_currency


def _current(actor: TenantContext) -> TenantContext:
    current = resolve_tenant_context(user_id=actor.user_id, business_id=actor.business_id)
    if current.role not in admin_ops._FINANCE_READ_ROLES:
        raise TenantPermissionDenied("finance read is not available for this role")
    return current


def _active_offerings(actor: TenantContext) -> list[BusinessOffering]:
    rows: list[BusinessOffering] = []
    for capability in list_business_capabilities(actor=actor):
        if capability.status != CapabilityStatus.ACTIVE:
            continue
        if not resolve_activity_connector(capability.connector_key).supports_offerings:
            continue
        rows.extend(
            item
            for item in list_business_offerings(actor=actor, capability_id=capability.id)
            if item.status == OfferingStatus.ACTIVE
        )
    rows.sort(key=lambda item: (item.title.casefold(), item.id))
    return rows


def build_cockpit_money(*, actor: TenantContext, business_name: str, limit: int = 20) -> CockpitMoneySnapshot:
    current = _current(actor)
    normalized_limit = max(1, min(int(limit), 50))
    summary = admin_ops.payment_summary(actor=current)
    payments = admin_ops.list_payments(actor=current, limit=normalized_limit)
    can_write = current.role in admin_ops._FINANCE_WRITE_ROLES

    customer_choices: list[CockpitMoneyChoice] = []
    if can_write:
        try:
            current.assert_can_view_customer_records()
        except TenantPermissionDenied:
            pass
        else:
            customer_choices = [
                CockpitMoneyChoice(id=item.id, title=item.display_name or "Клиент")
                for item in list_customers(actor=current)
            ]
            customer_choices.sort(key=lambda item: (item.title.casefold(), item.id))

    offering_choices: list[CockpitMoneyChoice] = []
    if can_write:
        offering_choices = [
            CockpitMoneyChoice(id=item.id, title=item.title)
            for item in _active_offerings(current)
        ]

    return CockpitMoneySnapshot(
        schema_version=_SCHEMA_VERSION,
        business_id=current.business_id,
        business_name=str(business_name),
        totals=tuple(
            CockpitMoneyTotal(
                currency=item.currency,
                amount_minor=item.amount_minor,
                display=_display(item.amount_minor, item.currency),
                paid_payments=item.paid_payments,
            )
            for item in summary.by_currency
        ),
        paid_payments=summary.paid_payments,
        paid_customers=summary.paid_customers,
        recent=tuple(
            CockpitMoneyPayment(
                id=item.id,
                amount_minor=item.amount_minor,
                currency=item.currency,
                display=_display(item.amount_minor, item.currency),
                status=item.status,
                note=item.note,
                provider=item.provider,
                created_at=item.created_at,
                refundable=(
                    can_write and item.status == "paid" and item.outcome_event_id is not None
                ),
            )
            for item in payments
        ),
        can_write=can_write,
        customer_choices=tuple(customer_choices),
        offering_choices=tuple(offering_choices),
    )


def _resolve_actor(*, telegram_user_id: int, requested_business_id: str | None) -> tuple[TenantContext, str]:
    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if context.onboarding_required or context.business_id is None or context.business_name is None:
        raise TenantAccessDenied("active business membership was not found")
    actor = resolve_tenant_context(user_id=context.user_id, business_id=context.business_id)
    return _current(actor), context.business_name


def resolve_cockpit_money(
    *, telegram_user_id: int, requested_business_id: str | None = None, limit: int = 20
) -> CockpitMoneySnapshot:
    actor, business_name = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    return build_cockpit_money(actor=actor, business_name=business_name, limit=limit)


def record_cockpit_payment(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    amount: str,
    currency: str,
    request_id: str,
    customer_id: str | None,
    offering_id: str | None,
    note: str,
) -> admin_ops.PaymentRecord:
    actor, _ = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if actor.role not in admin_ops._FINANCE_WRITE_ROLES:
        raise TenantPermissionDenied("finance write is not available for this role")
    try:
        normalized_request = str(UUID(str(request_id)))
    except ValueError as exc:
        raise ValueError("payment request id must be a UUID") from exc
    if customer_id:
        # Finance permission alone must not become a side door into CRM-linked
        # facts. A browser-supplied customer UUID is accepted only when the
        # current live role can also view customer records.
        actor.assert_can_view_customer_records()
    amount_minor, normalized_currency = _major_to_minor(amount, currency)
    return admin_ops.record_payment(
        actor=actor,
        amount_minor=amount_minor,
        currency=normalized_currency,
        customer_id=customer_id or None,
        offering_id=offering_id or None,
        note=note,
        idempotency_key=f"cockpit-payment:{normalized_request}",
    )


def refund_cockpit_payment(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    payment_id: str,
) -> admin_ops.PaymentRecord:
    actor, _ = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if actor.role not in admin_ops._FINANCE_WRITE_ROLES:
        raise TenantPermissionDenied("finance write is not available for this role")
    return admin_ops.refund_payment(
        actor=actor,
        payment_id=payment_id,
        idempotency_key=f"cockpit-refund:{payment_id}",
        reason="owner_confirmed_full_refund",
    )


__all__ = [
    "CockpitMoneyChoice",
    "CockpitMoneyPayment",
    "CockpitMoneySnapshot",
    "CockpitMoneyTotal",
    "build_cockpit_money",
    "record_cockpit_payment",
    "refund_cockpit_payment",
    "resolve_cockpit_money",
]
