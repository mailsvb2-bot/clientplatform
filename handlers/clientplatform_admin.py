from __future__ import annotations

"""Metrotherapy-parity, tenant-safe administration for ClientPlatform businesses."""

import asyncio
import importlib
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.activity import (
    get_business_profile,
    list_business_capabilities,
)
from clientplatform.application.admin_ops import (
    format_publication_calendar_lines,
    get_publication_calendar_projection,
)
from clientplatform.application.bookings import list_booking_slots
from clientplatform.application.messenger_switching import (
    available_staff_messenger_switches,
    build_staff_switch_command,
)
from clientplatform.application.control import (
    business_connection_statuses,
    business_delivery_summary,
)
from clientplatform.application.native_messenger_setup import (
    issue_native_messenger_setup,
)
from clientplatform.application.customer_timeline import (
    format_customer_timeline_lines,
    get_customer_timeline,
)
from clientplatform.application.customers import get_customer, list_customers
from clientplatform.application.programs import list_programs
from clientplatform.application.progress import list_business_program_progress
from clientplatform.application.tenancy import (
    grant_business_member,
    list_accessible_businesses,
    revoke_business_member,
)
from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import (
    PlatformRole,
    TenantContext,
    TenantPermissionDenied,
    TenancyError,
)
from clientplatform.infrastructure import TenancyRepository
from clientplatform.runtime.messenger_switch_links import StaffMessengerSwitchLinkService
from config.settings import settings
from services.db import get_db_ro

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_admin")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

log = logging.getLogger(__name__)


