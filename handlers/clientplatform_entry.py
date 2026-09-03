from __future__ import annotations

import asyncio
import importlib
import logging
import sqlite3
from typing import Any

from aiogram import F, Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, CallbackQuery, Message

from clientplatform.application.activity import claim_customer_invite
from clientplatform.application.bookings import list_customer_businesses
from clientplatform.application.tenancy import (
    get_owner_control_workspace,
    list_accessible_businesses,
    resolve_tenant_context,
)
from clientplatform.domain.activity import ActivityInvariantViolation
from services.db.core import db_operation_deadline

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_entry")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

log = logging.getLogger(__name__)
_START_TIMEOUT_SECONDS = 12.0
_START_STORAGE_DEADLINE_SECONDS = 8.0
_TELEGRAM_SAFE_TEXT_LIMIT = 3900
_SUPPORT_QUEUE_SUMMARY_PREVIEW = 240


async def register_clientplatform_bot_commands(bot: Bot) -> bool:
    """Expose the canonical entry commands in Telegram's command menu."""

    try:
        confirmed = await bot.set_my_commands(
            [
                BotCommand(command="start", description="Открыть ClientPlatform"),
                BotCommand(command="admin", description="Открыть админку бизнеса"),
                BotCommand(command="mybot", description="Управление моим Telegram-ботом"),
                BotCommand(command="privacy", description="Конфиденциальность и данные"),
                BotCommand(command="mydata", description="Экспортировать мои данные"),
                BotCommand(command="deletemydata", description="Удалить мои данные"),
                BotCommand(command="cancel", description="Отменить текущий шаг"),
            ]
        )
    except TelegramAPIError:
        log.warning("Failed to register ClientPlatform Telegram commands", exc_info=True)
        return False
    return confirmed is True


def _entry_keyboard():
    return control._keyboard(
        [
            [("Мои бизнесы", "cp:entry:businesses")],
            [("Мои специалисты и программы", "cp:entry:clients")],
        ]
    )


def _owner_landing_keyboard():
    return control._keyboard(
        [[("Подключить мой бизнес", "business")]]
    )


def _owner_landing_text() -> str:
    return (
        "Добро пожаловать в ClientPlatform.\n\n"
        "Вы открыли управляющий вход для владельца бизнеса. "
        "Нажмите «Подключить мой бизнес», чтобы создать новое рабочее пространство."
    )


async def _send_business_choice(
    message: Message,
    *,
    user_id: int,
    accesses: list[Any],
    state: FSMContext,
) -> None:
    if len(accesses) > 1:
        await state.clear()
        await message.answer(
            "Выберите бизнес, с которым хотите работать:",
            reply_markup=control._business_choice_keyboard(accesses),
        )
        return
    await control._resume_business(
        message,
        user_id=user_id,
        business_id=accesses[0].business.id,
        state=state,
    )


async def _safe_edit_start_status(status_message: Message | None, text: str) -> None:
    if status_message is None:
        return
    try:
        await status_message.edit_text(text)
    except TelegramAPIError:
        log.warning("Failed to edit ClientPlatform /start status", exc_info=True)


async def _safe_delete_start_status(status_message: Message | None) -> None:
    if status_message is None:
        return
    try:
        await status_message.delete()
    except TelegramAPIError:
        log.debug("Failed to delete ClientPlatform /start status", exc_info=True)


