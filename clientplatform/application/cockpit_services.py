from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from clientplatform.application import admin_ops
from clientplatform.application.activity import (
    archive_business_offering,
    create_business_offering,
    list_business_capabilities,
    list_business_offerings,
)
from clientplatform.application.cockpit import resolve_cockpit_context
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.activity import (
    BusinessCapability,
    BusinessOffering,
    CapabilityStatus,
    OfferingStatus,
    resolve_activity_connector,
)
from clientplatform.domain.money import (
    normalize_settlement_currency,
    settlement_currency_minor_unit_exponent,
)
from clientplatform.domain.tenancy import TenantAccessDenied, TenantContext, TenantPermissionDenied

_SCHEMA_VERSION = "2026-09-07.v1"


@dataclass(frozen=True, slots=True)
class CockpitServiceCapability:
    id: str
    connector_key: str
    title: str


@dataclass(frozen=True, slots=True)
class CockpitServiceItem:
    id: str
    capability_id: str
    capability_title: str
    title: str
    description: str
    price_minor: int | None
    price_currency: str | None
    price_display: str | None
    price_input: str | None


@dataclass(frozen=True, slots=True)
class CockpitServicesSnapshot:
    schema_version: str
    business_id: str
    business_name: str
    capabilities: tuple[CockpitServiceCapability, ...]
    items: tuple[CockpitServiceItem, ...]
    can_manage_offerings: bool
    can_manage_prices: bool
    can_view_prices: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _money_display(amount_minor: int, currency: str) -> str:
    exponent = settlement_currency_minor_unit_exponent(currency)
    amount = Decimal(int(amount_minor)) / (Decimal(10) ** exponent)
    rendered = f"{amount:,.{exponent}f}" if exponent else f"{amount:,.0f}"
    return f"{rendered.replace(',', ' ')} {currency}"


def _money_input(amount_minor: int, currency: str) -> str:
    exponent = settlement_currency_minor_unit_exponent(currency)
    amount = Decimal(int(amount_minor)) / (Decimal(10) ** exponent)
    return f"{amount:.{exponent}f}"


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
    current.assert_can_view_promotion_analytics()
    return current


def _offering_capabilities(actor: TenantContext) -> list[BusinessCapability]:
    return [
        item
        for item in list_business_capabilities(actor=actor)
        if item.status == CapabilityStatus.ACTIVE
        and resolve_activity_connector(item.connector_key).supports_offerings
    ]


def _offerings(actor: TenantContext, capabilities: list[BusinessCapability]) -> list[BusinessOffering]:
    rows: list[BusinessOffering] = []
    for capability in capabilities:
        rows.extend(
            item
            for item in list_business_offerings(actor=actor, capability_id=capability.id)
            if item.status == OfferingStatus.ACTIVE
        )
    rows.sort(key=lambda item: (item.title.casefold(), item.id))
    return rows


def build_cockpit_services(*, actor: TenantContext, business_name: str) -> CockpitServicesSnapshot:
    current = _current(actor)
    capabilities = _offering_capabilities(current)
    capability_by_id = {item.id: item for item in capabilities}
    offerings = _offerings(current, capabilities)

    prices: list[admin_ops.OfferingPrice] = []
    can_view_prices = current.role in admin_ops._FINANCE_READ_ROLES
    if can_view_prices:
        prices = admin_ops.list_offering_prices(actor=current)
    price_by_offering = {item.offering_id: item for item in prices}

    can_manage_offerings = True
    try:
        current.assert_can_manage_programs()
    except TenantPermissionDenied:
        can_manage_offerings = False
    can_manage_prices = current.role in admin_ops._FINANCE_WRITE_ROLES

    items: list[CockpitServiceItem] = []
    for offering in offerings:
        capability = capability_by_id[offering.capability_id]
        price = price_by_offering.get(offering.id)
        items.append(
            CockpitServiceItem(
                id=offering.id,
                capability_id=capability.id,
                capability_title=capability.title,
                title=offering.title,
                description=offering.description,
                price_minor=None if price is None else price.amount_minor,
                price_currency=None if price is None else price.currency,
                price_display=(
                    None if price is None else _money_display(price.amount_minor, price.currency)
                ),
                price_input=(
                    None if price is None else _money_input(price.amount_minor, price.currency)
                ),
            )
        )

    return CockpitServicesSnapshot(
        schema_version=_SCHEMA_VERSION,
        business_id=current.business_id,
        business_name=str(business_name),
        capabilities=tuple(
            CockpitServiceCapability(
                id=item.id,
                connector_key=item.connector_key,
                title=item.title,
            )
            for item in capabilities
        ),
        items=tuple(items),
        can_manage_offerings=can_manage_offerings,
        can_manage_prices=can_manage_prices,
        can_view_prices=can_view_prices,
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


def resolve_cockpit_services(
    *, telegram_user_id: int, requested_business_id: str | None = None
) -> CockpitServicesSnapshot:
    actor, business_name = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    return build_cockpit_services(actor=actor, business_name=business_name)


def create_cockpit_service(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    capability_id: str,
    title: str,
    description: str,
) -> BusinessOffering:
    actor, _ = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    actor.assert_can_manage_programs()
    allowed = {item.id for item in _offering_capabilities(actor)}
    if capability_id not in allowed:
        raise ValueError("offering capability is unavailable")
    return create_business_offering(
        actor=actor,
        capability_id=capability_id,
        title=title,
        description=description,
    )


def archive_cockpit_service(
    *, telegram_user_id: int, requested_business_id: str | None, offering_id: str
) -> BusinessOffering:
    actor, _ = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    actor.assert_can_manage_programs()
    return archive_business_offering(actor=actor, offering_id=offering_id)


def set_cockpit_service_price(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    offering_id: str,
    amount: str,
    currency: str,
) -> admin_ops.OfferingPrice:
    actor, _ = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    amount_minor, normalized_currency = _major_to_minor(amount, currency)
    return admin_ops.set_offering_price(
        actor=actor,
        offering_id=offering_id,
        amount_minor=amount_minor,
        currency=normalized_currency,
    )


__all__ = [
    "CockpitServiceCapability",
    "CockpitServiceItem",
    "CockpitServicesSnapshot",
    "archive_cockpit_service",
    "build_cockpit_services",
    "create_cockpit_service",
    "resolve_cockpit_services",
    "set_cockpit_service_price",
]