def _native_messenger_setup_ingress_enabled() -> bool:
    return (
        os.getenv("CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}

_ROLE_LABELS = {
    PlatformRole.OWNER: "Владелец",
    PlatformRole.ADMINISTRATOR: "Администратор",
    PlatformRole.MANAGER: "Менеджер",
    PlatformRole.CONTENT_MANAGER: "Контент-менеджер",
    PlatformRole.MARKETER: "Маркетолог",
    PlatformRole.ANALYST: "Аналитик",
    PlatformRole.SUPPORT: "Поддержка",
}
_ROLE_CODES = {
    "mgr": PlatformRole.MANAGER,
    "content": PlatformRole.CONTENT_MANAGER,
    "marketing": PlatformRole.MARKETER,
    "analytics": PlatformRole.ANALYST,
    "support": PlatformRole.SUPPORT,
}
_ROLE_PERMISSIONS = {
    PlatformRole.OWNER: (
        "Все разделы и настройки",
        "Сотрудники, роли и доступы",
        "Клиенты, программы и отправки",
        "Подключения и автоматизация",
    ),
    PlatformRole.ADMINISTRATOR: (
        "Управление бизнесом",
        "Сотрудники кроме владельцев и администраторов",
        "Клиенты, программы и отправки",
        "Аналитика и подключения",
    ),
    PlatformRole.MANAGER: (
        "Клиенты и записи",
        "Программы и отправки",
        "Операционная аналитика",
    ),
    PlatformRole.CONTENT_MANAGER: (
        "Программы и материалы",
        "Предложения и тексты",
        "Публикации",
    ),
    PlatformRole.MARKETER: (
        "Воронки и сегменты",
        "Предложения и тексты",
        "Маркетинговая аналитика",
    ),
    PlatformRole.ANALYST: (
        "Отчёты и аналитика",
        "Воронки и удержание",
        "Просмотр результатов",
    ),
    PlatformRole.SUPPORT: (
        "Клиенты",
        "Проблемные отправки",
        "Подключения и поддержка",
    ),
}

_SUPPORT_ROLES = {
    PlatformRole.OWNER,
    PlatformRole.ADMINISTRATOR,
    PlatformRole.MANAGER,
    PlatformRole.SUPPORT,
}
_MARKETING_ROLES = {
    PlatformRole.OWNER,
    PlatformRole.ADMINISTRATOR,
    PlatformRole.MANAGER,
    PlatformRole.MARKETER,
    PlatformRole.ANALYST,
}
_CONTENT_ROLES = {
    PlatformRole.OWNER,
    PlatformRole.ADMINISTRATOR,
    PlatformRole.MANAGER,
    PlatformRole.CONTENT_MANAGER,
    PlatformRole.MARKETER,
}
_AUTOMATION_ROLES = {
    PlatformRole.OWNER,
    PlatformRole.ADMINISTRATOR,
    PlatformRole.MANAGER,
    PlatformRole.MARKETER,
}
_ADMIN_ROLES = {
    PlatformRole.OWNER,
    PlatformRole.ADMINISTRATOR,
}
_OWNER_ROLES = {PlatformRole.OWNER}

_SECTION_ROLES = {
    "today": _SUPPORT_ROLES,
    "today-full": _SUPPORT_ROLES,
    "customers": _SUPPORT_ROLES,
    "customer-list": _SUPPORT_ROLES,
    "customer": _SUPPORT_ROLES,
    "behavior": _SUPPORT_ROLES,
    "messengers": _SUPPORT_ROLES,
    "messenger-connect": _ADMIN_ROLES,
    "attention": _SUPPORT_ROLES,
    "autopilot": _AUTOMATION_ROLES,
    "publications": _CONTENT_ROLES,
    "funnel": _MARKETING_ROLES,
    "money": _MARKETING_ROLES,
    "payments": _MARKETING_ROLES,
    "segments": _MARKETING_ROLES,
    "offers": _CONTENT_ROLES | _MARKETING_ROLES,
    "copy": _CONTENT_ROLES | _MARKETING_ROLES,
    "prices": _MARKETING_ROLES,
    "release": _ADMIN_ROLES,
    "invites": _ADMIN_ROLES,
    "funnel2": _ADMIN_ROLES,
    "retention": _ADMIN_ROLES,
    "recent": _ADMIN_ROLES,
    "system": _ADMIN_ROLES,
    "tariff": _OWNER_ROLES,
    "add-member": _OWNER_ROLES,
    "add-role": _OWNER_ROLES,
    "members": _OWNER_ROLES,
    "member": _OWNER_ROLES,
    "member-role": _OWNER_ROLES,
    "member-revoke": _OWNER_ROLES,
    "permissions": _OWNER_ROLES,
    "formats": _ADMIN_ROLES,
    "formats-edit": _ADMIN_ROLES,
    "rename": _ADMIN_ROLES,
}


class ClientPlatformAdminState(StatesGroup):
    waiting_member_user = State()


@dataclass(frozen=True, slots=True)
class AdminContext:
    user_id: int
    business_id: str
    business_name: str
    actor: TenantContext
    role: PlatformRole

    @property
    def business_token(self) -> str:
        return control._uuid_token(self.business_id)


def _role_label(role: PlatformRole) -> str:
    return _ROLE_LABELS.get(role, role.value)


def _callback(ctx: AdminContext, action: str, *payload: object) -> str:
    tail = ":".join(str(item) for item in payload)
    value = f"cpa:{ctx.business_token}:{action}"
    if tail:
        value += f":{tail}"
    if len(value.encode("utf-8")) > 64:
        raise ValueError("ClientPlatform admin callback exceeds Telegram limit")
    return value


def _keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def _back_keyboard(ctx: AdminContext, *extra: tuple[str, str]) -> InlineKeyboardMarkup:
    rows = [[item] for item in extra]
    rows.append([("⬅️ Назад", _callback(ctx, "back"))])
    return _keyboard(rows)


_ADMIN_MENU_GROUPS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "menu-work": (
        "📊 Работа и клиенты",
        (
            ("📊 Сегодня", "today"),
            ("📈 Подробный обзор", "today-full"),
            ("👥 Клиенты сегодня", "customers"),
            ("🔎 Все клиенты", "customer-list"),
            ("⚠️ Требуют внимания", "attention"),
            ("🧠 Поведение клиентов", "behavior"),
        ),
    ),
    "menu-content": (
        "✍️ Контент и каналы",
        (
            ("💬 Мессенджеры", "messengers"),
            ("📣 Публикации", "publications"),
            ("✍️ Подготовить тексты", "copy"),
            ("🧪 Проверка предложений", "offers"),
            ("🤖 Автопилот", "autopilot"),
        ),
    ),
    "menu-growth": (
        "📈 Маркетинг и деньги",
        (
            ("📉 Путь до заявки", "funnel"),
            ("💰 Деньги и клиенты", "money"),
            ("💰 Оплаты", "payments"),
            ("🧲 Группы клиентов", "segments"),
            ("💡 Подсказка по ценам", "prices"),
            ("🎁 Приглашения", "invites"),
            ("🧲 Воронка 2.0", "funnel2"),
            ("🧩 Удержание", "retention"),
        ),
    ),
    "menu-team": (
        "👥 Команда и тариф",
        (
            ("💳 Тариф ClientPlatform", "tariff"),
            ("👥 Добавить сотрудника", "add-member"),
            ("👥 Роли команды", "members"),
            ("🔐 Доступы сотрудников", "permissions"),
        ),
    ),
    "menu-system": (
        "⚙️ Системное",
        (
            ("🚦 Release gate", "release"),
            ("🧾 Последние действия", "recent"),
            ("🧪 Системные проверки", "system"),
        ),
    ),
}


def _admin_group_items(
    ctx: AdminContext,
    group_action: str,
) -> tuple[tuple[str, str], ...]:
    _title, items = _ADMIN_MENU_GROUPS[group_action]
    return tuple(
        (title, action)
        for title, action in items
        if ctx.role in _SECTION_ROLES.get(action, set())
    )


def _menu_keyboard(ctx: AdminContext) -> InlineKeyboardMarkup:
    rows = [
        [(title, _callback(ctx, group_action))]
        for group_action, (title, _items) in _ADMIN_MENU_GROUPS.items()
        if _admin_group_items(ctx, group_action)
    ]
    rows.append([("⬅️ Назад", _callback(ctx, "leave"))])
    return _keyboard(rows)


async def _load_admin_context(*, user_id: int, business_id: str) -> AdminContext:
    actor = await control._actor(user_id, business_id)
    accesses = await asyncio.to_thread(list_accessible_businesses, user_id=user_id)
    access = next(
        item for item in accesses if str(item.business.id) == str(actor.business_id)
    )
    return AdminContext(
        user_id=user_id,
        business_id=str(access.business.id),
        business_name=str(access.business.name),
        actor=actor,
        role=actor.role,
    )


def _parse_callback(data: str) -> tuple[str, str, tuple[str, ...]]:
    parts = str(data or "").split(":")
    if len(parts) < 3 or parts[0] != "cpa":
        raise ValueError("invalid ClientPlatform admin callback")
    if parts[1] in {"home", "formats", "back"} and len(parts) == 3:
        legacy_action = {
            "home": "menu",
            "formats": "formats",
            "back": "leave",
        }[parts[1]]
        return control._token_uuid(parts[2]), legacy_action, ()
    business_id = control._token_uuid(parts[1])
    return business_id, parts[2], tuple(parts[3:])


def _assert_section_allowed(ctx: AdminContext, action: str) -> None:
    allowed = _SECTION_ROLES.get(action)
    if allowed is not None and ctx.role not in allowed:
        raise TenantPermissionDenied("admin section is not allowed for this role")


async def _safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    message = control._callback_message(callback)
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        await message.answer(text, reply_markup=reply_markup)
    except TelegramAPIError:
        await message.answer(text, reply_markup=reply_markup)


async def _set_current_section(
    state: FSMContext,
    *,
    action: str,
    push: bool,
) -> None:
    data = await state.get_data()
    current = str(data.get("cp_admin_section") or "menu")
    history = list(data.get("cp_admin_history") or [])
    if push and action != current:
        history.append(current)
        history = history[-20:]
    await state.update_data(
        cp_admin_section=action,
        cp_admin_history=history,
    )


async def _render_menu(
    target: Message | CallbackQuery,
    state: FSMContext,
    ctx: AdminContext,
    *,
    reset: bool,
) -> None:
    if reset:
        await state.update_data(cp_admin_section="menu", cp_admin_history=[])
    text = (
        "⚙️ Управление бизнесом\n\n"
        f"{ctx.business_name} · {_role_label(ctx.role)}\n\n"
        "Выберите, чем хотите заняться. Редкие и технические функции спрятаны "
        "внутри соответствующих разделов."
    )
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, _menu_keyboard(ctx))
    else:
        await target.answer(text, reply_markup=_menu_keyboard(ctx))


