from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable

from clientplatform.application.cockpit_action_routing import build_cockpit_section_start_payload

from clientplatform.application.tenancy import (
    get_owner_control_workspace,
    list_accessible_businesses,
    resolve_tenant_context,
)
from clientplatform.domain.tenancy import (
    PlatformRole,
    TenantAccessDenied,
    TenantContext,
    TenantPermissionDenied,
)
from services.accounts.identity import resolve_canonical_user_id


@dataclass(frozen=True, slots=True)
class CockpitNavigationItem:
    id: str
    title: str
    summary: str
    when_to_use: str
    status: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CockpitBusinessOption:
    id: str
    name: str
    role: str
    selected: bool


@dataclass(frozen=True, slots=True)
class CockpitContext:
    user_id: int
    business_id: str | None
    business_name: str | None
    role: str | None
    onboarding_required: bool
    businesses: tuple[CockpitBusinessOption, ...]
    navigation: tuple[CockpitNavigationItem, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _allowed(actor: TenantContext, check: Callable[[], None]) -> bool:
    try:
        check()
    except TenantPermissionDenied:
        return False
    return True


def _nav_item(
    *,
    id: str,
    title: str,
    summary: str,
    when_to_use: str,
    allowed: bool = True,
    planned: bool = False,
) -> CockpitNavigationItem:
    if planned:
        return CockpitNavigationItem(
            id=id,
            title=title,
            summary=summary,
            when_to_use=when_to_use,
            status="planned",
            reason="Этот раздел скоро появится в кабинете. Пока используйте «Сегодня» и быстрые команды в боте.",
        )
    if allowed:
        return CockpitNavigationItem(
            id=id,
            title=title,
            summary=summary,
            when_to_use=when_to_use,
            status="available",
        )
    return CockpitNavigationItem(
        id=id,
        title=title,
        summary=summary,
        when_to_use=when_to_use,
        status="restricted",
        reason="Для Вашей роли этот раздел недоступен. Если он нужен, попросите владельца бизнеса изменить доступ.",
    )


def cockpit_navigation(actor: TenantContext) -> tuple[CockpitNavigationItem, ...]:
    """Build a role-aware UI projection from canonical tenancy assertions.

    This is navigation, not authorization. Every feature endpoint must continue
    to enforce its own canonical application/domain permission boundary.
    """

    can_customers = _allowed(actor, actor.assert_can_view_customer_records)
    can_event_funnel = (
        can_customers
        and _allowed(actor, actor.assert_can_view_outcome_ledger)
        and _allowed(actor, actor.assert_can_view_attribution_spine)
    )
    can_growth = _allowed(actor, actor.assert_can_view_promotion_analytics)
    can_content = _allowed(actor, actor.assert_can_view_programs)
    can_creatives = _allowed(actor, actor.assert_can_manage_promotions)
    can_manage_business = _allowed(actor, actor.assert_can_manage_business)
    can_services = _allowed(actor, actor.assert_can_view_promotion_analytics)
    can_money = actor.role in {
        PlatformRole.OWNER, PlatformRole.ADMINISTRATOR, PlatformRole.MANAGER,
        PlatformRole.MARKETER, PlatformRole.ANALYST,
    }
    # The existing owner-facing team UI is owner-only. Keep Cockpit honest
    # instead of advertising an administrator path that Telegram does not expose.
    can_team = actor.role == PlatformRole.OWNER

    return (
        _nav_item(
            id="home",
            title="Сегодня",
            summary="Главное по бизнесу в одном месте: что требует внимания и что делать дальше.",
            when_to_use="Открывайте сначала, если не знаете, с чего начать.",
        ),
        _nav_item(
            id="customers",
            title="Клиенты",
            summary="Клиенты, история взаимодействий и следующий шаг по каждому человеку.",
            when_to_use="Если нужно найти клиента, понять историю общения или продолжить работу.",
            allowed=can_customers,
        ),
        _nav_item(
            id="calendar",
            title="Записи и свободное время",
            summary="Ближайшие записи, свободное время и встречи.",
            when_to_use="Если нужно посмотреть или организовать ближайшие записи.",
            allowed=can_customers,
        ),
        _nav_item(
            id="sales",
            title="Обращения и продажи",
            summary="Обращения клиентов, сделки, следующий контакт и незавершённые продажи.",
            when_to_use="Если нужно понять, кому ответить, что предложить и где теряется продажа.",
            allowed=can_customers,
        ),
        _nav_item(
            id="services",
            title="Услуги",
            summary="Что Вы продаёте: услуги, предложения, описания и цены.",
            when_to_use="Если нужно добавить услугу, изменить цену или убрать предложение из активных.",
            allowed=can_services,
        ),
        _nav_item(
            id="events",
            title="Вебинары",
            summary="Создание вебинаров, регистрация, участие, предложения и оплаты в одной воронке.",
            when_to_use="Если хотите провести вебинар и видеть путь от рекламы до подтверждённой оплаты.",
            allowed=can_event_funnel,
        ),
        _nav_item(
            id="growth",
            title="Новые клиенты и реклама",
            summary=(
                "Каналы привлечения, реклама и создание визуалов для продвижения."
                if can_creatives
                else "Каналы привлечения, кампании и безопасные действия по росту."
            ),
            when_to_use=(
                "Если хотите привлечь клиентов, создать рекламную картинку или проверить рекламу."
                if can_creatives
                else "Если хотите проверить, что работает в привлечении и рекламе."
            ),
            allowed=can_growth,
        ),
        _nav_item(
            id="creative",
            title="Создать картинку",
            summary="Изображение для рекламы, поста или другого материала — в фирменном стиле бизнеса.",
            when_to_use="Если нужен готовый визуал без отдельного графического редактора.",
            allowed=can_creatives,
        ),
        _nav_item(
            id="content",
            title="Материалы и публикации",
            summary=(
                "Программы, публикации, материалы и создание картинок для контента."
                if can_creatives
                else "Программы, материалы, публикации и контент-план."
            ),
            when_to_use=(
                "Если нужно подготовить публикацию, картинку или материал для клиентов."
                if can_creatives
                else "Если нужно найти или проверить материалы для клиентов."
            ),
            allowed=can_content,
        ),
        _nav_item(
            id="automation",
            title="Автоматические действия",
            summary="Рутинные действия, согласования, ограничения и остановка автоматизации.",
            when_to_use="Если хотите поручить рутину системе или проверить, что ей разрешено делать.",
            allowed=can_manage_business,
        ),
        _nav_item(
            id="money",
            title="Деньги",
            summary="Подтверждённые оплаты, выручка, платящие клиенты и возвраты.",
            when_to_use="Если нужно зафиксировать оплату, посмотреть деньги бизнеса или оформить полный возврат.",
            allowed=can_money,
        ),
        _nav_item(
            id="analytics",
            title="Что приносит результат",
            summary="Результаты, источники и показатели, которые уже считает ClientPlatform.",
            when_to_use="Если нужно понять, что приносит результат и куда смотреть дальше.",
            allowed=can_growth,
        ),
        _nav_item(
            id="connections",
            title="Мессенджеры и подключения",
            summary="Мессенджеры, рекламные и внешние подключения бизнеса.",
            when_to_use="Если нужно подключить, проверить или заменить внешний канал.",
            allowed=can_manage_business,
        ),
        _nav_item(
            id="team",
            title="Сотрудники и доступы",
            summary="Сотрудники бизнеса и их права доступа.",
            when_to_use="Если нужно дать сотруднику доступ или изменить его роль.",
            allowed=can_team,
        ),
        _nav_item(
            id="billing",
            title="Тариф и оплата",
            summary="Тариф, лимиты и использование возможностей платформы.",
            when_to_use="Если нужно будет управлять тарифом или посмотреть использование.",
            planned=True,
        ),
        _nav_item(
            id="settings",
            title="Настройки бизнеса",
            summary="Название, описание деятельности и часовой пояс бизнеса.",
            when_to_use="Если нужно изменить основные данные бизнеса.",
            allowed=can_manage_business,
        ),
    )


def resolve_cockpit_context(
    *,
    telegram_user_id: int,
    requested_business_id: str | None = None,
) -> CockpitContext:
    """Resolve cockpit scope only from verified Telegram identity and canonical tenancy."""

    canonical_user_id = resolve_canonical_user_id(int(telegram_user_id))
    accesses = list_accessible_businesses(user_id=canonical_user_id)
    if not accesses:
        if requested_business_id:
            raise TenantAccessDenied("active business membership was not found")
        return CockpitContext(
            user_id=canonical_user_id,
            business_id=None,
            business_name=None,
            role=None,
            onboarding_required=True,
            businesses=(),
            navigation=(),
        )

    selected_id = str(requested_business_id or "").strip() or None
    if selected_id is None:
        selected_id = get_owner_control_workspace(
            user_id=canonical_user_id,
            platform="telegram",
        )
    if selected_id is None:
        selected_id = accesses[0].business.id

    actor = resolve_tenant_context(
        user_id=canonical_user_id,
        business_id=selected_id,
    )
    selected_access = next(
        (item for item in accesses if item.business.id == actor.business_id),
        None,
    )
    if selected_access is None:
        raise TenantAccessDenied("selected business is not in the accessible business set")

    businesses = tuple(
        CockpitBusinessOption(
            id=item.business.id,
            name=item.business.name,
            role=item.membership.role.value,
            selected=item.business.id == actor.business_id,
        )
        for item in accesses
    )
    return CockpitContext(
        user_id=actor.user_id,
        business_id=actor.business_id,
        business_name=selected_access.business.name,
        role=actor.role.value,
        onboarding_required=False,
        businesses=businesses,
        navigation=cockpit_navigation(actor),
    )


def resolve_cockpit_section_start_payload(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    section: str,
) -> str:
    """Resolve one role-authorized Cockpit section to the canonical Telegram surface."""

    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if context.onboarding_required or context.business_id is None:
        raise TenantAccessDenied("active business membership was not found")
    normalized = str(section or "").strip().lower()
    if normalized == "reactivation":
        actor = resolve_tenant_context(
            user_id=context.user_id,
            business_id=context.business_id,
        )
        if actor.role not in {
            PlatformRole.OWNER,
            PlatformRole.ADMINISTRATOR,
            PlatformRole.MANAGER,
            PlatformRole.SUPPORT,
        }:
            raise TenantPermissionDenied("reactivation action is unavailable for this role")
        return build_cockpit_section_start_payload(
            business_id=context.business_id,
            section=normalized,
        )
    if normalized == "ad-spend":
        actor = resolve_tenant_context(
            user_id=context.user_id,
            business_id=context.business_id,
        )
        if actor.role != PlatformRole.OWNER:
            raise TenantPermissionDenied("ad spend action is owner-only")
        return build_cockpit_section_start_payload(
            business_id=context.business_id,
            section=normalized,
        )
    item = next((entry for entry in context.navigation if entry.id == normalized), None)
    if item is None:
        raise ValueError("unsupported cockpit section")
    if item.status != "available":
        raise TenantPermissionDenied("cockpit section is not available for this role")
    return build_cockpit_section_start_payload(
        business_id=context.business_id,
        section=normalized,
    )


__all__ = [
    "CockpitBusinessOption",
    "CockpitContext",
    "CockpitNavigationItem",
    "cockpit_navigation",
    "resolve_cockpit_context",
    "resolve_cockpit_section_start_payload",
]