async def _dispatch_clientplatform_start(
    message: Message,
    state: FSMContext,
    *,
    user_id: int,
    managed_bot_business_id: str | None,
) -> None:
    if managed_bot_business_id is not None:
        links = await asyncio.to_thread(
            list_customer_businesses,
            telegram_user_id=user_id,
        )
        managed_links = [
            link for link in links if link.business_id == managed_bot_business_id
        ]
        await state.clear()
        if not managed_links:
            await message.answer(
                "Не удалось открыть кабинет этого специалиста. "
                "Попробуйте ещё раз через несколько секунд."
            )
            return
        await control._send_client_portal(message, links=managed_links)
        return

    payload = control._start_payload(message)
    if payload.casefold().startswith("cpo_"):
        # Landing owner links are explicit owner intent. Resolve only owner
        # workspaces here and never let an existing customer relationship
        # redirect this start into the customer portal.
        accesses = await asyncio.to_thread(list_accessible_businesses, user_id=user_id)
        if accesses:
            await _send_business_choice(
                message,
                user_id=user_id,
                accesses=accesses,
                state=state,
            )
            return
        await state.clear()
        await message.answer(
            _owner_landing_text(),
            reply_markup=_owner_landing_keyboard(),
        )
        return

    if payload.startswith("cpj_"):
        token = payload.removeprefix("cpj_")
        user = message.from_user
        try:
            claim = await asyncio.to_thread(
                claim_customer_invite,
                token=token,
                telegram_user_id=user_id,
                username=None if user is None else user.username,
                display_name=None if user is None else user.full_name,
            )
        except ActivityInvariantViolation as exc:
            await state.clear()
            await message.answer(str(exc))
            return
        await state.clear()
        detail = "Вы уже были подключены." if claim.already_connected else "Подключение завершено."
        await message.answer(
            f"Вы подключены к «{claim.business_name}». {detail}\n"
            "Материалы и сообщения этого специалиста будут приходить сюда.",
            reply_markup=control._client_portal_keyboard(claim.business_id),
        )
        return

    accesses, links = await asyncio.gather(
        asyncio.to_thread(list_accessible_businesses, user_id=user_id),
        asyncio.to_thread(list_customer_businesses, telegram_user_id=user_id),
    )
    if accesses and links:
        await state.clear()
        await message.answer(
            "У Вас есть два рабочих пространства. Выберите, куда перейти:",
            reply_markup=_entry_keyboard(),
        )
        return
    if accesses:
        await _send_business_choice(
            message,
            user_id=user_id,
            accesses=accesses,
            state=state,
        )
        return
    if links:
        await state.clear()
        await control._send_client_portal(message, links=links)
        return

    simple = importlib.import_module(
        ".clientplatform_simple_experience",
        __package__,
    )
    await state.clear()
    await message.answer(
        simple.welcome_text(),
        reply_markup=simple.welcome_keyboard(),
    )