async def _render_admin_group(
    target: Message | CallbackQuery,
    state: FSMContext,
    ctx: AdminContext,
    group_action: str,
    *,
    push: bool = True,
) -> None:
    title, _items = _ADMIN_MENU_GROUPS[group_action]
    visible = _admin_group_items(ctx, group_action)
    if not visible:
        raise TenantPermissionDenied("admin group is not allowed for this role")
    if push:
        await _set_current_section(state, action=group_action, push=True)
    rows = [[(label, _callback(ctx, action))] for label, action in visible]
    rows.append([("⬅️ Назад", _callback(ctx, "back"))])
    text = f"{title}\n\nВыберите нужное действие."
    markup = _keyboard(rows)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


def _today_for(profile: object) -> date:
    try:
        return datetime.now(
            ZoneInfo(str(getattr(profile, "timezone", "UTC")))
        ).date()
    except ZoneInfoNotFoundError:
        return datetime.now(timezone.utc).date()


def _on_date(value: object, target: date) -> bool:
    raw = str(value or "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.date() == target


@dataclass(frozen=True, slots=True)
class _EmptySummary:
    customers: int = 0
    programs: int = 0
    dispatch_pending: int = 0
    dispatch_sent: int = 0
    dispatch_attention: int = 0


async def _optional_thread_call(
    function: Any,
    *,
    default: Any,
    **kwargs: Any,
) -> Any:
    try:
        return await asyncio.to_thread(function, **kwargs)
    except TenantPermissionDenied:
        return default


async def _base_snapshot(ctx: AdminContext) -> tuple[Any, Any, list[Any], list[Any], list[Any], list[Any], list[Any]]:
    profile, summary, capabilities, slots, customers, programs, progress = await asyncio.gather(
        asyncio.to_thread(get_business_profile, actor=ctx.actor),
        _optional_thread_call(
            business_delivery_summary,
            default=_EmptySummary(),
            actor=ctx.actor,
        ),
        asyncio.to_thread(list_business_capabilities, actor=ctx.actor),
        _optional_thread_call(
            list_booking_slots,
            default=[],
            actor=ctx.actor,
            include_unavailable=True,
        ),
        _optional_thread_call(
            list_customers,
            default=[],
            actor=ctx.actor,
            include_archived=True,
        ),
        _optional_thread_call(
            list_programs,
            default=[],
            actor=ctx.actor,
            include_archived=True,
        ),
        _optional_thread_call(
            list_business_program_progress,
            default=[],
            actor=ctx.actor,
            limit=100,
        ),
    )
    return (
        profile,
        summary,
        list(capabilities),
        list(slots),
        list(customers),
        list(programs),
        list(progress),
    )


def _list_members_sync(actor: TenantContext) -> list[dict[str, Any]]:
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        rows = conn.execute(
            """
            SELECT id, user_id, role, status, created_at, updated_at, revoked_at
            FROM business_members
            WHERE business_id=?
            ORDER BY CASE WHEN role='owner' THEN 0 ELSE 1 END, created_at, id
            """,
            (current.business_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            if hasattr(row, "keys"):
                result.append({key: row[key] for key in row.keys()})
            else:
                result.append(
                    {
                        "id": row[0],
                        "user_id": row[1],
                        "role": row[2],
                        "status": row[3],
                        "created_at": row[4],
                        "updated_at": row[5],
                        "revoked_at": row[6],
                    }
                )
        return result


async def _render_today(callback: CallbackQuery, state: FSMContext, ctx: AdminContext, *, full: bool) -> None:
    profile, summary, capabilities, slots, customers, programs, progress = await _base_snapshot(ctx)
    today = _today_for(profile)
    new_customers = sum(_on_date(item.created_at, today) for item in customers)
    new_programs = sum(_on_date(item.created_at, today) for item in programs)
    open_slots = sum(item.slot.status == BookingSlotStatus.OPEN for item in slots)
    active_capabilities = sum(
        item.status == CapabilityStatus.ACTIVE for item in capabilities
    )
    if not full:
        text = (
            "📊 Сегодня (кратко)\n\n"
            f"Новых клиентов: {new_customers}\n"
            f"Новых программ: {new_programs}\n"
            f"Свободных времён: {open_slots}\n"
            f"Ожидают отправки: {summary.dispatch_pending}\n"
            f"Требуют внимания: {summary.dispatch_attention}"
        )
    else:
        completed = sum(item.completed_lessons for item in progress)
        total = sum(item.total_lessons for item in progress)
        text = (
            "📈 Сегодня (подробно)\n\n"
            f"Бизнес: {ctx.business_name}\n"
            f"Клиентов всего: {summary.customers}\n"
            f"Программ активно: {summary.programs}\n"
            f"Форматов подключено: {active_capabilities}\n"
            f"Свободных времён: {open_slots}\n\n"
            f"Отправки в очереди: {summary.dispatch_pending}\n"
            f"Успешно отправлено: {summary.dispatch_sent}\n"
            f"Ошибки отправки: {summary.dispatch_attention}\n\n"
            f"Прохождение материалов: {completed}/{total}"
        )
    await _safe_edit(callback, text, _back_keyboard(ctx))
    await _set_current_section(state, action="today-full" if full else "today", push=True)


async def _render_customer_list(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AdminContext,
    *,
    today_only: bool,
) -> None:
    profile, customers = await asyncio.gather(
        asyncio.to_thread(get_business_profile, actor=ctx.actor),
        asyncio.to_thread(list_customers, actor=ctx.actor, include_archived=False),
    )
    if today_only:
        today = _today_for(profile)
        customers = [item for item in customers if _on_date(item.created_at, today)]
    rows: list[list[tuple[str, str]]] = []
    for customer in customers[:20]:
        title = customer.display_name or f"Клиент {customer.id[:8]}"
        rows.append(
            [
                (
                    title,
                    _callback(
                        ctx,
                        "customer",
                        control._uuid_token(customer.id),
                    ),
                )
            ]
        )
    rows.append([("⬅️ Назад", _callback(ctx, "back"))])
    title = "👥 Клиенты сегодня" if today_only else "🔎 Карточка клиента"
    text = f"{title}\n\n"
    text += (
        f"Найдено: {len(customers)}\nВыберите клиента:"
        if customers
        else "Клиентов в этом разделе пока нет."
    )
    await _safe_edit(callback, text, _keyboard(rows))
    await _set_current_section(
        state,
        action="customers" if today_only else "customer-list",
        push=True,
    )


async def _render_customer_card(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AdminContext,
    customer_token: str,
) -> None:
    customer_id = control._token_uuid(customer_token)
    record, timeline = await asyncio.gather(
        asyncio.to_thread(get_customer, actor=ctx.actor, customer_id=customer_id),
        asyncio.to_thread(get_customer_timeline, actor=ctx.actor, customer_id=customer_id),
    )
    identity_lines = [
        f"• {item.platform.value}: @{item.username}"
        if item.username
        else f"• {item.platform.value}: {item.display_name or item.external_subject}"
        for item in record.identities
    ]
    text = (
        "🔎 Карточка клиента\n\n"
        f"Имя: {record.customer.display_name or 'не указано'}\n"
        f"Статус: {record.customer.status.value}\n"
        f"Создан: {record.customer.created_at}\n\n"
        "Контакты:\n"
        + ("\n".join(identity_lines) if identity_lines else "• не подключены")
        + "\n\nИстория клиента:\n"
        + "\n".join(format_customer_timeline_lines(timeline))
    )
    await _safe_edit(callback, text, _back_keyboard(ctx))
    await _set_current_section(state, action="customer", push=True)


async def _render_behavior(callback: CallbackQuery, state: FSMContext, ctx: AdminContext) -> None:
    progress = await asyncio.to_thread(
        list_business_program_progress,
        actor=ctx.actor,
        limit=25,
    )
    if not progress:
        text = "🧠 Поведение\n\nПрохождение программ ещё не началось."
    else:
        lines = []
        for item in progress[:15]:
            name = item.customer_display_name or f"Клиент {item.customer_id[:8]}"
            lines.append(
                f"• {name}: «{item.program_title}» — "
                f"{item.completed_lessons}/{item.total_lessons} ({item.percent_complete}%)"
            )
        text = "🧠 Поведение\n\n" + "\n".join(lines)
    await _safe_edit(callback, text, _back_keyboard(ctx))
    await _set_current_section(state, action="behavior", push=True)


async def _render_messengers(callback: CallbackQuery, state: FSMContext, ctx: AdminContext) -> None:
    connection_statuses = await asyncio.to_thread(
        business_connection_statuses, actor=ctx.actor
    )
    labels = {"telegram": "Telegram", "vk": "ВКонтакте", "max": "MAX"}
    status_labels = {
        "active": "✅ работает",
        "pending": "⏳ настройка",
        "attention": "⚠️ требует внимания",
        "disabled": "⏸ отключён",
        "revoked": "⛔ отозван",
    }
    by_platform: dict[str, list[Any]] = {"telegram": [], "vk": [], "max": []}
    for connection_platform, connection_status in connection_statuses:
        platform = str(connection_platform.value)
        if platform in by_platform:
            by_platform[platform].append(connection_status)

    lines = ["💬 Мессенджеры", "", "Каналы этого бизнеса:"]
    active: set[str] = set()
    for platform in ("vk", "max", "telegram"):
        items = by_platform[platform]
        if any(item.value == "active" for item in items):
            active.add(platform)
        if not items:
            lines.append(f"• {labels[platform]}: не подключён")
            continue
        states = [status_labels.get(item.value, item.value) for item in items]
        lines.append(f"• {labels[platform]}: {', '.join(states)}")
    lines.extend([
        "",
        "Можно подключить дополнительный мессенджер или продолжить работу "
        "в уже подключённом канале под тем же аккаунтом.",
    ])

    rows: list[list[InlineKeyboardButton]] = []
    if ctx.role in _ADMIN_ROLES and _native_messenger_setup_ingress_enabled():
        connect = {
            "telegram": "✈️ Подключить Telegram",
            "vk": "🔵 Подключить ВКонтакте",
            "max": "🟣 Подключить MAX",
        }
        for platform in ("telegram", "vk", "max"):
            if platform not in active:
                rows.append([InlineKeyboardButton(
                    text=connect[platform],
                    callback_data=_callback(ctx, "messenger-connect", platform),
                )])

    switch_labels = {
        ConnectionPlatform.VK: "🔵 Перейти во ВКонтакте",
        ConnectionPlatform.MAX: "🟣 Перейти в MAX",
    }
    try:
        switchable = available_staff_messenger_switches(ctx.actor)
    except (RuntimeError, ValueError):
        switchable = ()
    switch_links = StaffMessengerSwitchLinkService()
    for platform in switchable:
        if platform == ConnectionPlatform.TELEGRAM or platform not in switch_labels:
            continue
        try:
            url = switch_links.resolve_command_url(
                command=build_staff_switch_command(ctx.actor, platform),
                business_id=ctx.business_id,
            )
        except (RuntimeError, ValueError):
            continue
        if url:
            rows.append([InlineKeyboardButton(text=switch_labels[platform], url=url)])

    rows.append([InlineKeyboardButton(
        text="🤖 Мой Telegram-бот", callback_data=f"cpb:o:{ctx.business_token}"
    )])
    rows.append([InlineKeyboardButton(
        text="⬅️ Назад", callback_data=_callback(ctx, "back")
    )])
    await _safe_edit(
        callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await _set_current_section(state, action="messengers", push=True)


async def _render_messenger_connect(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AdminContext,
    platform: str,
) -> None:
    normalized = str(platform or "").strip().lower()
    if normalized not in {"telegram", "vk", "max"}:
        raise ValueError("unsupported messenger setup platform")
    if not _native_messenger_setup_ingress_enabled():
        await _safe_edit(
            callback,
            "Безопасное подключение временно недоступно: multi-messenger ingress не включён.",
            _back_keyboard(ctx),
        )
        return
    public_base = str(
        getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or ""
    ).strip().rstrip("/")
    if not public_base.startswith("https://"):
        await _safe_edit(
            callback,
            "Безопасное подключение временно недоступно: публичный HTTPS-адрес ClientPlatform не настроен.",
            _back_keyboard(ctx),
        )
        return
    issued = await asyncio.to_thread(
        issue_native_messenger_setup,
        actor=ctx.actor,
        platform=normalized,
        ttl_seconds=600,
    )
    label = {"telegram": "Telegram", "vk": "ВКонтакте", "max": "MAX"}[normalized]
    setup_url = f"{public_base}/clientplatform/connect/{issued.token}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🔐 Открыть подключение {label}",
                    url=setup_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=_callback(ctx, "messengers"),
                )
            ],
        ]
    )
    await _safe_edit(
        callback,
        (
            f"Подключение {label}\n\n"
            "Откройте одноразовую защищённую страницу ниже. "
            "Токен мессенджера вводится только на HTTPS-странице и не отправляется сообщением в Telegram.\n\n"
            "Ссылка действует 10 минут и перестаёт работать после первой отправки формы."
        ),
        keyboard,
    )
    await _set_current_section(state, action="messenger-connect", push=True)


