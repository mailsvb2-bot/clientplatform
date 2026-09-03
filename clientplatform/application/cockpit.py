from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable

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
            reason="Раздел уже предусмотрен архитектурой и будет подключаться без создания второго источника данных.",
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
        reason="В Вашей текущей роли этот раздел недоступен. Права проверяет сервер, а не интерфейс.",
    )


def cockpit_navigation(actor: TenantContext) -> tuple[CockpitNavigationItem, ...]:
    """Build a role-aware UI projection from canonical tenancy assertions.

    This is navigation, not authorization. Every feature endpoint must continue
    to enforce its own canonical application/domain permission boundary.
    """

    can_customers = _allowed(actor, actor.assert_can_view_customer_records)
    can_growth = _allowed(actor, actor.assert_can_view_promotion_analytics)
    can_content = _allowed(actor, actor.assert_can_view_programs)
    can_manage_business = _allowed(actor, actor.assert_can_manage_business)
    can_team = _allowed(actor, lambda: actor.assert_can_manage_members(PlatformRole.MANAGER))

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
            title="Календарь и записи",
            summary="Записи, слоты и ближайшие встречи без отдельного календарного мозга.",
            when_to_use="Если нужно посмотреть или организовать ближайшие записи.",
            allowed=can_customers,
        ),
        _nav_item(
            id="sales",
            title="Продажи",
            summary="Лиды, сделки, следующий контакт и незавершённые продажи.",
            when_to_use="Если нужно понять, кому ответить, что предложить и где теряется продажа.",
            allowed=can_customers,
        ),
        _nav_item(
            id="growth",
            title="Рост и реклама",
            summary="Каналы привлечения, кампании и безопасные действия по росту.",
            when_to_use="Если хотите привлечь больше клиентов или проверить, что работает в рекламе.",
            allowed=can_growth,
        ),
        _nav_item(
            id="content",
            title="Контент и материалы",
            summary="Программы, материалы, публикации и контент-план из канонических данных.",
            when_to_use="Если нужно подготовить, найти или запланировать материалы для клиентов.",
            allowed=can_content,
        ),
        _nav_item(
            id="automation",
            title="Автоматизация",
            summary="Разрешённая автоматическая работа, лимиты, согласования и остановка.",
            when_to_use="Если хотите поручить рутину системе или проверить, что ей разрешено делать.",
            allowed=can_manage_business,
        ),
        _nav_item(
            id="analytics",
            title="Аналитика",
            summary="Результаты, источники и показатели, которые уже считает ClientPlatform.",
            when_to_use="Если нужно понять, что приносит результат и куда смотреть дальше.",
            allowed=can_growth,
        ),
        _nav_item(
            id="connections",
            title="Подключения",
            summary="Мессенджеры, рекламные и внешние подключения бизнеса.",
            when_to_use="Если нужно подключить, проверить или заменить внешний канал.",
            allowed=can_manage_business,
        ),
        _nav_item(
            id="team",
            title="Команда и роли",
            summary="Участники бизнеса и их разрешённые роли без скрытого повышения прав.",
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
            title="Настройки и приватность",
            summary="Настройки бизнеса, данные, экспорт и приватность.",
            when_to_use="Если нужно изменить настройки бизнеса или управлять своими данными.",
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


def resolve_cockpit_actor(
    *,
    telegram_user_id: int,
    requested_business_id: str | None = None,
) -> TenantContext:
    """Resolve and then live-recheck the canonical actor for a cockpit content request."""

    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if context.business_id is None:
        raise TenantAccessDenied("active business membership was not found")
    return resolve_tenant_context(user_id=context.user_id, business_id=context.business_id)


__all__ = [
    "CockpitBusinessOption",
    "CockpitContext",
    "CockpitNavigationItem",
    "cockpit_navigation",
    "resolve_cockpit_actor",
    "resolve_cockpit_context",
]
