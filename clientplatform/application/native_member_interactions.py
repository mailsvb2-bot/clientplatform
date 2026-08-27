from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from clientplatform.application.activity import (
    get_business_profile,
    list_business_capabilities,
)
from clientplatform.application.acquisition_destination import (
    prepare_nearest_acquisition_destination,
)
from clientplatform.application.bookings import list_booking_slots
from clientplatform.application.messenger_switching import (
    available_staff_messenger_switches,
    build_staff_switch_command,
)
from clientplatform.application.connections import list_connections
from clientplatform.application.control import business_delivery_summary
from clientplatform.application.customers import get_customer, list_customers
from clientplatform.application.growth_cockpit import get_growth_cockpit
from clientplatform.application.programs import list_programs
from clientplatform.application.progress import list_business_program_progress
from clientplatform.application.sales_workspace import (
    add_sales_workspace_note,
    assign_sales_workspace_to_actor,
    cancel_sales_workspace_followup,
    claim_sales_workspace_handoff,
    get_sales_workspace_item,
    list_sales_workspace,
    list_sales_workspace_handoffs,
    list_sales_workspace_recent_closed,
    reopen_sales_workspace,
    resolve_sales_workspace_handoff,
    schedule_sales_workspace_followup,
    set_sales_workspace_next_action,
    suppress_sales_workspace_followup,
    transition_sales_workspace,
    unassign_sales_workspace,
)
from clientplatform.application.tenancy import (
    list_accessible_businesses,
    resolve_tenant_context,
)
from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from clientplatform.domain.messenger_channels import MessengerIngressRoute
from clientplatform.domain.promotions import PromotionChannel, PromotionError
from clientplatform.domain.sales import SalesError, SalesLeadStage
from clientplatform.domain.tenancy import (
    PlatformRole,
    TenantAccessDenied,
    TenantContext,
    TenantPermissionDenied,
)
from clientplatform.infrastructure import DispatchOutboxRepository, TenancyRepository
from config.settings import settings
from services.accounts.identity import resolve_account_for_identity
from services.db import get_db, get_db_ro
from services.messenger.bridge import (
    consume_bridge_token_and_link,
    resolve_bridge_token,
)
from services.messenger.entrypoints import parse_start_payload


log = logging.getLogger(__name__)