async def _render_attention(callback: CallbackQuery, state: FSMContext, ctx: AdminContext) -> None:
    summary = await asyncio.to_thread(business_delivery_summary, actor=ctx.actor)
    text = (
        "⚠️ Требуют внимания\n\n"
        f"Ошибки отправки: {summary.dispatch_attention}\n"
        f"Ожидают отправки: {summary.dispatch_pending}\n\n"
    )
    text += (
        "Нужно проверить неотправленные материалы и подключение клиентского бота."
        if summary.dispatch_attention or summary.dispatch_pending
        else "Сейчас критических задач нет."
    )
    await _safe_edit(callback, text, _back_keyboard(ctx))
    await _set_current_section(state, action="attention", push=True)


async def _render_marketing(callback: CallbackQuery, state: FSMContext, ctx: AdminContext, action: str) -> None:
    profile, summary, capabilities, slots, customers, programs, progress = await _base_snapshot(ctx)
    if action == "publications":
        projection = await asyncio.to_thread(
            get_publication_calendar_projection,
            actor=ctx.actor,
        )
        calendar = "\n".join(
            format_publication_calendar_lines(
                projection.entries,
                timezone_name=profile.timezone,
                max_entries=8,
            )
        )
        text = (
            "📣 Публикации\n\n"
            f"Черновики: {projection.draft_count}\n"
            f"Запланировано: {projection.scheduled_count}\n"
            f"Опубликовано: {projection.published_count}\n"
            f"Ошибки: {projection.failed_count}\n\n"
            "Ближайшие и последние:\n"
            f"{calendar}"
        )
        await _safe_edit(callback, text, _back_keyboard(ctx))
        await _set_current_section(state, action=action, push=True)
        return
    active_capabilities = [item for item in capabilities if item.status == CapabilityStatus.ACTIVE]
    completed_customers = sum(
        item.total_lessons > 0 and item.completed_lessons >= item.total_lessons
        for item in progress
    )
    enrolled_customers = len({item.customer_id for item in progress})
    stalled = sum(
        item.total_lessons > item.completed_lessons
        for item in progress
    )
    sections = {
        "autopilot": (
            "🤖 Growth Autopilot\n\n"
            f"Подключено форматов: {len(active_capabilities)}\n"
            f"Активных программ: {summary.programs}\n"
            f"Очередь отправки: {summary.dispatch_pending}\n\n"
            "Автопилот работает только в подтверждённых владельцем границах. "
            "Сейчас доступны безопасные выдачи программ и напоминания."
        ),
        "funnel": (
            "📉 Путь до заявки\n\n"
            f"Клиенты: {summary.customers}\n"
            f"Подключены к программам: {enrolled_customers}\n"
            f"Завершили программу: {completed_customers}\n"
            f"Свободных слотов: {sum(item.slot.status == BookingSlotStatus.OPEN for item in slots)}"
        ),
        "money": (
            "💰 Деньги и клиенты\n\n"
            f"Активных клиентов: {summary.customers}\n"
            f"Активных программ: {summary.programs}\n"
            "Заказы и выручка: платёжный модуль бизнеса ещё не подключён."
        ),
        "payments": (
            "💰 Оплаты\n\n"
            "Платёжная система этого бизнеса ещё не подключена.\n\n"
            "После подключения здесь появятся успешные, ожидающие и проблемные оплаты."
        ),
        "segments": (
            "🧲 Группы клиентов\n\n"
            f"Все клиенты: {summary.customers}\n"
            f"Проходят программы: {enrolled_customers}\n"
            f"Завершили: {completed_customers}\n"
            f"Остановились до завершения: {stalled}\n"
            f"Без программы: {max(0, summary.customers - enrolled_customers)}"
        ),
        "offers": (
            "🧪 Проверка предложений\n\n"
            f"Форматов работы: {len(active_capabilities)}\n"
            f"Программ: {len(programs)}\n\n"
            "Предложения можно сравнивать после накопления заявок и оплат."
        ),
        "copy": (
            "✍️ Подготовить тексты\n\n"
            f"Описание бизнеса:\n{profile.activity_description}\n\n"
            "Используйте это описание как основу; изменение доступно кнопкой ниже."
        ),
        "prices": (
            "💡 Подсказка по ценам\n\n"
            "В текущих предложениях ClientPlatform ещё нет структурированного поля цены.\n"
            "Сначала добавьте услуги и программы; затем здесь появится ценовой анализ."
        ),
    }
    extra: list[tuple[str, str]] = []
    if action == "copy":
        extra.append(("✏️ Изменить деятельность", f"cp:editact:{ctx.business_token}"))
    if action in {"offers", "prices"}:
        extra.append(("🧩 Форматы работы", _callback(ctx, "formats")))
    await _safe_edit(callback, sections[action], _back_keyboard(ctx, *extra))
    await _set_current_section(state, action=action, push=True)


