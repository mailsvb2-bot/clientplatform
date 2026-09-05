from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Callable

from clientplatform.application.cockpit import resolve_cockpit_context
from clientplatform.application.customer_timeline import (
    CustomerTimeline,
    get_customer_timeline,
)
from clientplatform.application.customers import get_customer, search_customers
from clientplatform.application.growth_cockpit import GrowthAction, get_customer_work_actions
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.customers import (
    Customer,
    CustomerIdentity,
    CustomerIdentityStatus,
    CustomerPlatform,
    CustomerRecord,
)
from clientplatform.domain.money import settlement_currency_minor_unit_exponent
from clientplatform.domain.tenancy import (
    TenantAccessDenied,
    TenantContext,
    TenantPermissionDenied,
)

_SCHEMA_VERSION = "2026-09-05.v1"
_PLATFORM_LABELS = {
    CustomerPlatform.TELEGRAM: "Telegram",
    CustomerPlatform.VK: "ВКонтакте",
    CustomerPlatform.MAX: "MAX",
    CustomerPlatform.EMAIL: "Email",
    CustomerPlatform.PHONE: "Телефон",
    CustomerPlatform.WEB: "Сайт",
    CustomerPlatform.INTERNAL: "Внутренний профиль",
}


@dataclass(frozen=True, slots=True)
class CockpitCustomerListItem:
    customer_id: str
    display_name: str
    status: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CockpitCustomerPage:
    schema_version: str
    business_id: str
    role: str
    query: str
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None
    previous_offset: int | None
    items: tuple[CockpitCustomerListItem, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CockpitCustomerContact:
    platform: str
    label: str
    display: str


@dataclass(frozen=True, slots=True)
class CockpitCustomerTimelineItem:
    occurred_at: str
    title: str
    detail: str | None
    money: str | None


@dataclass(frozen=True, slots=True)
class CockpitCustomerAction:
    title: str
    reason: str
    section: str
    action_key: str


@dataclass(frozen=True, slots=True)
class CockpitCustomerDetail:
    schema_version: str
    business_id: str
    role: str
    customer_id: str
    display_name: str
    status: str
    created_at: str
    updated_at: str
    contacts: tuple[CockpitCustomerContact, ...]
    timeline: tuple[CockpitCustomerTimelineItem, ...]
    next_action: CockpitCustomerAction | None
    limitations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _display_name(record: CustomerRecord) -> str:
    if record.customer.display_name:
        return record.customer.display_name
    for identity in record.identities:
        if identity.status == CustomerIdentityStatus.ACTIVE and identity.display_name:
            return identity.display_name
    return "Клиент"


def _safe_contact(identity: CustomerIdentity) -> CockpitCustomerContact | None:
    if identity.status != CustomerIdentityStatus.ACTIVE:
        return None
    label = _PLATFORM_LABELS[identity.platform]
    if identity.username:
        display = identity.username
        if identity.platform in {CustomerPlatform.TELEGRAM, CustomerPlatform.VK}:
            display = "@" + display.lstrip("@")
    elif identity.platform == CustomerPlatform.PHONE:
        display = "•••• " + identity.external_subject[-4:]
    elif identity.platform == CustomerPlatform.EMAIL:
        _, separator, domain = identity.external_subject.partition("@")
        display = f"•••@{domain}" if separator and domain else "Email подключён"
    elif identity.display_name:
        display = identity.display_name
    else:
        display = f"{label} подключён"
    return CockpitCustomerContact(
        platform=identity.platform.value,
        label=label,
        display=display,
    )


def _money_text(amount_minor: int | None, currency: str | None) -> str | None:
    if amount_minor is None or currency is None:
        return None
    exponent = settlement_currency_minor_unit_exponent(currency)
    amount = Decimal(amount_minor) / (Decimal(10) ** exponent)
    rendered = f"{amount:,.{exponent}f}" if exponent else f"{amount:,.0f}"
    return f"{rendered.replace(',', ' ').replace('.', ',')} {currency}"


def _timeline_items(timeline: CustomerTimeline) -> tuple[CockpitCustomerTimelineItem, ...]:
    return tuple(
        CockpitCustomerTimelineItem(
            occurred_at=item.occurred_at.isoformat(),
            title=item.title,
            detail=item.detail,
            money=_money_text(item.amount_minor, item.currency),
        )
        for item in timeline.entries
    )


def _customer_action(action: GrowthAction | None) -> CockpitCustomerAction | None:
    if action is None:
        return None
    return CockpitCustomerAction(
        title=action.title,
        reason=action.reason,
        section="sales",
        action_key=action.action_key,
    )


def build_cockpit_customer_page(
    *,
    actor: TenantContext,
    query: str | None = None,
    limit: int = 20,
    offset: int = 0,
    loader: Callable[..., tuple[list[Customer], bool]] = search_customers,
) -> CockpitCustomerPage:
    current_query = " ".join(str(query or "").split()).strip()
    customers, has_more = loader(
        actor=actor,
        query=current_query,
        limit=limit,
        offset=offset,
    )
    items = tuple(
        CockpitCustomerListItem(
            customer_id=item.id,
            display_name=item.display_name or "Клиент",
            status=item.status.value,
            updated_at=item.updated_at,
        )
        for item in customers
    )
    return CockpitCustomerPage(
        schema_version=_SCHEMA_VERSION,
        business_id=actor.business_id,
        role=actor.role.value,
        query=current_query,
        limit=limit,
        offset=offset,
        has_more=has_more,
        next_offset=offset + limit if has_more else None,
        previous_offset=max(0, offset - limit) if offset else None,
        items=items,
    )


def build_cockpit_customer_detail(
    *,
    actor: TenantContext,
    customer_id: str,
    timeline_limit: int = 20,
    record_loader: Callable[..., CustomerRecord] = get_customer,
    timeline_loader: Callable[..., CustomerTimeline] = get_customer_timeline,
    action_loader: Callable[..., tuple[GrowthAction, ...]] = get_customer_work_actions,
) -> CockpitCustomerDetail:
    if isinstance(timeline_limit, bool) or not isinstance(timeline_limit, int):
        raise ValueError("timeline_limit must be an integer between 1 and 50")
    if not 1 <= timeline_limit <= 50:
        raise ValueError("timeline_limit must be an integer between 1 and 50")
    record = record_loader(actor=actor, customer_id=customer_id)
    limitations: list[str] = []
    timeline_items: tuple[CockpitCustomerTimelineItem, ...] = ()
    try:
        timeline = timeline_loader(
            actor=actor,
            customer_id=record.customer.id,
            limit=timeline_limit,
        )
        timeline_items = _timeline_items(timeline)
    except (TenantAccessDenied, TenantPermissionDenied):
        raise
    except (OSError, RuntimeError, ValueError):
        limitations.append("timeline_unavailable")

    next_action = None
    try:
        actions = action_loader(
            actor=actor,
            customer_id=record.customer.id,
            limit=1,
        )
        next_action = _customer_action(actions[0] if actions else None)
    except (TenantAccessDenied, TenantPermissionDenied):
        raise
    except (OSError, RuntimeError, ValueError):
        limitations.append("customer_work_unavailable")

    contacts = tuple(
        contact
        for identity in record.identities
        if (contact := _safe_contact(identity)) is not None
    )
    return CockpitCustomerDetail(
        schema_version=_SCHEMA_VERSION,
        business_id=actor.business_id,
        role=actor.role.value,
        customer_id=record.customer.id,
        display_name=_display_name(record),
        status=record.customer.status.value,
        created_at=record.customer.created_at,
        updated_at=record.customer.updated_at,
        contacts=contacts,
        timeline=timeline_items,
        next_action=next_action,
        limitations=tuple(dict.fromkeys(limitations)),
    )

def _resolve_actor(
    *, telegram_user_id: int, requested_business_id: str | None
) -> TenantContext:
    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if context.onboarding_required or context.business_id is None:
        raise TenantAccessDenied("active business membership was not found")
    return resolve_tenant_context(
        user_id=context.user_id,
        business_id=context.business_id,
    )


def resolve_cockpit_customer_page(
    *,
    telegram_user_id: int,
    requested_business_id: str | None = None,
    query: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> CockpitCustomerPage:
    actor = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    return build_cockpit_customer_page(
        actor=actor, query=query, limit=limit, offset=offset
    )


def resolve_cockpit_customer_detail(
    *,
    telegram_user_id: int,
    customer_id: str,
    requested_business_id: str | None = None,
    timeline_limit: int = 20,
) -> CockpitCustomerDetail:
    actor = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    return build_cockpit_customer_detail(
        actor=actor,
        customer_id=customer_id,
        timeline_limit=timeline_limit,
    )


__all__ = [
    "CockpitCustomerAction",
    "CockpitCustomerContact",
    "CockpitCustomerDetail",
    "CockpitCustomerListItem",
    "CockpitCustomerPage",
    "CockpitCustomerTimelineItem",
    "build_cockpit_customer_detail",
    "build_cockpit_customer_page",
    "resolve_cockpit_customer_detail",
    "resolve_cockpit_customer_page",
]