_SUPPORT_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.SUPPORT,
    }
)
_MARKETING_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.MARKETER,
        PlatformRole.ANALYST,
    }
)
_CONTENT_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.CONTENT_MANAGER,
        PlatformRole.MARKETER,
    }
)
_AUTOMATION_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.MARKETER,
    }
)
_ACQUISITION_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.CONTENT_MANAGER,
        PlatformRole.MARKETER,
    }
)
_CONNECTION_ROLES = frozenset({PlatformRole.OWNER, PlatformRole.ADMINISTRATOR})
_OWNER_ROLES = frozenset({PlatformRole.OWNER})
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
    "работа": "work",
    "рост": "growth",
    "управление": "manage",
    "команда": "team",
    "сегодня": "today",
    "клиенты": "customers",
    "записи": "bookings",
    "программы": "programs",
    "мессенджеры": "messengers",
    "обращения": "sales",
    "продажи": "sales",
    "обращения и продажи": "sales",
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
    args: tuple[str, ...] = ()


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
    note_match = re.fullmatch(
        r"заметка\s+([0-9a-f-]{6,36})\s+(.{1,500})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if note_match is not None:
        return ParsedMemberInteraction(
            "sales-note-text",
            (note_match.group(1), note_match.group(2).strip()),
        )
    next_match = re.fullmatch(
        r"(?:следующее|следующее\s+действие)\s+([0-9a-f-]{6,36})\s+(.{1,500})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if next_match is not None:
        return ParsedMemberInteraction(
            "sales-next-text",
            (next_match.group(1), next_match.group(2).strip()),
        )
    close_match = re.fullmatch(
        r"результат\s+([0-9a-f-]{6,36})\s+"
        r"(won|lost|выиграно|потеряно)\s+(.{1,500})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if close_match is not None:
        raw_stage = close_match.group(2).casefold()
        stage = "won" if raw_stage in {"won", "выиграно"} else "lost"
        return ParsedMemberInteraction(
            "sales-close-text",
            (close_match.group(1), stage, close_match.group(3).strip()),
        )
    followup_match = re.fullmatch(
        r"напомнить\s+([0-9a-f-]{6,36})\s+(1|24|72)\s+(.{1,4000})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if followup_match is not None:
        return ParsedMemberInteraction(
            "sales-followup-text",
            (
                followup_match.group(1),
                followup_match.group(2),
                followup_match.group(3).strip(),
            ),
        )
    optout_match = re.fullmatch(
        r"не\s+писать\s+([0-9a-f-]{6,36})\s+подтвердить",
        raw,
        flags=re.IGNORECASE,
    )
    if optout_match is not None:
        return ParsedMemberInteraction(
            "sales-followup-optout-text",
            (optout_match.group(1),),
        )
    alias = _ALIASES.get(_compact(raw))
    if alias is not None:
        return ParsedMemberInteraction(alias)
    if raw.casefold().startswith(_COMMAND_PREFIX):
        parts = raw[len(_COMMAND_PREFIX) :].split(":")
        action = parts[0].strip().casefold()
        args = tuple(part.strip() for part in parts[1:] if part.strip())
        if action in {
            "menu",
            "menu-all",
            "work",
            "growth",
            "manage",
            "team",
            "today",
            "today-full",
            "customers",
            "customer",
            "bookings",
            "programs",
            "behavior",
            "attention",
            "messengers",
            "autopilot",
            "publications",
            "funnel",
            "money",
            "payments",
            "segments",
            "offers",
            "copy",
            "prices",
            "release",
            "funnel2",
            "retention",
            "recent",
            "system",
            "formats",
            "tariff",
            "members",
            "member",
            "permissions",
            "connect-telegram",
            "connect-vk",
            "connect-max",
            "sales",
            "sales-recent",
            "sales-lead",
            "sales-actions",
            "sales-result-menu",
            "sales-assign",
            "sales-unassign",
            "sales-stage",
            "sales-note-help",
            "sales-next-help",
            "sales-close-help",
            "sales-reopen",
            "sales-handoffs",
            "sales-handoff-claim",
            "sales-handoff-resolve",
            "sales-followup-menu",
            "sales-followup-help",
            "sales-followup-cancel",
            "sales-followup-optout-help",
            "acquire",
        }:
            return ParsedMemberInteraction(action, args)
    return ParsedMemberInteraction("menu")


def _button(label: str, command: str) -> CustomerInteractionButton:
    return CustomerInteractionButton(label=label[:40], command=command)


def _menu_rows(role: PlatformRole) -> tuple[tuple[CustomerInteractionButton, ...], ...]:
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (_button("📊 Работа", "cpm:work"),),
        (_button("💬 Мессенджеры", "cpm:messengers"),),
    ]
    if role in (_MARKETING_ROLES | _CONTENT_ROLES | _AUTOMATION_ROLES):
        rows.append((_button("📈 Рост", "cpm:growth"),))
    if role in _CONNECTION_ROLES:
        rows.append((_button("🛡 Управление", "cpm:manage"),))
    if role in _OWNER_ROLES:
        rows.append((_button("👥 Команда", "cpm:team"),))
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


def _native_primary_action(actor: TenantContext) -> CustomerInteractionButton:
    try:
        next_action = get_growth_cockpit(
            actor=actor,
            period_days=7,
            advertising_loader=lambda **_kwargs: None,
        ).next_action
    except (OSError, RuntimeError, TenantAccessDenied, TenantPermissionDenied, ValueError):
        next_action = None

    if next_action is not None:
        if next_action.action_key == "sales_handoff" and actor.role in _SUPPORT_ROLES:
            return _button("🙋 Ответить клиентам", "cpm:sales-handoffs")
        if next_action.action_key.startswith("sales_plan:") and actor.role in _SUPPORT_ROLES:
            return _button("💬 Продолжить работу с клиентом", "cpm:sales")
        if next_action.action_key == "attribution_review":
            if actor.role in _MARKETING_ROLES:
                return _button("💰 Проверить источники оплат", "cpm:money")
            if actor.role in _SUPPORT_ROLES:
                return _button("📊 Проверить, что происходит", "cpm:today")

    if actor.role in _ACQUISITION_ROLES:
        return _button("🚀 Найти новых клиентов", "cpm:acquire")
    if actor.role in _SUPPORT_ROLES:
        return _button("📊 Проверить, что происходит", "cpm:today")
    if actor.role in (_MARKETING_ROLES | _CONTENT_ROLES | _AUTOMATION_ROLES):
        return _button("📈 Посмотреть результат", "cpm:growth")
    return _button("📚 Открыть программы", "cpm:programs")


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
    primary = _native_primary_action(actor)
    return CustomerInteractionMessage(
        text=(
            heading
            + f"🏠 {_business_name(actor)}\n\n"
            + "ClientPlatform показывает главное действие первым. "
            + "Остальные функции никуда не исчезли — они находятся в «Все возможности»."
        ),
        rows=(
            (primary,),
            (_button("⋯ Все возможности", "cpm:menu-all"),),
        ),
    )


def _menu_all_message(actor: TenantContext) -> CustomerInteractionMessage:
    return CustomerInteractionMessage(
        text=(
            "Все возможности · ClientPlatform\n\n"
            "Выберите раздел. Главная страница остаётся короткой, а полный функционал доступен здесь."
        ),
        rows=(*_menu_rows(actor.role), _back_row()),
    )


def _back_row() -> tuple[CustomerInteractionButton, ...]:
    return (_button("🏠 Рабочий кабинет", "cpm:menu"),)


def _permission_message() -> CustomerInteractionMessage:
    return CustomerInteractionMessage(
        text="Для Вашей роли этот раздел недоступен.",
        rows=(_back_row(),),
    )


def _stale_message() -> CustomerInteractionMessage:
    return CustomerInteractionMessage(
        text="Эта кнопка уже неактуальна. Откройте нужный раздел заново.",
        rows=(_back_row(),),
    )



def _work_message(actor: TenantContext) -> CustomerInteractionMessage:
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if actor.role in _SUPPORT_ROLES:
        rows.extend(
            [
                (_button("📊 Сегодня", "cpm:today"),),
                (_button("📈 Сегодня подробно", "cpm:today-full"),),
                (_button("👥 Клиенты", "cpm:customers:0"),),
                (_button("📅 Записи", "cpm:bookings"),),
                (_button("🧠 Поведение", "cpm:behavior"),),
                (_button("⚠️ Требуют внимания", "cpm:attention"),),
                (_button("💬 Обращения и продажи", "cpm:sales"),),
            ]
        )
    rows.append((_button("📚 Программы", "cpm:programs"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text="Работа · ClientPlatform\n\nОперационные разделы бизнеса:",
        rows=tuple(rows),
    )


_SALES_STAGE_LABELS = {
    "new": "Новое",
    "contacted": "Связались",
    "qualified": "Подтверждён интерес",
    "checkout": "Оформление",
    "won": "Оплатил / выиграно",
    "lost": "Потеряно",
}
_SALES_SOURCE_LABELS = {
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "max": "MAX",
    "website": "Сайт",
    "yandex_direct": "Яндекс Директ",
    "referral": "Рекомендации",
    "manual": "Вручную",
}
_HANDOFF_REASON_LABELS = {
    "explicit_request": "Клиент попросил человека",
    "low_confidence": "Нужна ручная проверка",
    "sensitive_context": "Требуется личное внимание",
    "pricing_exception": "Нестандартные условия",
    "negative_sentiment": "Клиент недоволен",
    "repeated_failure": "Автоматический сценарий не справился",
}
_HANDOFF_SEVERITY_LABELS = {"urgent": "🔴 Срочно", "high": "🟠 Важно", "normal": "🟢 Обычно"}
_FOLLOWUP_CHANNELS = frozenset({"telegram", "vk", "max"})


def _sales_reference_item(actor: TenantContext, reference: str) -> dict[str, Any]:
    needle = str(reference or "").strip().casefold()
    if len(needle) < 6:
        raise ValueError("sales lead reference is too short")
    items = [
        *list_sales_workspace(actor=actor, limit=50),
        *list_sales_workspace_recent_closed(actor=actor, limit=50),
    ]
    matches = [
        item
        for item in items
        if str(item.get("id") or "").casefold().startswith(needle)
    ]
    if len(matches) != 1:
        raise ValueError("sales lead reference is missing or ambiguous")
    return matches[0]


def _sales_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    items = list_sales_workspace(actor=actor, limit=6)
    handoffs = list_sales_workspace_handoffs(actor=actor, limit=1)
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    lines = ["Обращения и продажи", ""]
    if handoffs:
        rows.append((_button("🙋 Нужно подключиться", "cpm:sales-handoffs"),))
    for item in items:
        lead_id = str(item.get("id") or "")
        customer = str(item.get("customer_name") or "Клиент")
        stage = _SALES_STAGE_LABELS.get(str(item.get("stage") or ""), "В работе")
        owner = item.get("assigned_user_id")
        owner_text = f"ответственный {owner}" if owner is not None else "без ответственного"
        lines.append(f"• {customer} · {stage} · {owner_text} · {lead_id[:8]}")
        rows.append((_button(customer[:32], f"cpm:sales-lead:{lead_id}"),))
    if not items:
        lines.append("Открытых обращений сейчас нет.")
    lines.extend(["", "Откройте клиента — ClientPlatform покажет главное действие и все дополнительные возможности."])
    rows.append((_button("🗂 Недавно закрытые", "cpm:sales-recent"),))
    rows.append((_button("📊 Работа", "cpm:work"),))
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


def _sales_recent_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    items = list_sales_workspace_recent_closed(actor=actor, limit=7)
    lines = ["Недавно закрытые обращения", ""]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    for item in items:
        lead_id = str(item.get("id") or "")
        customer = str(item.get("customer_name") or "Клиент")
        stage = _SALES_STAGE_LABELS.get(str(item.get("stage") or ""), "Закрыто")
        lines.append(f"• {customer} · {stage} · {lead_id[:8]}")
        rows.append((_button(customer[:32], f"cpm:sales-lead:{lead_id}"),))
    if not items:
        lines.append("Недавно закрытых обращений нет.")
    rows.append((_button("💬 К обращениям", "cpm:sales"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


def _sales_handoffs_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    items = list_sales_workspace_handoffs(actor=actor, limit=4)
    lines = ["Нужно подключиться лично", ""]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    for index, item in enumerate(items, start=1):
        handoff_id = str(item.get("id") or "")
        status = "взято в работу" if str(item.get("status") or "") == "claimed" else "ожидает"
        reason = _HANDOFF_REASON_LABELS.get(str(item.get("reason") or ""), "Нужно личное внимание")
        severity = _HANDOFF_SEVERITY_LABELS.get(str(item.get("severity") or ""), "🟢 Обычно")
        lines.append(f"{index}. {item.get('customer_name') or 'Клиент'} · {severity} · {reason} · {status}")
        actions: list[CustomerInteractionButton] = []
        if str(item.get("status") or "") == "open":
            actions.append(_button(f"✋ Взять {index}", f"cpm:sales-handoff-claim:{handoff_id}"))
        actions.append(_button(f"✅ Готово {index}", f"cpm:sales-handoff-resolve:{handoff_id}"))
        rows.append(tuple(actions))
    if not items:
        lines.append("Сейчас нет обращений, где требуется личное участие.")
    rows.append((_button("💬 К обращениям", "cpm:sales"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


def _sales_primary_button(
    actor: TenantContext,
    item: dict[str, Any],
    lead_id: str,
) -> CustomerInteractionButton | None:
    stage = str(item.get("stage") or "")
    assigned_user = item.get("assigned_user_id")
    if stage == "lost":
        return _button("↩️ Вернуть в работу", f"cpm:sales-reopen:{lead_id}")
    if stage == "won":
        return _button("📝 Добавить заметку", f"cpm:sales-note-help:{lead_id}")
    if assigned_user is None or int(assigned_user) != int(actor.user_id):
        return _button("👤 Взять обращение", f"cpm:sales-assign:{lead_id}")
    if stage == "new":
        return _button("✅ Я связался", f"cpm:sales-stage:{lead_id}:contacted")
    if stage == "contacted":
        return _button("👍 Клиент заинтересован", f"cpm:sales-stage:{lead_id}:qualified")
    if stage == "qualified":
        return _button("🧾 Перейти к оформлению", f"cpm:sales-stage:{lead_id}:checkout")
    if stage == "checkout":
        return _button("🏁 Указать результат", f"cpm:sales-result-menu:{lead_id}")
    return _button("➡️ Указать следующий шаг", f"cpm:sales-next-help:{lead_id}")


def _sales_lead_message(actor: TenantContext, lead_id: str) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    item = get_sales_workspace_item(actor=actor, lead_id=lead_id)
    if item is None:
        return _stale_message()
    stage = str(item.get("stage") or "")
    assigned_user = item.get("assigned_user_id")
    source_key = str(item.get("attribution_source") or item.get("source_kind") or "").strip().lower()
    source = _SALES_SOURCE_LABELS.get(source_key, "Источник не определён")
    if assigned_user is None:
        owner_text = "не назначен"
    elif int(assigned_user) == int(actor.user_id):
        owner_text = "Вы"
    else:
        owner_text = "другой сотрудник"
    lines = [
        f"💬 {item.get('customer_name') or 'Клиент'}", "",
        f"Стадия: {_SALES_STAGE_LABELS.get(stage, stage or 'В работе')}",
        f"Ответственный: {owner_text}",
        f"Следующий шаг: {item.get('next_action') or 'ClientPlatform подскажет по ходу работы'}",
        f"Срок: {item.get('due_at') or 'не задан'}",
        f"Источник: {source}",
    ]
    if item.get("followup_suppressed"):
        lines.append("Напоминания клиенту: отключены по его просьбе")
    elif item.get("active_followup_id"):
        lines.append(
            f"Напоминание клиенту: {item.get('active_followup_scheduled_at') or 'запланировано'}"
        )
    if item.get("closure_reason"):
        lines.append(f"Причина результата: {item['closure_reason']}")
    if item.get("next_plan_id"):
        approval = "нужно Ваше подтверждение" if item.get("next_plan_requires_approval") else "подтверждение не требуется"
        lines.append(f"ClientPlatform подготовил следующий шаг · {approval}")

    rows: list[tuple[CustomerInteractionButton, ...]] = []
    primary = _sales_primary_button(actor, item, lead_id)
    if primary is not None:
        rows.append((primary,))
    rows.append((_button("⋯ Другие действия", f"cpm:sales-actions:{lead_id}"),))
    rows.append((_button("💬 К обращениям", "cpm:sales"),))
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


def _sales_actions_message(actor: TenantContext, lead_id: str) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    item = get_sales_workspace_item(actor=actor, lead_id=lead_id)
    if item is None:
        return _stale_message()
    stage = str(item.get("stage") or "")
    assigned_user = item.get("assigned_user_id")
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if stage not in {"won", "lost"}:
        if assigned_user is not None and int(assigned_user) == int(actor.user_id):
            rows.append((_button("👤 Снять ответственного", f"cpm:sales-unassign:{lead_id}"),))
        else:
            rows.append((_button("👤 Взять себе", f"cpm:sales-assign:{lead_id}"),))
        rows.append((
            _button("Связались", f"cpm:sales-stage:{lead_id}:contacted"),
            _button("Интерес", f"cpm:sales-stage:{lead_id}:qualified"),
            _button("Оформление", f"cpm:sales-stage:{lead_id}:checkout"),
        ))
        rows.append((
            _button("📝 Заметка", f"cpm:sales-note-help:{lead_id}"),
            _button("➡️ Следующее", f"cpm:sales-next-help:{lead_id}"),
        ))
        rows.append((
            _button("✅ Оплатил", f"cpm:sales-close-help:{lead_id}:won"),
            _button("❌ Не состоялось", f"cpm:sales-close-help:{lead_id}:lost"),
        ))
        followup_source = str(item.get("source_kind") or "").strip().lower()
        followup_allowed = (
            followup_source in _FOLLOWUP_CHANNELS
            and str(item.get("contact_basis") or "") != "none"
            and not bool(item.get("followup_suppressed"))
        )
        if item.get("active_followup_id") or followup_allowed:
            rows.append((_button("✉️ Напомнить клиенту", f"cpm:sales-followup-menu:{lead_id}"),))
    else:
        rows.append((_button("📝 Заметка", f"cpm:sales-note-help:{lead_id}"),))
        if stage == "lost":
            rows.append((_button("↩️ Вернуть в работу", f"cpm:sales-reopen:{lead_id}"),))
    rows.append((_button("← К клиенту", f"cpm:sales-lead:{lead_id}"),))
    return CustomerInteractionMessage(
        text="Другие действия\n\nЗдесь сохранены все дополнительные возможности работы с обращением.",
        rows=tuple(rows),
    )


def _sales_result_message(actor: TenantContext, lead_id: str) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    item = get_sales_workspace_item(actor=actor, lead_id=lead_id)
    if item is None or str(item.get("stage") or "") in {"won", "lost"}:
        return _stale_message()
    return CustomerInteractionMessage(
        text="Чем закончилось обращение?\n\nВыберите результат. Затем ClientPlatform попросит коротко указать причину.",
        rows=(
            (
                _button("✅ Клиент оплатил", f"cpm:sales-close-help:{lead_id}:won"),
                _button("❌ Не состоялось", f"cpm:sales-close-help:{lead_id}:lost"),
            ),
            (_button("← К клиенту", f"cpm:sales-lead:{lead_id}"),),
        ),
    )


def _sales_input_help(lead_id: str, kind: str, stage: str | None = None) -> CustomerInteractionMessage:
    short_id = str(lead_id)[:8]
    if kind == "note":
        example = f"заметка {short_id} Клиент попросил связаться утром"
        title = "Добавить заметку"
    elif kind == "next":
        example = f"следующее {short_id} Позвонить и подтвердить время"
        title = "Следующее действие"
    else:
        result = "выиграно" if stage == "won" else "потеряно"
        example = f"результат {short_id} {result} причина результата"
        title = "Закрыть обращение"
    return CustomerInteractionMessage(
        text=f"{title}\n\nОтправьте одним сообщением:\n{example}",
        rows=((_button("💬 К обращениям", "cpm:sales"),), _back_row()),
    )


def _sales_followup_message(actor: TenantContext, lead_id: str) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    item = get_sales_workspace_item(actor=actor, lead_id=lead_id)
    if item is None or str(item.get("stage") or "") in {"won", "lost"}:
        return _stale_message()
    source = str(item.get("source_kind") or "").strip().lower()
    active = bool(item.get("active_followup_id"))
    suppressed = bool(item.get("followup_suppressed"))
    allowed = source in _FOLLOWUP_CHANNELS and str(item.get("contact_basis") or "") != "none" and not suppressed
    lines = ["Напоминание клиенту", ""]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if suppressed:
        lines.append("Клиент попросил больше не писать по этому каналу.")
    elif active:
        lines.append(f"Напоминание уже запланировано: {item.get('active_followup_scheduled_at') or 'в выбранное время'}.")
        rows.append((_button("✖️ Отменить напоминание", f"cpm:sales-followup-cancel:{lead_id}"),))
    elif allowed:
        lines.append("Сообщение уйдёт только по исходному каналу клиента после явной команды.")
        rows.append((_button("✉️ Запланировать", f"cpm:sales-followup-help:{lead_id}"),))
    else:
        lines.append("Для этого обращения автоматическое напоминание сейчас недоступно.")
    if allowed:
        rows.append((_button("🚫 Больше не писать", f"cpm:sales-followup-optout-help:{lead_id}"),))
    rows.append((_button("↩️ К карточке", f"cpm:sales-lead:{lead_id}"),))
    rows.append((_button("💬 К обращениям", "cpm:sales"),))
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


def _sales_followup_help(lead_id: str) -> CustomerInteractionMessage:
    short_id = str(lead_id)[:8]
    return CustomerInteractionMessage(
        text=(
            "Запланировать напоминание\n\nОтправьте одним сообщением:\n"
            f"напомнить {short_id} 24 Ваш текст клиенту\n\n"
            "Вместо 24 можно указать 1 или 72 часа. Если выбранное время попадёт на ночь, "
            "ClientPlatform перенесёт отправку на ближайшее разрешённое утро."
        ),
        rows=((_button("↩️ К карточке", f"cpm:sales-lead:{lead_id}"),),),
    )


def _sales_followup_optout_help(lead_id: str) -> CustomerInteractionMessage:
    short_id = str(lead_id)[:8]
    return CustomerInteractionMessage(
        text=(
            "Больше не писать клиенту\n\nЕсли клиент действительно попросил больше не писать, "
            "подтвердите отдельным сообщением:\n"
            f"не писать {short_id} подтвердить\n\n"
            "Активное напоминание будет отменено, а новые сообщения по этому каналу будут запрещены."
        ),
        rows=((_button("↩️ К карточке", f"cpm:sales-lead:{lead_id}"),),),
    )


def _sales_mutation_message(
    actor: TenantContext,
    parsed: ParsedMemberInteraction,
    *,
    interaction_key: str,
) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    if parsed.action in {"sales-assign", "sales-unassign"}:
        if len(parsed.args) != 1:
            return _stale_message()
        lead_id = parsed.args[0]
        if parsed.action == "sales-assign":
            assign_sales_workspace_to_actor(actor=actor, lead_id=lead_id)
        else:
            unassign_sales_workspace(actor=actor, lead_id=lead_id)
        return _sales_lead_message(actor, lead_id)
    if parsed.action == "sales-stage":
        if len(parsed.args) != 2 or parsed.args[1] not in {"contacted", "qualified", "checkout"}:
            return _stale_message()
        transition_sales_workspace(
            actor=actor,
            lead_id=parsed.args[0],
            stage=SalesLeadStage(parsed.args[1]),
        )
        return _sales_lead_message(actor, parsed.args[0])
    if parsed.action == "sales-note-text":
        if len(parsed.args) != 2:
            return _stale_message()
        item = _sales_reference_item(actor, parsed.args[0])
        lead_id = str(item["id"])
        add_sales_workspace_note(
            actor=actor,
            lead_id=lead_id,
            note=parsed.args[1],
            interaction_key=interaction_key,
        )
        card = _sales_lead_message(actor, lead_id)
        return CustomerInteractionMessage(text="✅ Заметка сохранена.\n\n" + card.text, rows=card.rows)
    if parsed.action == "sales-next-text":
        if len(parsed.args) != 2:
            return _stale_message()
        item = _sales_reference_item(actor, parsed.args[0])
        lead_id = str(item["id"])
        set_sales_workspace_next_action(
            actor=actor,
            lead_id=lead_id,
            next_action=parsed.args[1],
        )
        return _sales_lead_message(actor, lead_id)
    if parsed.action == "sales-close-text":
        if len(parsed.args) != 3 or parsed.args[1] not in {"won", "lost"}:
            return _stale_message()
        item = _sales_reference_item(actor, parsed.args[0])
        lead_id = str(item["id"])
        transition_sales_workspace(
            actor=actor,
            lead_id=lead_id,
            stage=SalesLeadStage(parsed.args[1]),
            reason=parsed.args[2],
        )
        return _sales_lead_message(actor, lead_id)
    if parsed.action == "sales-reopen":
        if len(parsed.args) != 1:
            return _stale_message()
        reopen_sales_workspace(actor=actor, lead_id=parsed.args[0])
        return _sales_lead_message(actor, parsed.args[0])
    if parsed.action in {"sales-handoff-claim", "sales-handoff-resolve"}:
        if len(parsed.args) != 1:
            return _stale_message()
        if parsed.action == "sales-handoff-claim":
            claim_sales_workspace_handoff(actor=actor, handoff_id=parsed.args[0])
        else:
            resolve_sales_workspace_handoff(actor=actor, handoff_id=parsed.args[0])
        return _sales_handoffs_message(actor)
    if parsed.action == "sales-followup-text":
        if len(parsed.args) != 3 or parsed.args[1] not in {"1", "24", "72"}:
            return _stale_message()
        item = _sales_reference_item(actor, parsed.args[0])
        lead_id = str(item["id"])
        followup = schedule_sales_workspace_followup(
            actor=actor, lead_id=lead_id, message_text=parsed.args[2],
            hours_from_now=int(parsed.args[1]), interaction_key=interaction_key,
        )
        card = _sales_lead_message(actor, lead_id)
        scheduled_at = str(getattr(followup, "scheduled_at", "") or "указанное время")
        return CustomerInteractionMessage(
            text=f"✅ Напоминание клиенту запланировано на {scheduled_at}.\n\n{card.text}", rows=card.rows
        )
    if parsed.action == "sales-followup-cancel":
        if len(parsed.args) != 1:
            return _stale_message()
        cancel_sales_workspace_followup(actor=actor, lead_id=parsed.args[0])
        card = _sales_lead_message(actor, parsed.args[0])
        return CustomerInteractionMessage(text="✅ Напоминание отменено.\n\n" + card.text, rows=card.rows)
    if parsed.action == "sales-followup-optout-text":
        if len(parsed.args) != 1:
            return _stale_message()
        item = _sales_reference_item(actor, parsed.args[0])
        lead_id = str(item["id"])
        suppress_sales_workspace_followup(actor=actor, lead_id=lead_id)
        card = _sales_lead_message(actor, lead_id)
        return CustomerInteractionMessage(
            text="✅ Запрет на follow-up сохранён.\n\n" + card.text, rows=card.rows
        )
    return _stale_message()


def _growth_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in (_MARKETING_ROLES | _CONTENT_ROLES | _AUTOMATION_ROLES):
        return _permission_message()
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if actor.role in _ACQUISITION_ROLES:
        rows.append((_button("🚀 Найти новых клиентов", "cpm:acquire"),))
    if actor.role in _AUTOMATION_ROLES:
        rows.append((_button("🤖 Growth Autopilot", "cpm:autopilot"),))
    if actor.role in _CONTENT_ROLES:
        rows.append((_button("📣 Публикации", "cpm:publications"),))
    if actor.role in _MARKETING_ROLES:
        rows.extend(
            [
                (_button("📉 Путь до заявки", "cpm:funnel"),),
                (_button("💰 Деньги и клиенты", "cpm:money"),),
                (_button("💳 Оплаты", "cpm:payments"),),
                (_button("🧲 Группы клиентов", "cpm:segments"),),
            ]
        )
    if actor.role in (_CONTENT_ROLES | _MARKETING_ROLES):
        rows.append((_button("🧪 Предложения", "cpm:offers"),))
        rows.append((_button("✍️ Тексты", "cpm:copy"),))
    if actor.role in _MARKETING_ROLES:
        rows.append((_button("💡 Подсказка по ценам", "cpm:prices"),))
    # Native interaction messages allow at most ten buttons. Keep the primary
    # acquisition action and navigation invariant when all tools are available.
    rows = rows[:9]
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text="Рост · ClientPlatform\n\nМаркетинг, воронка и удержание:",
        rows=tuple(rows),
    )


def _acquisition_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _ACQUISITION_ROLES:
        return _permission_message()
    public_base = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip()
    if not public_base:
        return CustomerInteractionMessage(
            text=(
                "Найти новых клиентов\n\nПубличная ссылка ClientPlatform пока не настроена. "
                "Ничего не опубликовано и расходы не запущены."
            ),
            rows=((_button("📈 Рост", "cpm:growth"),), _back_row()),
        )
    try:
        prepared = prepare_nearest_acquisition_destination(
            actor=actor,
            public_base_url=public_base,
            attribution_channel=PromotionChannel.WEBSITE,
        )
    except (PromotionError, TenantPermissionDenied, ValueError):
        return CustomerInteractionMessage(
            text=(
                "Найти новых клиентов\n\nНе удалось безопасно подготовить ссылку. "
                "Ничего не опубликовано и расходы не запущены."
            ),
            rows=((_button("🔄 Проверить снова", "cpm:acquire"),), (_button("📈 Рост", "cpm:growth"),), _back_row()),
        )
    if prepared is None:
        return CustomerInteractionMessage(
            text=(
                "Найти новых клиентов\n\nСначала нужно открыть хотя бы одно будущее "
                "время для записи. После этого ClientPlatform сама выберет ближайшее."
            ),
            rows=((_button("📅 Записи", "cpm:bookings"),), (_button("📈 Рост", "cpm:growth"),), _back_row()),
        )
    destination = prepared.destination
    if not destination.has_native_messenger_destination:
        return CustomerInteractionMessage(
            text=(
                "Найти новых клиентов\n\nПредложение подготовлено, но у бизнеса пока "
                "нет публичного Telegram, ВКонтакте или MAX. Ссылку не публикую, "
                "чтобы не вести клиента в тупик."
            ),
            rows=((_button("💬 Мессенджеры", "cpm:messengers"),), (_button("📈 Рост", "cpm:growth"),), _back_row()),
        )
    names = {ConnectionPlatform.TELEGRAM: "Telegram", ConnectionPlatform.VK: "ВКонтакте", ConnectionPlatform.MAX: "MAX"}
    channels = ", ".join(names[item.platform] for item in destination.messenger_destinations)
    creative = prepared.promotion.campaign.creative
    slot = prepared.promotion.slot
    return CustomerInteractionMessage(
        text=(
            "🚀 Найти новых клиентов\n\n"
            f"Ближайшее свободное время: {slot.local_start} · {slot.offering_title}.\n"
            f"{creative.headline}\n\n{creative.primary_text}\n\n"
            f"Записаться: {destination.public_url}\n\nДоступно клиенту: {channels}. "
            "Источник сохранится независимо от выбранного мессенджера. Это только "
            "готовый материал и измеряемая ссылка — платная реклама не запускается."
        ),
        rows=((_button("🔄 Обновить", "cpm:acquire"),), (_button("📈 Рост", "cpm:growth"),), _back_row()),
    )


def _manage_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (_button("💬 Мессенджеры", "cpm:messengers"),),
        (_button("✅ Готовность системы", "cpm:release"),),
        (_button("🧲 Продажи и воронка", "cpm:funnel2"),),
        (_button("🧩 Удержание", "cpm:retention"),),
        (_button("🧾 Последние действия", "cpm:recent"),),
        (_button("🛡 Проверить систему", "cpm:system"),),
        (_button("🧩 Форматы работы", "cpm:formats"),),
    ]
    if actor.role in _OWNER_ROLES:
        rows.append((_button("💳 Тариф", "cpm:tariff"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text="Управление · ClientPlatform\n\nСостояние, каналы и системные настройки:",
        rows=tuple(rows),
    )


def _team_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _OWNER_ROLES:
        return _permission_message()
    return CustomerInteractionMessage(
        text="Команда · ClientPlatform\n\nСотрудники, роли и доступы:",
        rows=(
            (_button("👥 Роли команды", "cpm:members:0"),),
            (_button("🔐 Доступы сотрудников", "cpm:permissions"),),
            _back_row(),
        ),
    )


def _today_full_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    profile = get_business_profile(actor=actor)
    summary = business_delivery_summary(actor=actor)
    capabilities = list_business_capabilities(actor=actor)
    slots = list_booking_slots(actor=actor, include_unavailable=True)
    progress = list_business_program_progress(actor=actor, limit=100)
    active_capabilities = sum(item.status == CapabilityStatus.ACTIVE for item in capabilities)
    open_slots = sum(item.slot.status == BookingSlotStatus.OPEN for item in slots)
    completed = sum(item.completed_lessons for item in progress)
    total = sum(item.total_lessons for item in progress)
    return CustomerInteractionMessage(
        text=(
            "Сегодня · подробно\n\n"
            f"Бизнес: {_business_name(actor)}\n"
            f"Профиль: {profile.status.value}\n"
            f"Клиентов: {summary.customers}\n"
            f"Программ: {summary.programs}\n"
            f"Форматов работы: {active_capabilities}\n"
            f"Свободных времён: {open_slots}\n\n"
            f"В очереди: {summary.dispatch_pending}\n"
            f"Отправлено: {summary.dispatch_sent}\n"
            f"Требуют внимания: {summary.dispatch_attention}\n"
            f"Прохождение материалов: {completed}/{total}"
        ),
        rows=((_button("📊 Кратко", "cpm:today"),), _back_row()),
    )


def _page_number(args: tuple[str, ...], *, default: int = 0) -> int:
    if not args:
        return default
    raw = args[0]
    if not raw.isdigit():
        raise ValueError("invalid native page")
    page = int(raw)
    if page < 0 or page > 10000:
        raise ValueError("native page is outside range")
    return page


def _customer_message(actor: TenantContext, customer_id: str) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    record = get_customer(actor=actor, customer_id=customer_id)
    identity_lines = [
        (
            f"• {item.platform.value}: @{item.username}"
            if item.username
            else f"• {item.platform.value}: {item.display_name or item.external_subject}"
        )
        for item in record.identities
    ]
    return CustomerInteractionMessage(
        text=(
            "Карточка клиента\n\n"
            f"Имя: {record.customer.display_name or 'не указано'}\n"
            f"Статус: {record.customer.status.value}\n"
            f"Создан: {record.customer.created_at}\n\n"
            "Контакты:\n"
            + ("\n".join(identity_lines) if identity_lines else "• не подключены")
        ),
        rows=((_button("👥 К списку", "cpm:customers:0"),), _back_row()),
    )


def _behavior_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    progress = list_business_program_progress(actor=actor, limit=25)
    if not progress:
        text = "Поведение\n\nПрохождение программ ещё не началось."
    else:
        lines = []
        for item in progress[:15]:
            name = item.customer_display_name or f"Клиент {item.customer_id[:8]}"
            lines.append(
                f"• {name}: «{item.program_title}» — "
                f"{item.completed_lessons}/{item.total_lessons} ({item.percent_complete}%)"
            )
        text = "Поведение\n\n" + "\n".join(lines)
    return CustomerInteractionMessage(text=text, rows=(_back_row(),))


def _attention_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    summary = business_delivery_summary(actor=actor)
    text = (
        "Требуют внимания\n\n"
        f"Ошибки отправки: {summary.dispatch_attention}\n"
        f"Ожидают отправки: {summary.dispatch_pending}\n\n"
    )
    text += (
        "Нужно проверить неотправленные материалы и подключения."
        if summary.dispatch_attention or summary.dispatch_pending
        else "Сейчас критических задач нет."
    )
    return CustomerInteractionMessage(text=text, rows=(_back_row(),))


def _growth_report_message(actor: TenantContext, action: str) -> CustomerInteractionMessage:
    allowed = {
        "autopilot": _AUTOMATION_ROLES,
        "publications": _CONTENT_ROLES,
        "funnel": _MARKETING_ROLES,
        "money": _MARKETING_ROLES,
        "payments": _MARKETING_ROLES,
        "segments": _MARKETING_ROLES,
        "offers": _CONTENT_ROLES | _MARKETING_ROLES,
        "copy": _CONTENT_ROLES | _MARKETING_ROLES,
        "prices": _MARKETING_ROLES,
    }
    if actor.role not in allowed[action]:
        return _permission_message()
    profile = get_business_profile(actor=actor)
    summary = business_delivery_summary(actor=actor)
    capabilities = list_business_capabilities(actor=actor)
    slots = list_booking_slots(actor=actor, include_unavailable=True)
    progress = list_business_program_progress(actor=actor, limit=100)
    active = [item for item in capabilities if item.status == CapabilityStatus.ACTIVE]
    enrolled = len({item.customer_id for item in progress})
    completed = sum(
        item.total_lessons > 0 and item.completed_lessons >= item.total_lessons
        for item in progress
    )
    stalled = sum(item.total_lessons > item.completed_lessons for item in progress)
    open_slots = sum(item.slot.status == BookingSlotStatus.OPEN for item in slots)
    sections = {
        "autopilot": (
            "Growth Autopilot\n\n"
            f"Форматов: {len(active)}\nПрограмм: {summary.programs}\n"
            f"Очередь отправки: {summary.dispatch_pending}\n\n"
            "Автопилот работает только в подтверждённых владельцем границах."
        ),
        "publications": (
            "Публикации\n\nПубликационный контур ещё не подключён к этому бизнесу."
        ),
        "funnel": (
            "Путь до заявки\n\n"
            f"Клиенты: {summary.customers}\nВ программах: {enrolled}\n"
            f"Завершили: {completed}\nСвободных записей: {open_slots}"
        ),
        "money": (
            "Деньги и клиенты\n\n"
            f"Активных клиентов: {summary.customers}\n"
            f"Активных программ: {summary.programs}\n"
            "Заказы и выручка появятся после подключения платёжного модуля."
        ),
        "payments": "Оплаты\n\nПлатёжная система бизнеса ещё не подключена.",
        "segments": (
            "Группы клиентов\n\n"
            f"Все клиенты: {summary.customers}\nВ программах: {enrolled}\n"
            f"Завершили: {completed}\nОстановились: {stalled}\n"
            f"Без программы: {max(0, summary.customers - enrolled)}"
        ),
        "offers": (
            "Проверка предложений\n\n"
            f"Форматов работы: {len(active)}\nПрограмм: {summary.programs}\n\n"
            "Сравнение предложений станет точнее после накопления заявок и оплат."
        ),
        "copy": (
            "Подготовить тексты\n\n"
            f"Описание бизнеса:\n{profile.activity_description}"
        ),
        "prices": (
            "Подсказка по ценам\n\n"
            "В предложениях пока нет структурированного поля цены."
        ),
    }
    return CustomerInteractionMessage(
        text=sections[action],
        rows=((_button("📈 Рост", "cpm:growth"),), _back_row()),
    )


def _admin_report_message(actor: TenantContext, action: str) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    profile = get_business_profile(actor=actor)
    summary = business_delivery_summary(actor=actor)
    capabilities = list_business_capabilities(actor=actor)
    slots = list_booking_slots(actor=actor, include_unavailable=True)
    customers = list_customers(actor=actor)
    programs = list_programs(actor=actor)
    progress = list_business_program_progress(actor=actor, limit=100)
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
            "Release gate\n\n"
            f"Профиль бизнеса: {'✅' if profile.status.value == 'ready' else '❌'}\n"
            f"Форматы работы: {'✅' if active else '❌'}\n"
            f"Ошибки отправки: {'✅' if summary.dispatch_attention == 0 else '❌'}\n\n"
            f"Итог: {'ГОТОВО' if release_ok else 'ТРЕБУЕТ НАСТРОЙКИ'}"
        ),
        "funnel2": (
            "Воронка 2.0\n\n"
            f"Клиенты: {summary.customers}\nВ программах: {enrolled}\n"
            f"Завершили: {complete}\nДоступных записей: {open_slots}\n"
            f"Отправлено материалов: {summary.dispatch_sent}"
        ),
        "retention": (
            "Удержание\n\n"
            f"Клиентов всего: {summary.customers}\n"
            f"Незавершённых прохождений: {len(incomplete)}\n"
            f"Без активной программы: {max(0, summary.customers - enrolled)}"
        ),
        "recent": (
            "Последние действия\n\n"
            + (
                "\n".join(f"• {label} — {stamp}" for stamp, label in recent_items)
                if recent_items
                else "Действий пока нет."
            )
        ),
        "system": (
            "Системные проверки\n\n"
            "Tenant-доступ: ✅\nPostgreSQL-чтение: ✅\n"
            f"Профиль бизнеса: {'✅' if profile.status.value == 'ready' else '⚠️'}\n"
            f"Очередь отправки: {'✅' if summary.dispatch_attention == 0 else '⚠️'}\n"
            f"Программы: {summary.programs}\nКлиенты: {summary.customers}"
        ),
    }
    return CustomerInteractionMessage(
        text=sections[action],
        rows=((_button("🛡 Управление", "cpm:manage"),), _back_row()),
    )


def _formats_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    capabilities = list_business_capabilities(actor=actor, include_disabled=True)
    lines = [
        f"{'✅' if item.status == CapabilityStatus.ACTIVE else '➖'} {item.title}"
        for item in capabilities
    ]
    return CustomerInteractionMessage(
        text="Форматы работы\n\n" + ("\n".join(lines) if lines else "Форматы ещё не выбраны."),
        rows=((_button("🛡 Управление", "cpm:manage"),), _back_row()),
    )


def _tariff_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _OWNER_ROLES:
        return _permission_message()
    return CustomerInteractionMessage(
        text=(
            "Тариф ClientPlatform\n\n"
            "Тарифный модуль пока не активирован для этого бизнеса. "
            "Текущие данные и настройки продолжают работать."
        ),
        rows=((_button("🛡 Управление", "cpm:manage"),), _back_row()),
    )


def _list_members(actor: TenantContext) -> list[dict[str, Any]]:
    if actor.role not in _OWNER_ROLES:
        raise TenantPermissionDenied("team section is owner-only")
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        rows = conn.execute(
            """
            SELECT user_id, role, status
            FROM business_members
            WHERE business_id=?
            ORDER BY CASE WHEN role='owner' THEN 0 ELSE 1 END, created_at, id
            """,
            (current.business_id,),
        ).fetchall()
    return [
        {
            "user_id": int(row["user_id"] if hasattr(row, "keys") else row[0]),
            "role": str(row["role"] if hasattr(row, "keys") else row[1]),
            "status": str(row["status"] if hasattr(row, "keys") else row[2]),
        }
        for row in rows
    ]


def _members_message(actor: TenantContext, page: int) -> CustomerInteractionMessage:
    members = _list_members(actor)
    page_size = 7
    count = max(1, (len(members) + page_size - 1) // page_size)
    if page >= count:
        raise ValueError("member page is outside result set")
    current = members[page * page_size : (page + 1) * page_size]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    lines: list[str] = []
    for item in current:
        role = PlatformRole(item["role"])
        marker = "✅" if item["status"] == "active" else "➖"
        label = f"{marker} {item['user_id']} · {_ROLE_LABELS.get(role, role.value)}"
        lines.append(f"• {label}")
        rows.append((_button(label, f"cpm:member:{item['user_id']}"),))
    navigation: list[CustomerInteractionButton] = []
    if page > 0:
        navigation.append(_button("⬅️ Назад", f"cpm:members:{page - 1}"))
    if page + 1 < count:
        navigation.append(_button("Вперёд ➡️", f"cpm:members:{page + 1}"))
    if navigation:
        rows.append(tuple(navigation))
    rows.append((_button("👥 Команда", "cpm:team"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            "Роли команды\n\n"
            + ("\n".join(lines) if lines else "Сотрудников пока нет.")
            + f"\n\nСтраница {page + 1}/{count}"
        ),
        rows=tuple(rows),
    )


def _member_message(actor: TenantContext, user_id: int) -> CustomerInteractionMessage:
    member = next((item for item in _list_members(actor) if item["user_id"] == user_id), None)
    if member is None:
        raise ValueError("member was not found")
    role = PlatformRole(member["role"])
    return CustomerInteractionMessage(
        text=(
            "Сотрудник\n\n"
            f"Account ID: {user_id}\n"
            f"Роль: {_ROLE_LABELS.get(role, role.value)}\n"
            f"Статус: {member['status']}"
        ),
        rows=((_button("👥 К команде", "cpm:members:0"),), _back_row()),
    )


def _permissions_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _OWNER_ROLES:
        return _permission_message()
    permissions = {
        PlatformRole.OWNER: "Все разделы, сотрудники, клиенты, подключения",
        PlatformRole.ADMINISTRATOR: "Бизнес, клиенты, аналитика, подключения",
        PlatformRole.MANAGER: "Клиенты, записи, программы, операционная аналитика",
        PlatformRole.CONTENT_MANAGER: "Программы, материалы, публикации",
        PlatformRole.MARKETER: "Воронки, сегменты, предложения, маркетинг",
        PlatformRole.ANALYST: "Отчёты, воронки, удержание",
        PlatformRole.SUPPORT: "Клиенты, проблемные отправки, поддержка",
    }
    lines = [
        f"{_ROLE_LABELS[role]}: {text}"
        for role, text in permissions.items()
    ]
    return CustomerInteractionMessage(
        text="Доступы сотрудников\n\n" + "\n\n".join(lines),
        rows=((_button("👥 Команда", "cpm:team"),), _back_row()),
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
            (_button("👥 Клиенты", "cpm:customers:0"),),
            (_button("📅 Записи", "cpm:bookings"),),
            _back_row(),
        ),
    )


def _customers_message(actor: TenantContext, page: int = 0) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    customers = list_customers(actor=actor)
    page_size = 7
    count = max(1, (len(customers) + page_size - 1) // page_size)
    if page >= count:
        raise ValueError("customer page is outside result set")
    current = customers[page * page_size : (page + 1) * page_size]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    lines: list[str] = []
    for customer in current:
        name = str(customer.display_name or "").strip() or f"Клиент {customer.id[:8]}"
        lines.append(f"• {name}")
        rows.append((_button(name, f"cpm:customer:{customer.id}"),))
    navigation: list[CustomerInteractionButton] = []
    if page > 0:
        navigation.append(_button("⬅️ Назад", f"cpm:customers:{page - 1}"))
    if page + 1 < count:
        navigation.append(_button("Вперёд ➡️", f"cpm:customers:{page + 1}"))
    if navigation:
        rows.append(tuple(navigation))
    rows.append((_button("📊 Работа", "cpm:work"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            "Клиенты\n\n"
            + ("\n".join(lines) if lines else "Активных клиентов пока нет.")
            + f"\n\nСтраница {page + 1}/{count}"
        ),
        rows=tuple(rows),
    )


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
    current_platform: ConnectionPlatform,
) -> CustomerInteractionMessage:
    connections = list_connections(actor=actor)
    labels = {
        ConnectionPlatform.TELEGRAM: ("✈️", "Telegram"),
        ConnectionPlatform.VK: ("🔵", "ВКонтакте"),
        ConnectionPlatform.MAX: ("🟣", "MAX"),
    }
    by_platform = {platform: [] for platform in labels}
    for item in connections:
        by_platform[item.platform].append(item.status.value)
    lines = ["Мессенджеры", ""]
    active: set[ConnectionPlatform] = set()
    for platform in (
        ConnectionPlatform.VK,
        ConnectionPlatform.MAX,
        ConnectionPlatform.TELEGRAM,
    ):
        icon, title = labels[platform]
        statuses = by_platform[platform]
        if "active" in statuses:
            active.add(platform)
        state = ", ".join(statuses) if statuses else "не подключён"
        current = " · сейчас здесь" if platform == current_platform else ""
        lines.append(f"{icon} {title} — {state}{current}")

    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if actor.role in _CONNECTION_ROLES and setup_available:
        connect_labels = {
            ConnectionPlatform.TELEGRAM: "✈️ Подключить Telegram",
            ConnectionPlatform.VK: "🔵 Подключить ВКонтакте",
            ConnectionPlatform.MAX: "🟣 Подключить MAX",
        }
        for platform in (
            ConnectionPlatform.TELEGRAM,
            ConnectionPlatform.VK,
            ConnectionPlatform.MAX,
        ):
            if platform not in active:
                rows.append(
                    (
                        _button(
                            connect_labels[platform],
                            f"cpm:connect-{platform.value}",
                        ),
                    )
                )

    switch_labels = {
        ConnectionPlatform.TELEGRAM: "✈️ Перейти в Telegram",
        ConnectionPlatform.VK: "🔵 Перейти во ВКонтакте",
        ConnectionPlatform.MAX: "🟣 Перейти в MAX",
    }
    try:
        switchable = available_staff_messenger_switches(actor)
    except (RuntimeError, ValueError):
        switchable = ()
    for platform in switchable:
        if platform == current_platform:
            continue
        rows.append(
            (
                _button(
                    switch_labels[platform],
                    build_staff_switch_command(actor, platform),
                ),
            )
        )
    rows.append(_back_row())
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


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
        # The exception may contain a provider token, secret reference or raw
        # capability. Log only stable context; never serialize exception text
        # or traceback across this credential-adjacent boundary.
        log.error(
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
    channel_name = {
        ConnectionPlatform.TELEGRAM: "Telegram",
        ConnectionPlatform.VK: "ВКонтакте",
        ConnectionPlatform.MAX: "MAX",
    }[platform]
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
    current_platform: ConnectionPlatform = ConnectionPlatform.TELEGRAM,
) -> CustomerInteractionMessage:
    if parsed.action == "menu":
        return _menu_message(actor, linked=linked)
    try:
        if parsed.action == "menu-all":
            return _menu_all_message(actor)
        if parsed.action == "work":
            return _work_message(actor)
        if parsed.action == "growth":
            return _growth_message(actor)
        if parsed.action == "acquire":
            return _acquisition_message(actor)
        if parsed.action == "manage":
            return _manage_message(actor)
        if parsed.action == "team":
            return _team_message(actor)
        if parsed.action == "today":
            if actor.role not in _SUPPORT_ROLES:
                return _permission_message()
            return _today_message(actor)
        if parsed.action == "today-full":
            return _today_full_message(actor)
        if parsed.action == "customers":
            return _customers_message(actor, _page_number(parsed.args))
        if parsed.action == "customer":
            if len(parsed.args) != 1:
                return _stale_message()
            return _customer_message(actor, parsed.args[0])
        if parsed.action == "bookings":
            if actor.role not in _SUPPORT_ROLES:
                return _permission_message()
            return _bookings_message(actor)
        if parsed.action == "programs":
            return _programs_message(actor)
        if parsed.action == "behavior":
            return _behavior_message(actor)
        if parsed.action == "attention":
            return _attention_message(actor)
        if parsed.action == "sales":
            return _sales_message(actor)
        if parsed.action == "sales-recent":
            return _sales_recent_message(actor)
        if parsed.action == "sales-handoffs":
            return _sales_handoffs_message(actor)
        if parsed.action == "sales-lead":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_lead_message(actor, parsed.args[0])
        if parsed.action == "sales-actions":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_actions_message(actor, parsed.args[0])
        if parsed.action == "sales-result-menu":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_result_message(actor, parsed.args[0])
        if parsed.action in {
            "sales-assign",
            "sales-unassign",
            "sales-stage",
            "sales-note-text",
            "sales-next-text",
            "sales-close-text",
            "sales-reopen",
            "sales-handoff-claim",
            "sales-handoff-resolve",
            "sales-followup-text",
            "sales-followup-cancel",
            "sales-followup-optout-text",
        }:
            return _sales_mutation_message(
                actor, parsed, interaction_key=setup_key
            )
        if parsed.action == "sales-note-help":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_input_help(parsed.args[0], "note")
        if parsed.action == "sales-next-help":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_input_help(parsed.args[0], "next")
        if parsed.action == "sales-close-help":
            if len(parsed.args) != 2 or parsed.args[1] not in {"won", "lost"}:
                return _stale_message()
            return _sales_input_help(parsed.args[0], "close", parsed.args[1])
        if parsed.action == "sales-followup-menu":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_followup_message(actor, parsed.args[0])
        if parsed.action == "sales-followup-help":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_followup_help(parsed.args[0])
        if parsed.action == "sales-followup-optout-help":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_followup_optout_help(parsed.args[0])
        if parsed.action == "messengers":
            return _messengers_message(
                actor,
                setup_available=setup_issuer is not None,
                current_platform=current_platform,
            )
        if parsed.action in {
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
            return _growth_report_message(actor, parsed.action)
        if parsed.action in {"release", "funnel2", "retention", "recent", "system"}:
            return _admin_report_message(actor, parsed.action)
        if parsed.action == "formats":
            return _formats_message(actor)
        if parsed.action == "tariff":
            return _tariff_message(actor)
        if parsed.action == "members":
            return _members_message(actor, _page_number(parsed.args))
        if parsed.action == "member":
            if len(parsed.args) != 1 or not parsed.args[0].isdigit():
                return _stale_message()
            return _member_message(actor, int(parsed.args[0]))
        if parsed.action == "permissions":
            return _permissions_message(actor)
        if parsed.action == "connect-telegram":
            return _setup_message(
                actor,
                platform=ConnectionPlatform.TELEGRAM,
                setup_issuer=setup_issuer,
                setup_key=setup_key,
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
    except SalesError:
        return _stale_message()
    except ValueError:
        return _stale_message()
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
    action_payload = "\x1f".join((parsed.action, *parsed.args))
    action_digest = hashlib.sha256(action_payload.encode("utf-8")).hexdigest()[:20]
    action_key = f"{parsed.action}:{action_digest}"
    interaction_key = (
        f"route:{route.id}:event:{provider_event_id}:"
        + f"member:{actor.user_id}:action:{action_key}"
    )
    interaction = _render(
        actor,
        parsed,
        linked=resolution.linked,
        setup_issuer=setup_issuer,
        setup_key=interaction_key,
        current_platform=route.platform,
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