# Keep the canonical fallback renderer directly testable even when the runtime
# extension replaces _render_marketing after module import.
_render_marketing_fallback = _render_marketing


async def _render_admin_report(callback: CallbackQuery, state: FSMContext, ctx: AdminContext, action: str) -> None:
    profile, summary, capabilities, slots, customers, programs, progress = await _base_snapshot(ctx)
    active = sum(item.status == CapabilityStatus.ACTIVE for item in capabilities)
    open_slots = sum(item.slot.status == BookingSlotStatus.OPEN for item in slots)
    enrolled = len({item.customer_id for item in progress})
    complete = sum(
        item.total_lessons > 0 and item.completed_lessons >= item.total_lessons
        for item in progress
    )
    incomplete = [item for item in progress if item.completed_lessons < item.total_lessons]
    recent_items = sorted(
        [
            (str(item.created_at), f"Клиент: {item.display_name or item.id[:8]}")
            for item in customers
        ]
        + [(str(item.created_at), f"Программа: {item.title}") for item in programs]
        + [(str(item.updated_at), f"Прогресс: {item.program_title}") for item in progress],
        reverse=True,
    )[:10]
    release_ok = (
        profile.status.value == "ready"
        and active > 0
        and summary.dispatch_attention == 0
    )
    sections = {
        "release": (
            "🚦 Release gate\n\n"
            f"Профиль бизнеса: {'✅' if profile.status.value == 'ready' else '❌'}\n"
            f"Форматы работы: {'✅' if active else '❌'}\n"
            f"Ошибки отправки: {'✅' if summary.dispatch_attention == 0 else '❌'}\n"
            f"Перезапуска не требуется: ✅\n\n"
            f"Итог: {'ГОТОВО' if release_ok else 'ТРЕБУЕТ НАСТРОЙКИ'}"
        ),
        "invites": (
            "🎁 Приглашения и рекомендации\n\n"
            f"Подключено клиентов: {summary.customers}\n\n"
            "Создайте персональную ссылку для клиента кнопкой ниже."
        ),
        "funnel2": (
            "🧲 Воронка 2.0\n\n"
            f"Клиенты: {summary.customers}\n"
            f"В программах: {enrolled}\n"
            f"Завершили: {complete}\n"
            f"Доступных записей: {open_slots}\n"
            f"Отправлено материалов: {summary.dispatch_sent}"
        ),
        "retention": (
            "🧩 Удержание\n\n"
            f"Клиентов всего: {summary.customers}\n"
            f"Незавершённых прохождений: {len(incomplete)}\n"
            f"Без активной программы: {max(0, summary.customers - enrolled)}\n\n"
            "Следующий шаг — вернуть остановившихся клиентов через безопасное напоминание."
        ),
        "recent": (
            "🧾 Последние действия\n\n"
            + ("\n".join(f"• {label} — {stamp}" for stamp, label in recent_items)
               if recent_items else "Действий пока нет.")
        ),
        "system": (
            "🧪 Системные проверки\n\n"
            "Tenant-доступ: ✅\n"
            "PostgreSQL-чтение: ✅\n"
            f"Профиль бизнеса: {'✅' if profile.status.value == 'ready' else '⚠️'}\n"
            f"Очередь отправки: {'✅' if summary.dispatch_attention == 0 else '⚠️'}\n"
            f"Программы: {summary.programs}\n"
            f"Клиенты: {summary.customers}"
        ),
    }
    extra: list[tuple[str, str]] = []
    if action == "invites":
        extra.append(("➕ Подключить клиента", f"cp:invite:{ctx.business_token}"))
    await _safe_edit(callback, sections[action], _back_keyboard(ctx, *extra))
    await _set_current_section(state, action=action, push=True)


