from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from clientplatform.application.bookings import list_booking_slots
from clientplatform.application.connections import list_connections
from clientplatform.application.control import business_delivery_summary
from clientplatform.application.customers import list_customers
from clientplatform.application.programs import list_programs
from clientplatform.application.tenancy import (
    list_accessible_businesses,
    resolve_tenant_context,
)
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from clientplatform.domain.messenger_channels import MessengerIngressRoute
from clientplatform.domain.tenancy import (
    PlatformRole,
    TenantAccessDenied,
    TenantContext,
    TenantPermissionDenied,
)
from clientplatform.infrastructure import DispatchOutboxRepository
from services.accounts.identity import resolve_account_for_identity
from services.db import get_db, get_db_ro
from services.messenger.bridge import (
    consume_bridge_token_and_link,
    resolve_bridge_token,
)
from services.messenger.entrypoints import parse_start_payload


log = logging.getLogger(__name__)

_CUSTOMER_RECORD_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.SUPPORT,
    }
)
_CONNECTION_ROLES = frozenset({PlatformRole.OWNER, PlatformRole.ADMINISTRATOR})
_ROLE_LABELS = {
    PlatformRole.OWNER: "Владелец",
    PlatformRole.ADMINISTRATOR: "Администратор",
    PlatformRole.MANAGER: "Менеджер",
    PlatformRole.CONTENT_MANAGER: "Контент-менеджер",
    PlatformRole.MARKETER: "Маркетолог",
    PlatformRole.ANALYST: "Аналитик",
    PlatformRole.SUPPORT: "Поддержка",
}
_ALIASES = {
    "start": "menu",
    "/start": "menu",
    "меню": "menu",
    "кабинет": "menu",
    "админ": "menu",
    "/admin": "menu",
    "сегодня": "today",
    "клиенты": "customers",
    "записи": "bookings",
    "программы": "programs",
    "мессенджеры": "messengers",
}
_COMMAND_PREFIX = "cpm:"
NativeSetupCommandIssuer = Callable[
    [TenantContext, ConnectionPlatform, str],
    str,
]


class NativeMemberBridgeRejected(RuntimeError):
    """A member link command cannot be admitted into this business."""


@dataclass(frozen=True, slots=True)
class NativeMemberResolution:
    actor: TenantContext
    account_id: int
    linked: bool = False


@dataclass(frozen=True, slots=True)
class ParsedMemberInteraction:
    action: str


def _compact(value: object) -> str:
    return " ".join(
        str(value or "").strip().casefold().replace("ё", "е").split()
    )


def _bridge_token(value: object) -> str | None:
    raw = " ".join(str(value or "").strip().split())
    lowered = raw.casefold()
    payload = raw
    if lowered.startswith("/start ") or lowered.startswith("start "):
        payload = raw.split(maxsplit=1)[1].strip()
    parsed = parse_start_payload(payload)
    if parsed.kind != "bridge" or not parsed.value:
        return None
    return parsed.value