@router.callback_query(F.data == "business")
async def clientplatform_owner_business_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Start owner onboarding from the canonical landing action."""

    await state.clear()
    await state.set_state(control.ClientPlatformControlState.business_name)
    await callback.answer()
    await control._callback_message(callback).answer(
        "Как называется Ваше дело, проект или практика?\n\n"
        "Например: «Практика Анны», «Автосервис Мотор» или «Школа английского»."
    )


@router.message(CommandStart())
async def clientplatform_entry_start(
    message: Message,
    state: FSMContext,
    managed_bot_business_id: str | None = None,
) -> None:
    """Acknowledge `/start` before storage work and fail visibly on stalls."""

    user_id = control._user_id(message)
    status_message = await message.answer("Открываю…")
    try:
        with db_operation_deadline(_START_STORAGE_DEADLINE_SECONDS):
            await asyncio.wait_for(
                _dispatch_clientplatform_start(
                    message,
                    state,
                    user_id=user_id,
                    managed_bot_business_id=managed_bot_business_id,
                ),
                timeout=_START_TIMEOUT_SECONDS,
            )
    except TimeoutError:
        log.error(
            "ClientPlatform /start timed out user_id=%s timeout_seconds=%s",
            user_id,
            _START_TIMEOUT_SECONDS,
        )
        await _safe_edit_start_status(
            status_message,
            "ClientPlatform отвечает дольше обычного. "
            "Нажмите «Старт» ещё раз через несколько секунд.",
        )
        return
    except sqlite3.DatabaseError:
        log.exception("ClientPlatform /start storage failed user_id=%s", user_id)
        await _safe_edit_start_status(
            status_message,
            "Не удалось открыть ClientPlatform. "
            "Нажмите «Старт» ещё раз — сохранённые данные не потеряны.",
        )
        return
    except RuntimeError:
        log.exception("ClientPlatform /start dispatch failed user_id=%s", user_id)
        await _safe_edit_start_status(
            status_message,
            "Не удалось открыть ClientPlatform. "
            "Нажмите «Старт» ещё раз — сохранённые данные не потеряны.",
        )
        return

    await _safe_delete_start_status(status_message)


@router.message(Command("admin"))
async def clientplatform_admin_command(message: Message, state: FSMContext) -> None:
    """Open the owner administration panel before generic FSM handlers."""

    admin = importlib.import_module(".clientplatform_admin", __package__)
    await admin.open_admin_command(message, state)


@router.message(Command("mybot"))
async def clientplatform_mybot_command(message: Message, state: FSMContext) -> None:
    """Route `/mybot` before generic FSM text handlers can persist it as data."""

    bot_setup = importlib.import_module(".clientplatform_bot_setup", __package__)
    await bot_setup.open_my_bot_command(message, state)


def _telegram_support_actor(user_id: int):
    accesses = list(list_accessible_businesses(user_id=user_id))
    if not accesses:
        return None, accesses
    if len(accesses) == 1:
        business_id = str(accesses[0].business.id)
    else:
        business_id = get_owner_control_workspace(user_id=user_id, platform="telegram")
        if business_id is None:
            return None, accesses
    try:
        return resolve_tenant_context(user_id=user_id, business_id=business_id), accesses
    except (ValueError, RuntimeError):
        return None, accesses


@router.message(Command("support"))
async def clientplatform_support_case_command(message: Message) -> None:
    """Create/list tenant support cases through the channel-neutral application owner."""

    support_cases = importlib.import_module("clientplatform.application.support_cases")
    user_id = control._user_id(message)
    actor, accesses = await asyncio.to_thread(_telegram_support_actor, user_id)
    if actor is None:
        if not accesses:
            await message.answer("Сначала подключите бизнес, затем создайте обращение в поддержку.")
        else:
            await message.answer(
                "У Вас несколько бизнесов. Сначала откройте нужный бизнес через /start, "
                "затем повторите /support."
            )
        return
    parts = str(getattr(message, "text", "") or "").strip().split(maxsplit=2)
    if len(parts) >= 2 and parts[1].casefold() == "list":
        cases = await asyncio.to_thread(support_cases.list_tenant_support_cases, actor=actor, limit=20)
        if not cases:
            await message.answer("У этого бизнеса пока нет обращений в поддержку.")
            return
        lines = [
            f"• {case.id} · {case.category.value} · {case.status.value} · {case.summary}"
            for case in cases
        ]
        await message.answer("Обращения в поддержку:\n\n" + "\n".join(lines))
        return
    if len(parts) != 3:
        await message.answer(
            "Формат: /support <category> <описание>\n"
            "Категории: general, billing, technical, security, integration\n"
            "Список: /support list"
        )
        return
    try:
        case = await asyncio.to_thread(
            support_cases.create_support_case,
            actor=actor,
            category=parts[1].casefold(),
            summary=parts[2],
            idempotency_key=_platform_support_idempotency_key(message),
        )
    except ValueError:
        await message.answer(
            "Не удалось создать обращение. Проверьте категорию и описание (3–1000 символов)."
        )
        return
    await message.answer(
        "Обращение создано.\n\n"
        f"Case: {case.id}\n"
        f"Категория: {case.category.value}\n"
        f"Статус: {case.status.value}"
    )


@router.message(Command("platformstatus"))
async def clientplatform_platform_status_command(message: Message) -> None:
    """Expose the read-only platform snapshot only to configured operators."""

    operator = importlib.import_module(
        "services.platform_operator_dashboard"
    )
    user_id = control._user_id(message)
    try:
        snapshot = await asyncio.to_thread(
            operator.platform_operator_snapshot,
            user_id,
        )
    except operator.PlatformOperatorPermissionDenied:
        await message.answer("Доступ к состоянию платформы недоступен.")
        return

    recovery = snapshot["disaster_recovery"]
    telemetry = snapshot["resource_telemetry"]
    release_report = snapshot["release_contract"]["report"]
    recovery_status = str(recovery.get("status", "UNKNOWN"))
    recovery_reason = str(recovery.get("reason", "unknown"))
    telemetry_status = str(telemetry.get("status", "UNKNOWN"))
    await message.answer(
        "ClientPlatform · состояние платформы\n\n"
        f"{release_report}\n"
        f"Disaster recovery: {recovery_status} — {recovery_reason}\n"
        f"Resource telemetry: {telemetry_status}"
    )


def _platform_support_command_parts(message: Message) -> list[str]:
    return str(getattr(message, "text", "") or "").strip().split(maxsplit=4)


def _platform_support_idempotency_key(message: Message) -> str:
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        raise ValueError("support command requires Telegram message identity")
    return f"telegram:{int(chat_id)}:{int(message_id)}"


def _platform_support_usage() -> str:
    return (
        "Использование:\n"
        "/supportsession open <business_id> <ticket> <причина>\n"
        "/supportsession read <session_id> <business_id>\n"
        "/supportsession revoke <session_id> <business_id>"
    )


@router.message(Command("supportsession"))
async def clientplatform_platform_support_session_command(message: Message) -> None:
    """Hidden platform-operator surface for one-business audited support access."""

    support = importlib.import_module("services.platform_support_access")
    user_id = control._user_id(message)
    parts = _platform_support_command_parts(message)
    if len(parts) < 2:
        await message.answer(_platform_support_usage())
        return

    action = parts[1].casefold()
    try:
        if action == "open":
            if len(parts) != 5:
                await message.answer(_platform_support_usage())
                return
            session = await asyncio.to_thread(
                support.issue_support_session,
                user_id,
                business_id=parts[2],
                ticket_ref=parts[3],
                reason=parts[4],
                idempotency_key=_platform_support_idempotency_key(message),
            )
            await message.answer(
                "ClientPlatform · support session создана\n\n"
                f"Session: {session.id}\n"
                f"Business: {session.business_id}\n"
                f"Истекает: {session.expires_at}\n"
                "Режим: read-only"
            )
            return
        if action == "read":
            if len(parts) != 4:
                await message.answer(_platform_support_usage())
                return
            snapshot = await asyncio.to_thread(
                support.read_support_business,
                user_id,
                session_id=parts[2],
                business_id=parts[3],
            )
            await message.answer(
                "ClientPlatform · support read\n\n"
                f"Business: {snapshot.business_name}\n"
                f"Business ID: {snapshot.business_id}\n"
                f"Статус: {snapshot.business_status}\n"
                f"Session: {snapshot.session_id}\n"
                f"Истекает: {snapshot.session_expires_at}\n"
                "Доступ: read-only"
            )
            return
        if action == "revoke":
            if len(parts) != 4:
                await message.answer(_platform_support_usage())
                return
            session = await asyncio.to_thread(
                support.revoke_support_session,
                user_id,
                session_id=parts[2],
                business_id=parts[3],
            )
            await message.answer(
                "ClientPlatform · support session отозвана\n\n"
                f"Session: {session.id}\n"
                f"Business: {session.business_id}\n"
                f"Отозвана: {session.revoked_at}"
            )
            return
        await message.answer(_platform_support_usage())
    except support.PlatformSupportPermissionDenied:
        await message.answer("Доступ к support session недоступен.")
    except support.PlatformSupportSessionUnavailable:
        await message.answer("Support session недоступна или больше не активна.")
    except (support.PlatformSupportSessionConflict, ValueError):
        await message.answer("Параметры support session некорректны или конфликтуют.")


def _platform_support_queue_usage() -> str:
    return (
        "Использование:\n"
        "/supportqueue list\n"
        "/supportqueue claim <case_id>\n"
        "/supportqueue release <case_id>\n"
        "/supportqueue resolve <case_id>\n"
        "/supportqueue session <case_id> <причина>"
    )


def _support_queue_summary_preview(value: object) -> str:
    summary = str(value or "")
    if len(summary) <= _SUPPORT_QUEUE_SUMMARY_PREVIEW:
        return summary
    return summary[: _SUPPORT_QUEUE_SUMMARY_PREVIEW - 1].rstrip() + "…"


def _platform_support_queue_chunks(cases: list[Any]) -> list[str]:
    header = "ClientPlatform · support queue"
    chunks: list[str] = []
    current = header
    for case in cases:
        line = (
            f"• {case.id} · business={case.business_id} · {case.category.value} · "
            f"{case.status.value} · {_support_queue_summary_preview(case.summary)}"
        )
        separator = "\n\n" if current.startswith(header) and "\n" not in current else "\n"
        candidate = current + separator + line
        if len(candidate) <= _TELEGRAM_SAFE_TEXT_LIMIT:
            current = candidate
            continue
        chunks.append(current)
        current = header + " · продолжение\n\n" + line
    chunks.append(current)
    return chunks


@router.message(Command("supportqueue"))
async def clientplatform_platform_support_queue_command(message: Message) -> None:
    """Hidden platform-operator queue; queue actions never grant tenant access."""

    support_cases = importlib.import_module("clientplatform.application.support_cases")
    repository = importlib.import_module("clientplatform.infrastructure.support_case_repository")
    user_id = control._user_id(message)
    parts = str(getattr(message, "text", "") or "").strip().split(maxsplit=3)
    if len(parts) < 2:
        await message.answer(_platform_support_queue_usage())
        return
    action = parts[1].casefold()
    operation_key = _platform_support_idempotency_key(message)
    try:
        if action == "list":
            cases = await asyncio.to_thread(support_cases.list_platform_support_queue, user_id, limit=50)
            if not cases:
                await message.answer("Открытых support cases нет.")
                return
            for chunk in _platform_support_queue_chunks(cases):
                await message.answer(chunk)
            return
        if len(parts) < 3:
            await message.answer(_platform_support_queue_usage())
            return
        case_id = parts[2]
        if action == "claim":
            case = await asyncio.to_thread(
                support_cases.claim_platform_support_case,
                user_id, case_id=case_id, idempotency_key=operation_key
            )
        elif action == "release":
            case = await asyncio.to_thread(
                support_cases.release_platform_support_case,
                user_id, case_id=case_id, idempotency_key=operation_key
            )
        elif action == "resolve":
            case = await asyncio.to_thread(
                support_cases.resolve_platform_support_case,
                user_id, case_id=case_id, idempotency_key=operation_key
            )
        elif action == "session":
            if len(parts) != 4:
                await message.answer(_platform_support_queue_usage())
                return
            session = await asyncio.to_thread(
                support_cases.issue_support_session_for_case,
                user_id,
                case_id=case_id,
                reason=parts[3],
                idempotency_key=operation_key,
            )
            await message.answer(
                "Support session создана отдельно от queue claim.\n\n"
                f"Session: {session.id}\n"
                f"Business: {session.business_id}\n"
                f"Истекает: {session.expires_at}"
            )
            return
        else:
            await message.answer(_platform_support_queue_usage())
            return
        await message.answer(
            f"Case: {case.id}\nBusiness: {case.business_id}\nСтатус: {case.status.value}"
        )
    except support_cases.PlatformSupportCasePermissionDenied:
        await message.answer("Доступ к support queue недоступен.")
    except repository.SupportCaseUnavailable:
        await message.answer("Support case недоступен или его состояние уже изменилось.")
    except repository.SupportCaseConflict:
        await message.answer("Support case уже изменён другим оператором или запрос конфликтует.")
    except (PermissionError, ValueError):
        await message.answer("Операция support queue недоступна с указанными параметрами.")


def _platform_directory_usage() -> str:
    return (
        "Использование:\n"
        "/platformdirectory business <business_id>\n"
        "/platformdirectory user <platform_user_id>\n"
        "/platformdirectory name <часть названия>"
    )


def _platform_directory_chunks(result: Any) -> list[str]:
    truncation = (
        "\n⚠️ Показаны первые 20 совпадений; есть дополнительные результаты."
        if result.truncated
        else ""
    )
    header = f"ClientPlatform · platform directory\nAudit: {result.audit_id}{truncation}"
    chunks: list[str] = []
    current = header
    for item in result.matches:
        matched = ""
        if item.matched_user_id is not None:
            role = "-" if item.matched_role is None else item.matched_role.value
            membership = item.matched_membership_status or "-"
            matched = f"\n  user={item.matched_user_id} · role={role} · membership={membership}"
        block = (
            f"• {item.business_name}\n"
            f"  business={item.business_id}\n"
            f"  status={item.business_status.value} · created={item.business_created_at}\n"
            f"  active_members={item.active_member_count} · active_owners={item.active_owner_count}"
            f"{matched}"
        )
        candidate = current + "\n\n" + block
        if len(candidate) <= _TELEGRAM_SAFE_TEXT_LIMIT:
            current = candidate
            continue
        chunks.append(current)
        current = "ClientPlatform · platform directory · продолжение\n\n" + block
    chunks.append(current)
    return chunks


@router.message(Command("platformdirectory"))
async def clientplatform_platform_directory_command(message: Message) -> None:
    """Hidden, query-bound platform operator directory; it never grants tenant access."""

    directory = importlib.import_module("clientplatform.application.platform_directory")
    user_id = control._user_id(message)
    parts = str(getattr(message, "text", "") or "").strip().split(maxsplit=2)
    if len(parts) != 3:
        await message.answer(_platform_directory_usage())
        return
    kind = {
        "business": "business_id",
        "user": "user_id",
        "name": "business_name",
    }.get(parts[1].casefold())
    if kind is None:
        await message.answer(_platform_directory_usage())
        return
    try:
        result = await asyncio.to_thread(
            directory.search_platform_directory,
            user_id,
            query_kind=kind,
            query=parts[2],
            limit=20,
        )
    except directory.PlatformDirectoryPermissionDenied:
        await message.answer("Доступ к platform directory недоступен.")
        return
    except ValueError:
        await message.answer("Параметры platform directory некорректны или слишком широки.")
        return
    if not result.matches:
        await message.answer(f"Совпадений нет.\nAudit: {result.audit_id}")
        return
    for chunk in _platform_directory_chunks(result):
        await message.answer(chunk)


def _account_merge_usage() -> str:
    return (
        "Использование:\n"
        "/accountmerge plan <source_account_id> <target_account_id>\n"
        "/accountmerge apply <source_account_id> <target_account_id> "
        "<plan_sha256> <confirmation_code> <operation_key> <причина>"
    )


def _account_merge_plan_chunks(plan: Any) -> list[str]:
    state = "READY" if plan.can_apply else "BLOCKED"
    expansion = [
        f"• business={item.business_id} · role={item.role} · status={item.status}"
        for item in plan.access_expansions
    ]
    blockers = [f"• {item}" for item in plan.blockers]
    dependencies = [
        f"• {item.table}.{item.column} · {item.policy} · source={item.source_rows} · target={item.target_rows}"
        for item in plan.dependencies
        if item.source_rows or item.target_rows
    ]
    lines = [
        f"ClientPlatform · account consolidation · {state}",
        f"Source: account={plan.source_account_id} · user={plan.source_user_id} · channels={','.join(plan.source_platforms) or '-'}",
        f"Target: account={plan.target_account_id} · user={plan.target_user_id} · channels={','.join(plan.target_platforms) or '-'}",
        f"Plan SHA-256: {plan.plan_fingerprint}",
    ]
    if plan.can_apply:
        lines.append(f"Confirmation: {plan.confirmation_code}")
    if expansion:
        lines.extend(["", "Tenant access expansion (явно):", *expansion])
    if blockers:
        lines.extend(["", "Blockers:", *blockers])
    if dependencies:
        lines.extend(["", "Durable identity dependencies:", *dependencies])

    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= _TELEGRAM_SAFE_TEXT_LIMIT:
            current = candidate
            continue
        chunks.append(current)
        current = "ClientPlatform · account consolidation · продолжение\n" + line
    if current:
        chunks.append(current)
    return chunks


@router.message(Command("accountmerge"))
async def clientplatform_account_merge_command(message: Message) -> None:
    """Hidden high-trust duplicate-account dry-run/apply surface."""

    account_merge = importlib.import_module("services.accounts.consolidation")
    user_id = control._user_id(message)
    raw = str(getattr(message, "text", "") or "").strip()
    parts = raw.split(maxsplit=7)
    if len(parts) < 2:
        await message.answer(_account_merge_usage())
        return
    action = parts[1].casefold()
    try:
        if action == "plan":
            if len(parts) != 4:
                await message.answer(_account_merge_usage())
                return
            plan = await asyncio.to_thread(
                account_merge.plan_account_consolidation,
                user_id,
                source_account_id=int(parts[2]),
                target_account_id=int(parts[3]),
            )
            for chunk in _account_merge_plan_chunks(plan):
                await message.answer(chunk)
            return
        if action == "apply":
            if len(parts) != 8:
                await message.answer(_account_merge_usage())
                return
            result = await asyncio.to_thread(
                account_merge.apply_account_consolidation,
                user_id,
                source_account_id=int(parts[2]),
                target_account_id=int(parts[3]),
                expected_plan_fingerprint=parts[4],
                confirmation_code=parts[5],
                idempotency_key=parts[6],
                reason=parts[7],
            )
            replay = " · idempotent replay" if result.idempotent_replay else ""
            await message.answer(
                "Account consolidation применён.\n"
                f"Operation: {result.operation_id}{replay}\n"
                f"Source → Target: {result.source_account_id} → {result.target_account_id}\n"
                f"Plan: {result.plan_fingerprint}"
            )
            return
        await message.answer(_account_merge_usage())
    except account_merge.AccountConsolidationPermissionDenied:
        await message.answer("Доступ к account consolidation недоступен.")
    except account_merge.AccountConsolidationStalePlan:
        await message.answer("Dry-run устарел: состояние изменилось. Выполните /accountmerge plan заново.")
    except account_merge.AccountConsolidationConflict as exc:
        await message.answer(f"Account consolidation заблокирован: {exc}")
    except (account_merge.AccountConsolidationUnavailable, ValueError):
        await message.answer("Account consolidation недоступен с указанными параметрами.")


@router.message(Command("cancel"))
async def clientplatform_cancel_command(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    await state.clear()
    if current_state:
        await message.answer("Текущий шаг отменён. Нажмите /start, чтобы открыть кабинет.")
        return
    await message.answer("Сейчас нет незавершённого шага. Нажмите /start.")


@router.message(F.text.startswith("/"))
async def clientplatform_unknown_command(message: Message, state: FSMContext) -> None:
    """Never allow a Telegram command to become a business/user field value."""

    await state.clear()
    await message.answer(
        "Команда не была сохранена как данные. "
        "Доступны /start, /admin, /mybot и /cancel."
    )


@router.callback_query(F.data == "cp:entry:businesses")
async def open_business_workspace(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = int(callback.from_user.id)
    accesses = await asyncio.to_thread(list_accessible_businesses, user_id=user_id)
    if not accesses:
        await control._callback_message(callback).answer(
            "Активных бизнесов больше нет. Нажмите /start, чтобы обновить меню."
        )
        return
    await _send_business_choice(
        control._callback_message(callback),
        user_id=user_id,
        accesses=accesses,
        state=state,
    )


@router.callback_query(F.data == "cp:entry:clients")
async def open_customer_workspace(callback: CallbackQuery, state: FSMContext) -> None:
    links = await asyncio.to_thread(
        list_customer_businesses,
        telegram_user_id=int(callback.from_user.id),
    )
    if not links:
        await control._callback_message(callback).answer(
            "Активных подключений к специалистам больше нет. Нажмите /start, чтобы обновить меню."
        )
        return
    await state.clear()
    await control._send_client_portal(control._callback_message(callback), links=links)


@router.errors()
async def clientplatform_entry_error(event: object) -> bool:
    if await control.clientplatform_control_error(event):
        return True

    exception = getattr(event, "exception", None)
    update = getattr(event, "update", None)
    if not isinstance(exception, Exception):
        return False

    log.error(
        "Unhandled ClientPlatform interaction failure",
        exc_info=(type(exception), exception, exception.__traceback__),
    )
    message = getattr(update, "message", None)
    callback = getattr(update, "callback_query", None)
    try:
        if isinstance(message, Message):
            await message.answer(
                "Не удалось продолжить настройку ClientPlatform. "
                "Отправьте /start — сохранённые данные не потеряны."
            )
            return True
        if isinstance(callback, CallbackQuery):
            await callback.answer(
                "Не удалось выполнить действие. Откройте ClientPlatform через /start.",
                show_alert=True,
            )
            return True
    except TelegramAPIError:
        log.warning("Failed to report ClientPlatform interaction failure", exc_info=True)
    return False


if not bool(getattr(control, "_dual_role_entry_composed", False)):
    original_router = control.router
    interaction_safety = importlib.import_module(
        ".clientplatform_interaction_safety",
        __package__,
    )
    interaction_safety.install_interaction_safety(router, control)
    admin = importlib.import_module(
        ".clientplatform_admin",
        __package__,
    )
    admin.install_admin_dashboard_button(control)
    admin_callback_guard = importlib.import_module(
        ".clientplatform_admin_callback_guard",
        __package__,
    )
    admin_callback_guard.install_admin_callback_namespace_guard(admin, control)
    dashboard_dispatch = importlib.import_module(
        ".clientplatform_dashboard_dispatch",
        __package__,
    )
    dashboard_dispatch.install_dynamic_dashboard_dispatch(control)
    onboarding_recovery = importlib.import_module(
        ".clientplatform_onboarding_recovery",
        __package__,
    )
    program_media = importlib.import_module(
        ".clientplatform_program_media_router",
        __package__,
    )
    program_builder = importlib.import_module(
        ".clientplatform_program_builder",
        __package__,
    )
    simple_experience = importlib.import_module(
        ".clientplatform_simple_experience",
        __package__,
    )
    booking_wizard_ux = importlib.import_module(
        ".clientplatform_booking_wizard_ux",
        __package__,
    )
    cloud_media = importlib.import_module(
        ".clientplatform_cloud_media",
        __package__,
    )
    lesson_editor = importlib.import_module(
        ".clientplatform_program_lesson_editor_composition",
        __package__,
    )
    privacy = importlib.import_module(
        ".clientplatform_privacy",
        __package__,
    )
    router.include_router(admin.router)
    router.include_router(privacy.router)
    router.include_router(interaction_safety.router)
    router.include_router(onboarding_recovery.router)
    # Booking wizard UX must precede the legacy/simple router because it owns
    # the same booking_start FSM state and intentionally replaces only that
    # prompt with one-click duration choices.
    router.include_router(booking_wizard_ux.router)
    router.include_router(simple_experience.router)
    router.include_router(cloud_media.router)
    router.include_router(program_media.router)
    router.include_router(lesson_editor.router)
    router.include_router(program_builder.router)
    router.include_router(original_router)
    control.router = router
    control._admin_router_composed = True
    control._interaction_safety_router_composed = True
    control._onboarding_recovery_router_composed = True
    control._booking_wizard_ux_router_composed = True
    control._simple_experience_router_composed = True
    control._cloud_media_router_composed = True
    control._program_media_router_composed = True
    control._program_lesson_editor_composed = True
    control._multi_lesson_program_builder_composed = True
    control._dual_role_entry_composed = True