async def _render_formats(callback: CallbackQuery, state: FSMContext, ctx: AdminContext) -> None:
    capabilities = await asyncio.to_thread(
        list_business_capabilities,
        actor=ctx.actor,
        include_disabled=True,
    )
    lines = [
        f"{'✅' if item.status == CapabilityStatus.ACTIVE else '➖'} {item.title}"
        for item in capabilities
    ]
    text = "🧩 Форматы работы\n\n" + ("\n".join(lines) if lines else "Форматы ещё не выбраны.")
    await _safe_edit(
        callback,
        text,
        _back_keyboard(
            ctx,
            ("Настроить форматы", _callback(ctx, "formats-edit")),
        ),
    )
    await _set_current_section(state, action="formats", push=True)


async def _render_tariff(callback: CallbackQuery, state: FSMContext, ctx: AdminContext) -> None:
    text = (
        "💳 Тариф ClientPlatform\n\n"
        "Тарифный модуль ClientPlatform пока не активирован для этого бизнеса.\n\n"
        "Текущие данные и настройки бизнеса сохранены и продолжают работать."
    )
    await _safe_edit(callback, text, _back_keyboard(ctx))
    await _set_current_section(state, action="tariff", push=True)


async def _render_members(callback: CallbackQuery, state: FSMContext, ctx: AdminContext) -> None:
    members = await asyncio.to_thread(_list_members_sync, ctx.actor)
    rows: list[list[tuple[str, str]]] = []
    for member in members:
        role = PlatformRole(str(member["role"]))
        marker = "✅" if str(member["status"]) == "active" else "➖"
        rows.append(
            [
                (
                    f"{marker} {member['user_id']} · {_role_label(role)}",
                    _callback(ctx, "member", member["user_id"]),
                )
            ]
        )
    rows.append([("⬅️ Назад", _callback(ctx, "back"))])
    await _safe_edit(
        callback,
        "👥 Роли команды\n\nВыберите сотрудника:",
        _keyboard(rows),
    )
    await _set_current_section(state, action="members", push=True)


async def _render_member_card(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AdminContext,
    user_id: int,
) -> None:
    members = await asyncio.to_thread(_list_members_sync, ctx.actor)
    member = next((item for item in members if int(item["user_id"]) == user_id), None)
    if member is None:
        await _safe_edit(callback, "Сотрудник больше не найден.", _back_keyboard(ctx))
        return
    role = PlatformRole(str(member["role"]))
    rows: list[list[tuple[str, str]]] = []
    if role != PlatformRole.OWNER:
        for code, target_role in _ROLE_CODES.items():
            prefix = "✅ " if target_role == role and str(member["status"]) == "active" else ""
            rows.append(
                [
                    (
                        f"{prefix}{_role_label(target_role)}",
                        _callback(ctx, "member-role", user_id, code),
                    )
                ]
            )
        rows.append(
            [("Отозвать доступ", _callback(ctx, "member-revoke", user_id))]
        )
    rows.append([("⬅️ Назад", _callback(ctx, "back"))])
    text = (
        "👤 Сотрудник\n\n"
        f"Telegram ID: {user_id}\n"
        f"Роль: {_role_label(role)}\n"
        f"Статус: {member['status']}"
    )
    await _safe_edit(callback, text, _keyboard(rows))
    await _set_current_section(state, action="member", push=True)