def _active_customer_identity_exists(
    *,
    business_id: str,
    platform: str,
    external_subject: str,
) -> bool:
    with get_db_ro() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM customer_identities ci
            JOIN customers c
              ON c.id=ci.customer_id AND c.business_id=ci.business_id
             AND c.status='active'
            WHERE ci.business_id=? AND ci.platform=?
              AND ci.external_subject=? AND ci.status='active'
            LIMIT 1
            """,
            (business_id, platform, external_subject),
        ).fetchone()
    return row is not None


def _member_context(account_id: int, business_id: str) -> TenantContext | None:
    try:
        return resolve_tenant_context(user_id=int(account_id), business_id=business_id)
    except TenantAccessDenied:
        return None


def resolve_native_member(
    *,
    route: MessengerIngressRoute,
    external_subject: str,
    raw_text: object,
    display_name: str | None = None,
) -> NativeMemberResolution | None:
    """Resolve staff before Customer creation, optionally consuming an explicit bridge."""

    subject = str(external_subject or "").strip()
    if not subject:
        return None
    platform = route.platform.value

    existing_account = resolve_account_for_identity(
        platform,
        subject,
        display_name=display_name,
        allow_create=False,
    )
    if existing_account is not None:
        actor = _member_context(int(existing_account), route.business_id)
        if actor is not None:
            return NativeMemberResolution(
                actor=actor,
                account_id=int(existing_account),
                linked=False,
            )

    token = _bridge_token(raw_text)
    if token is None:
        return None
    if _active_customer_identity_exists(
        business_id=route.business_id,
        platform=platform,
        external_subject=subject,
    ):
        raise NativeMemberBridgeRejected(
            "эта учётная запись уже используется как клиент этого бизнеса"
        )

    preview = resolve_bridge_token(token)
    if preview is None or preview.consumed:
        raise NativeMemberBridgeRejected("ссылка входа устарела или уже использована")
    if preview.target_platform and preview.target_platform != platform:
        raise NativeMemberBridgeRejected("ссылка входа предназначена для другого мессенджера")

    actor = _member_context(int(preview.canonical_user_id), route.business_id)
    if actor is None:
        raise NativeMemberBridgeRejected(
            "ссылка не подтверждает доступ к этому бизнесу"
        )

    consumed = consume_bridge_token_and_link(
        token,
        platform=platform,
        external_user_id=subject,
        display_name=display_name,
    )
    if (
        consumed is None
        or int(consumed.canonical_user_id) != int(preview.canonical_user_id)
    ):
        raise NativeMemberBridgeRejected(
            "ссылка входа уже была использована; создайте новую"
        )

    current = _member_context(int(consumed.canonical_user_id), route.business_id)
    if current is None:
        raise NativeMemberBridgeRejected("доступ сотрудника больше не активен")
    return NativeMemberResolution(
        actor=current,
        account_id=int(consumed.canonical_user_id),
        linked=True,
    )


def parse_native_member_interaction(value: object) -> ParsedMemberInteraction:
    raw = str(value or "").strip()
    alias = _ALIASES.get(_compact(raw))
    if alias is not None:
        return ParsedMemberInteraction(alias)
    if raw.casefold().startswith(_COMMAND_PREFIX):
        action = raw[len(_COMMAND_PREFIX) :].split(":", 1)[0].strip().casefold()
        if action in {
            "menu",
            "today",
            "customers",
            "bookings",
            "programs",
            "messengers",
            "connect-vk",
            "connect-max",
        }:
            return ParsedMemberInteraction(action)
    return ParsedMemberInteraction("menu")


def _button(label: str, command: str) -> CustomerInteractionButton:
    return CustomerInteractionButton(label=label[:40], command=command)


def _menu_rows(role: PlatformRole) -> tuple[tuple[CustomerInteractionButton, ...], ...]:
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if role in _CUSTOMER_RECORD_ROLES:
        rows.extend(
            [
                (_button("📊 Сегодня", "cpm:today"),),
                (_button("👥 Клиенты", "cpm:customers"),),
                (_button("📅 Записи", "cpm:bookings"),),
            ]
        )
    rows.append((_button("📚 Программы", "cpm:programs"),))
    if role in _CONNECTION_ROLES:
        rows.append((_button("💬 Мессенджеры", "cpm:messengers"),))
    return tuple(rows)


def _business_name(actor: TenantContext) -> str:
    accesses = list_accessible_businesses(user_id=actor.user_id)
    access = next(
        (
            item
            for item in accesses
            if str(item.business.id) == str(actor.business_id)
        ),
        None,
    )
    return str(access.business.name) if access is not None else "ClientPlatform"


def _menu_message(
    actor: TenantContext,
    *,
    linked: bool,
) -> CustomerInteractionMessage:
    heading = (
        "✅ Этот мессенджер подключён к Вашему рабочему аккаунту.\n\n"
        if linked
        else ""
    )
    return CustomerInteractionMessage(
        text=(
            heading
            + "ClientPlatform · рабочий кабинет\n\n"
            + f"Бизнес: {_business_name(actor)}\n"
            + f"Роль: {_ROLE_LABELS.get(actor.role, actor.role.value)}\n\n"
            + "Выберите раздел:"
        ),
        rows=_menu_rows(actor.role),
    )


def _back_row() -> tuple[CustomerInteractionButton, ...]:
    return (_button("🏠 Рабочий кабинет", "cpm:menu"),)


def _permission_message() -> CustomerInteractionMessage:
    return CustomerInteractionMessage(
        text="Для Вашей роли этот раздел недоступен.",
        rows=(_back_row(),),
    )


def _today_message(actor: TenantContext) -> CustomerInteractionMessage:
    summary = business_delivery_summary(actor=actor)
    return CustomerInteractionMessage(
        text=(
            "Сегодня · ClientPlatform\n\n"
            + f"Клиентов: {summary.customers}\n"
            + f"Программ: {summary.programs}\n"
            + f"В очереди отправки: {summary.dispatch_pending}\n"
            + f"Отправлено: {summary.dispatch_sent}\n"
            + f"Требуют внимания: {summary.dispatch_attention}"
        ),
        rows=(
            (_button("👥 Клиенты", "cpm:customers"),),
            (_button("📅 Записи", "cpm:bookings"),),
            _back_row(),
        ),
    )


def _customers_message(actor: TenantContext) -> CustomerInteractionMessage:
    customers = list_customers(actor=actor)
    if not customers:
        text = "Активных клиентов пока нет."
    else:
        lines = []
        for customer in customers[:12]:
            name = str(customer.display_name or "").strip() or f"Клиент {customer.id[:8]}"
            lines.append(f"• {name}")
        suffix = (
            f"\n\nПоказаны первые 12 из {len(customers)}."
            if len(customers) > 12
            else ""
        )
        text = "Клиенты\n\n" + "\n".join(lines) + suffix
    return CustomerInteractionMessage(text=text, rows=(_back_row(),))


def _bookings_message(actor: TenantContext) -> CustomerInteractionMessage:
    slots = list_booking_slots(actor=actor, include_unavailable=False)
    if not slots:
        text = "Открытых слотов для записи сейчас нет."
    else:
        lines = [
            f"• {item.offering_title} — {item.local_start}"
            for item in slots[:10]
        ]
        suffix = (
            f"\n\nПоказаны первые 10 из {len(slots)}."
            if len(slots) > 10
            else ""
        )
        text = "Открытая запись\n\n" + "\n".join(lines) + suffix
    return CustomerInteractionMessage(text=text, rows=(_back_row(),))


def _programs_message(actor: TenantContext) -> CustomerInteractionMessage:
    programs = list_programs(actor=actor)
    if not programs:
        text = "Программ пока нет."
    else:
        lines = [
            f"• {item.title} — {item.status.value}"
            for item in programs[:12]
        ]
        suffix = (
            f"\n\nПоказаны первые 12 из {len(programs)}."
            if len(programs) > 12
            else ""
        )
        text = "Программы\n\n" + "\n".join(lines) + suffix
    return CustomerInteractionMessage(text=text, rows=(_back_row(),))


def _messengers_message(
    actor: TenantContext,
    *,
    setup_available: bool,
) -> CustomerInteractionMessage:
    connections = list_connections(actor=actor)
    if not connections:
        text = "Подключённых мессенджеров пока нет."
    else:
        icons = {"telegram": "✈️", "vk": "🔵", "max": "🟣"}
        lines = [
            f"{icons.get(item.platform.value, '•')} "
            + f"{item.platform.value.upper()} — {item.status.value}"
            for item in connections
        ]
        text = "Мессенджеры\n\n" + "\n".join(lines)
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if actor.role in _CONNECTION_ROLES and setup_available:
        rows.extend(
            [
                (_button("🔵 Подключить ВКонтакте", "cpm:connect-vk"),),
                (_button("🟣 Подключить MAX", "cpm:connect-max"),),
            ]
        )
    rows.append(_back_row())
    return CustomerInteractionMessage(text=text, rows=tuple(rows))


def _setup_message(
    actor: TenantContext,
    *,
    platform: ConnectionPlatform,
    setup_issuer: NativeSetupCommandIssuer | None,
    setup_key: str,
) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    if setup_issuer is None:
        return CustomerInteractionMessage(
            text="Защищённая настройка мессенджера сейчас недоступна.",
            rows=(_back_row(),),
        )
    try:
        command = setup_issuer(actor, platform, setup_key)
    except (RuntimeError, ValueError):
        log.exception(
            "Native messenger setup command issuance failed",
            extra={
                "business_id": actor.business_id,
                "member_user_id": actor.user_id,
                "platform": platform.value,
            },
        )
        return CustomerInteractionMessage(
            text=(
                "Не удалось подготовить защищённую настройку. "
                "Повторите попытку позже."
            ),
            rows=(_back_row(),),
        )
    channel_name = "ВКонтакте" if platform == ConnectionPlatform.VK else "MAX"
    return CustomerInteractionMessage(
        text=(
            f"Подключение {channel_name}\n\n"
            "Кнопка ниже откроет защищённую HTTPS-страницу ClientPlatform. "
            "Ссылка действует ограниченное время и предназначена только для этого бизнеса.\n\n"
            "Токен провайдера вводите только на этой странице — не отправляйте его сообщением в мессенджере."
        ),
        rows=(
            (_button("🔐 Открыть защищённую настройку", command),),
            _back_row(),
        ),
    )


def _render(
    actor: TenantContext,
    parsed: ParsedMemberInteraction,
    *,
    linked: bool,
    setup_issuer: NativeSetupCommandIssuer | None,
    setup_key: str,
) -> CustomerInteractionMessage:
    if parsed.action == "menu":
        return _menu_message(actor, linked=linked)
    try:
        if parsed.action == "today":
            return _today_message(actor)
        if parsed.action == "customers":
            return _customers_message(actor)
        if parsed.action == "bookings":
            return _bookings_message(actor)
        if parsed.action == "programs":
            return _programs_message(actor)
        if parsed.action == "messengers":
            return _messengers_message(
                actor,
                setup_available=setup_issuer is not None,
            )
        if parsed.action == "connect-vk":
            return _setup_message(
                actor,
                platform=ConnectionPlatform.VK,
                setup_issuer=setup_issuer,
                setup_key=setup_key,
            )
        if parsed.action == "connect-max":
            return _setup_message(
                actor,
                platform=ConnectionPlatform.MAX,
                setup_issuer=setup_issuer,
                setup_key=setup_key,
            )
    except TenantPermissionDenied:
        return _permission_message()
    return _menu_message(actor, linked=linked)


def process_native_member_interaction(
    *,
    route: MessengerIngressRoute,
    resolution: NativeMemberResolution,
    external_subject: str,
    raw_text: object,
    provider_event_id: str,
    setup_issuer: NativeSetupCommandIssuer | None = None,
) -> Any:
    actor = resolve_tenant_context(
        user_id=resolution.actor.user_id,
        business_id=route.business_id,
    )
    parsed = parse_native_member_interaction(raw_text)
    interaction_key = (
        f"route:{route.id}:event:{provider_event_id}:"
        + f"member:{actor.user_id}:action:{parsed.action}"
    )
    interaction = _render(
        actor,
        parsed,
        linked=resolution.linked,
        setup_issuer=setup_issuer,
        setup_key=interaction_key,
    )
    with get_db() as conn:
        return DispatchOutboxRepository(conn).materialize_member_interaction(
            business_id=route.business_id,
            connection_id=route.connection_id,
            member_user_id=actor.user_id,
            platform=route.platform.value,
            external_subject=external_subject,
            interaction=interaction,
            interaction_key=interaction_key,
        )


__all__ = [
    "NativeMemberBridgeRejected",
    "NativeMemberResolution",
    "NativeSetupCommandIssuer",
    "parse_native_member_interaction",
    "process_native_member_interaction",
    "resolve_native_member",
]