async def _render_permissions(callback: CallbackQuery, state: FSMContext, ctx: AdminContext) -> None:
    lines = []
    for role, permissions in _ROLE_PERMISSIONS.items():
        lines.append(f"{_role_label(role)}:")
        lines.extend(f"• {item}" for item in permissions)
        lines.append("")
    text = "🔐 Доступы сотрудников\n\n" + "\n".join(lines).rstrip()
    await _safe_edit(callback, text, _back_keyboard(ctx))
    await _set_current_section(state, action="permissions", push=True)


async def _begin_add_member(callback: CallbackQuery, state: FSMContext, ctx: AdminContext) -> None:
    rows = [
        [(_role_label(role), _callback(ctx, "add-role", code))]
        for code, role in _ROLE_CODES.items()
    ]
    rows.append([("⬅️ Назад", _callback(ctx, "back"))])
    await _safe_edit(
        callback,
        "👥 Добавить сотрудника\n\nСначала выберите роль:",
        _keyboard(rows),
    )
    await _set_current_section(state, action="add-member", push=True)


async def _select_add_member_role(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AdminContext,
    code: str,
) -> None:
    role = _ROLE_CODES.get(code)
    if role is None:
        raise ValueError("unknown member role")
    await state.set_state(ClientPlatformAdminState.waiting_member_user)
    await state.update_data(
        cp_admin_business_id=ctx.business_id,
        cp_admin_member_role=role.value,
    )
    await _safe_edit(
        callback,
        "👥 Добавить сотрудника\n\n"
        f"Роль: {_role_label(role)}\n\n"
        "Отправьте Telegram ID, @username, перешлите сообщение сотрудника "
        "или выберите пользователя через Telegram.\n\n"
        "Для отмены отправьте /cancel.",
        InlineKeyboardMarkup(inline_keyboard=[]),
    )


async def _resolve_target_user_id(message: Message) -> int | None:
    shared = getattr(message, "user_shared", None)
    if shared is not None and getattr(shared, "user_id", None):
        return int(shared.user_id)
    forwarded = getattr(message, "forward_from", None)
    if forwarded is not None and getattr(forwarded, "id", None):
        return int(forwarded.id)
    text = str(message.text or "").strip()
    if text.isdigit():
        return int(text)
    if text.startswith("@") and len(text) > 1:
        try:
            chat = await message.bot.get_chat(text)
        except TelegramAPIError:
            return None
        return int(chat.id)
    return None


@router.message(ClientPlatformAdminState.waiting_member_user)
async def receive_member_user(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("cp_admin_business_id") or "")
    role = PlatformRole(str(data.get("cp_admin_member_role") or ""))
    ctx = await _load_admin_context(
        user_id=control._user_id(message),
        business_id=business_id,
    )
    _assert_section_allowed(ctx, "add-member")
    target_user_id = await _resolve_target_user_id(message)
    if target_user_id is None:
        await message.answer(
            "Не понял, кого добавить. Отправьте числовой Telegram ID, @username "
            "или перешлите сообщение сотрудника."
        )
        return
    member = await asyncio.to_thread(
        grant_business_member,
        actor=ctx.actor,
        user_id=target_user_id,
        role=role,
    )
    await state.clear()
    await message.answer(
        f"✅ Сотрудник добавлен: {member.user_id}\n"
        f"Роль: {_role_label(member.role)}"
    )
    await _render_menu(message, state, ctx, reset=True)


async def _navigate_back(callback: CallbackQuery, state: FSMContext, ctx: AdminContext) -> None:
    data = await state.get_data()
    history = list(data.get("cp_admin_history") or [])
    action = str(history.pop() if history else "menu")
    if action in _ADMIN_MENU_GROUPS:
        await _render_admin_group(callback, state, ctx, action, push=False)
    elif action == "customer-list":
        await _render_customer_list(callback, state, ctx, today_only=False)
    elif action == "customers":
        await _render_customer_list(callback, state, ctx, today_only=True)
    elif action == "members":
        await _render_members(callback, state, ctx)
    elif action == "add-member":
        await _begin_add_member(callback, state, ctx)
    else:
        action = "menu"
        await _render_menu(callback, state, ctx, reset=False)
    await state.update_data(cp_admin_history=history, cp_admin_section=action)


async def open_admin_command(message: Message, state: FSMContext) -> None:
    user_id = control._user_id(message)
    accesses = await asyncio.to_thread(list_accessible_businesses, user_id=user_id)
    await state.clear()
    if not accesses:
        await message.answer("Сначала создайте бизнес через /start.")
        return
    if len(accesses) == 1:
        ctx = await _load_admin_context(
            user_id=user_id,
            business_id=str(accesses[0].business.id),
        )
        await _render_menu(message, state, ctx, reset=True)
        return
    await message.answer(
        "Для какого бизнеса открыть админку?",
        reply_markup=_keyboard(
            [
                [
                    (
                        access.business.name,
                        f"cpa:{control._uuid_token(access.business.id)}:menu",
                    )
                ]
                for access in accesses
            ]
        ),
    )


async def _answer_stale_callback(callback: CallbackQuery) -> None:
    log.warning(
        "Invalid ClientPlatform admin callback: %s",
        callback.data,
        exc_info=True,
    )
    try:
        await callback.answer(
            "Кнопка устарела. Откройте /admin ещё раз.",
            show_alert=True,
        )
    except TelegramAPIError:
        return


@router.callback_query(F.data.startswith("cpa:"))
async def admin_gate(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        business_id, action, payload = _parse_callback(str(callback.data or ""))
        ctx = await _load_admin_context(
            user_id=control._canonical_telegram_user_id(
                int(callback.from_user.id),
                username=getattr(callback.from_user, "username", None),
                display_name=getattr(callback.from_user, "full_name", None),
            ),
            business_id=business_id,
        )
        if action not in {"menu", "back", "leave"}:
            if action in _ADMIN_MENU_GROUPS:
                if not _admin_group_items(ctx, action):
                    raise TenantPermissionDenied("admin group is not allowed for this role")
            else:
                _assert_section_allowed(ctx, action)

        legacy_callback = str(callback.data or "").startswith(
            ("cpa:home:", "cpa:formats:", "cpa:back:")
        )
        if action == "menu":
            await state.clear()
            if legacy_callback:
                await control._callback_message(callback).answer(
                    "⚙️ Управление бизнесом\n\n"
                    f"{ctx.business_name} · {_role_label(ctx.role)}\n\n"
                    "Выберите, чем хотите заняться. Редкие и технические функции "
                    "спрятаны внутри соответствующих разделов.",
                    reply_markup=_menu_keyboard(ctx),
                )
            else:
                await _render_menu(callback, state, ctx, reset=True)
        elif action == "back":
            await _navigate_back(callback, state, ctx)
        elif action in _ADMIN_MENU_GROUPS:
            await _render_admin_group(callback, state, ctx, action)
        elif action == "leave":
            await state.clear()
            await control._send_dashboard(
                control._callback_message(callback),
                user_id=ctx.user_id,
                business_id=ctx.business_id,
            )
        elif action == "today":
            await _render_today(callback, state, ctx, full=False)
        elif action == "today-full":
            await _render_today(callback, state, ctx, full=True)
        elif action == "customers":
            await _render_customer_list(callback, state, ctx, today_only=True)
        elif action == "customer-list":
            await _render_customer_list(callback, state, ctx, today_only=False)
        elif action == "customer":
            await _render_customer_card(callback, state, ctx, payload[0])
        elif action == "behavior":
            await _render_behavior(callback, state, ctx)
        elif action == "messengers":
            await _render_messengers(callback, state, ctx)
        elif action == "messenger-connect":
            await _render_messenger_connect(callback, state, ctx, payload[0])
        elif action == "attention":
            await _render_attention(callback, state, ctx)
        elif action in {
            "autopilot",
            "publications",
            "funnel",
            "money",
            "payments",
            "segments",
            "offers",
            "copy",
            "prices",
        }:
            await _render_marketing(callback, state, ctx, action)
        elif action in {
            "release",
            "invites",
            "funnel2",
            "retention",
            "recent",
            "system",
        }:
            await _render_admin_report(callback, state, ctx, action)
        elif action == "formats":
            if legacy_callback:
                await state.clear()
                await control._send_capability_setup(
                    control._callback_message(callback),
                    user_id=ctx.user_id,
                    business_id=ctx.business_id,
                )
            else:
                await _render_formats(callback, state, ctx)
        elif action == "formats-edit":
            await state.clear()
            await control._send_capability_setup(
                control._callback_message(callback),
                user_id=ctx.user_id,
                business_id=ctx.business_id,
            )
        elif action == "tariff":
            await _render_tariff(callback, state, ctx)
        elif action == "add-member":
            await _begin_add_member(callback, state, ctx)
        elif action == "add-role":
            await _select_add_member_role(callback, state, ctx, payload[0])
        elif action == "members":
            await _render_members(callback, state, ctx)
        elif action == "member":
            await _render_member_card(callback, state, ctx, int(payload[0]))
        elif action == "member-role":
            role = _ROLE_CODES[payload[1]]
            member = await asyncio.to_thread(
                grant_business_member,
                actor=ctx.actor,
                user_id=int(payload[0]),
                role=role,
            )
            await _render_member_card(callback, state, ctx, member.user_id)
        elif action == "member-revoke":
            member = await asyncio.to_thread(
                revoke_business_member,
                actor=ctx.actor,
                user_id=int(payload[0]),
            )
            await _safe_edit(
                callback,
                f"Доступ сотрудника {member.user_id} отозван.",
                _back_keyboard(ctx),
            )
        elif action == "permissions":
            await _render_permissions(callback, state, ctx)
        else:
            await _safe_edit(
                callback,
                "Этот раздел больше недоступен. Откройте админ-меню заново.",
                _back_keyboard(ctx),
            )
    except (TenancyError, StopIteration):
        log.warning(
            "Blocked ClientPlatform admin callback user_id=%s callback=%s",
            getattr(callback.from_user, "id", None),
            callback.data,
        )
        try:
            await callback.answer(
                "Доступ к этому разделу отозван или не назначен.",
                show_alert=True,
            )
        except TelegramAPIError:
            return
    except IndexError:
        await _answer_stale_callback(callback)
    except KeyError:
        await _answer_stale_callback(callback)
    except TypeError:
        await _answer_stale_callback(callback)
    except ValueError:
        await _answer_stale_callback(callback)


async def send_admin_panel(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    """Compatibility entry used by production probes and earlier extensions."""

    ctx = await _load_admin_context(user_id=user_id, business_id=business_id)
    await message.answer(
        "⚙️ Управление бизнесом\n\n"
        f"{ctx.business_name} · {_role_label(ctx.role)}\n\n"
        "Выберите, чем хотите заняться. Редкие и технические функции спрятаны "
        "внутри соответствующих разделов.",
        reply_markup=_menu_keyboard(ctx),
    )


def install_admin_dashboard_button(control_module: ModuleType) -> None:
    """Add the Metrotherapy-style panel entry to every business dashboard."""

    if bool(getattr(control_module, "_admin_dashboard_installed", False)):
        return
    original = control_module._dashboard_keyboard

    def dashboard_with_admin(
        business_id: str,
        capabilities: list[object],
    ) -> InlineKeyboardMarkup:
        markup = original(business_id, capabilities)
        button = InlineKeyboardButton(
            text="🛠 Панель",
            callback_data=f"cpa:{control_module._uuid_token(business_id)}:menu",
        )
        return InlineKeyboardMarkup(
            inline_keyboard=[*markup.inline_keyboard, [button]]
        )

    control_module._dashboard_keyboard = dashboard_with_admin
    control_module._admin_dashboard_installed = True


__all__ = [
    "ClientPlatformAdminState",
    "admin_gate",
    "install_admin_dashboard_button",
    "open_admin_command",
    "router",
    "send_admin_panel",
]
