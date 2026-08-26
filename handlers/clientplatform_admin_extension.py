from __future__ import annotations

import asyncio
import contextvars
import importlib
import logging
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from types import ModuleType
from typing import Any, Awaitable, Callable

from aiogram import F, BaseMiddleware, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from clientplatform.application import admin_ops
from clientplatform.application.activity import list_business_offerings
from clientplatform.application.customers import list_customers
from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.tenancy import TenantPermissionDenied, TenancyError
from clientplatform.runtime import admin_observability
from core.telegram_multi_egress import (
    install_multi_egress_bot,
    telegram_egress_snapshot,
)


log = logging.getLogger(__name__)
router = Router(name="clientplatform_admin_extension")


class ClientPlatformAdminOpsState(StatesGroup):
    publication_title = State()
    publication_body = State()
    payment_value = State()
    price_value = State()


@dataclass(slots=True)
class _InteractionTrace:
    started: float
    ack_ms: int = 0
    ack_finished: float | None = None
    lock_wait_ms: int = 0
    handler_started: float | None = None
    telegram_ms: int = 0


_TRACE: contextvars.ContextVar[_InteractionTrace | None] = contextvars.ContextVar(
    "clientplatform_admin_interaction_trace",
    default=None,
)
_INSTALLED = False

_EMPTY_INSIGHTS = admin_ops.AdminInsightSnapshot(
    active_customers=0,
    active_offerings=0,
    active_invites=0,
    claimed_invites=0,
    enrollments=0,
    completed_enrollments=0,
    publication_drafts=0,
    publications_published=0,
    paid_payments=0,
    paid_amount_minor=0,
    payment_currency="RUB",
    priced_offerings=0,
    active_staff=0,
)
_EMPTY_INTERACTION = admin_ops.InteractionSnapshot(
    count=0,
    successes=0,
    failures=0,
    p50_ms=0,
    p95_ms=0,
    max_ms=0,
    ack_p95_ms=0,
    lock_p95_ms=0,
    telegram_p95_ms=0,
    window_minutes=60,
)


async def _optional_thread(
    function: Callable[..., Any],
    *,
    default: Any,
    forward_default: bool = False,
    **kwargs: Any,
) -> Any:
    if forward_default:
        kwargs["default"] = default
    try:
        return await asyncio.to_thread(function, **kwargs)
    except TenancyError:
        return default


def _payment_totals(payments: list[Any]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for payment in payments:
        if payment.status != "paid":
            continue
        totals[payment.currency] = (
            totals.get(payment.currency, 0) + int(payment.amount_minor)
        )
    return totals


def _payment_totals_text(payments: list[Any]) -> str:
    totals = _payment_totals(payments)
    if not totals:
        return "0,00 RUB"
    return " · ".join(
        _money(amount, currency)
        for currency, amount in sorted(totals.items())
    )


def _payment_average_text(payments: list[Any]) -> str:
    totals = _payment_totals(payments)
    counts: dict[str, int] = {}
    for payment in payments:
        if payment.status == "paid":
            counts[payment.currency] = counts.get(payment.currency, 0) + 1
    if not totals:
        return "0,00 RUB"
    return " · ".join(
        _money(total // max(1, counts[currency]), currency)
        for currency, total in sorted(totals.items())
    )


def _ops_callback(ctx: Any, action: str, *payload: object) -> str:
    tail = ":".join(str(item) for item in payload)
    value = f"cpao:{ctx.business_token}:{action}"
    if tail:
        value += f":{tail}"
    if len(value.encode("utf-8")) > 64:
        raise ValueError("ClientPlatform admin operation callback exceeds Telegram limit")
    return value


def _flow_keyboard(
    admin: ModuleType,
    ctx: Any,
    *,
    return_action: str,
    extra: list[tuple[str, str]] | None = None,
) -> InlineKeyboardMarkup:
    rows = [[item] for item in list(extra or [])]
    rows.append([("⬅️ Назад", _ops_callback(ctx, return_action))])
    return admin._keyboard(rows)


def _money(amount_minor: int, currency: str) -> str:
    amount = Decimal(int(amount_minor)) / Decimal(100)
    rendered = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
    return f"{rendered} {currency}"


def _percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0%"
    return f"{round(numerator * 100 / denominator)}%"


def _status_icon(value: bool) -> str:
    return "✅" if value else "⚠️"


def _can_write_finance(ctx: Any) -> bool:
    """Use the application layer's canonical finance-write role set."""

    return getattr(ctx.actor, "role", None) in admin_ops._FINANCE_WRITE_ROLES


def _finance_write_buttons(
    admin: ModuleType,
    ctx: Any,
    *,
    action: str,
    offerings: list[Any],
) -> list[tuple[str, str]]:
    if not _can_write_finance(ctx):
        return []
    if action in {"money", "payments"}:
        return [
            ("➕ Зафиксировать оплату вручную", _ops_callback(ctx, "payment-new"))
        ]
    if action == "prices":
        return [
            (
                f"💵 Цена · {item.title[:22]}",
                _ops_callback(
                    ctx,
                    "price-set",
                    admin.control._uuid_token(item.id),
                ),
            )
            for item in offerings[:10]
        ]
    return []


def _telegram_payment_idempotency_key(message: Message) -> str:
    user_id = int(message.from_user.id)
    chat_id = int(getattr(getattr(message, "chat", None), "id", user_id))
    message_id = int(getattr(message, "message_id", 0) or 0)
    return f"telegram-payment:{chat_id}:{message_id}"


async def _all_offerings(actor: Any, capabilities: list[Any]) -> list[Any]:
    active_ids = [
        item.id
        for item in capabilities
        if item.status == CapabilityStatus.ACTIVE
        and getattr(item, "id", None) is not None
    ]
    groups = await asyncio.gather(
        *[
            _optional_thread(
                list_business_offerings,
                default=[],
                actor=actor,
                capability_id=capability_id,
            )
            for capability_id in active_ids
        ]
    )
    return [offering for group in groups for offering in group]


async def _enhanced_attention(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: Any,
) -> None:
    admin = importlib.import_module(".clientplatform_admin", __package__)
    summary, alerts = await asyncio.gather(
        asyncio.to_thread(admin.business_delivery_summary, actor=ctx.actor),
        _optional_thread(
            admin_ops.list_open_alerts,
            default=[],
            actor=ctx.actor,
        ),
    )
    lines = [
        "⚠️ Требуют внимания",
        "",
        f"Ошибки отправки: {summary.dispatch_attention}",
        f"Ожидают отправки: {summary.dispatch_pending}",
        f"Открытые системные предупреждения: {len(alerts)}",
    ]
    if alerts:
        lines.append("")
        lines.extend(
            f"{'🔴' if item.severity == 'critical' else '🟠'} {item.message}"
            for item in alerts[:8]
        )
    elif not summary.dispatch_attention and not summary.dispatch_pending:
        lines.extend(["", "Сейчас критических задач нет."])
    await admin._safe_edit(
        callback,
        "\n".join(lines),
        admin._back_keyboard(
            ctx,
            ("🔄 Перепроверить", _ops_callback(ctx, "alerts-refresh")),
        ),
    )
    await admin._set_current_section(state, action="attention", push=True)


async def _enhanced_marketing(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: Any,
    action: str,
) -> None:
    admin = importlib.import_module(".clientplatform_admin", __package__)
    (
        base_snapshot,
        insights,
        payments,
        publications,
        prices,
        interaction,
        autopilot_value,
    ) = await asyncio.gather(
        admin._base_snapshot(ctx),
        _optional_thread(
            admin_ops.business_admin_insights,
            default=_EMPTY_INSIGHTS,
            actor=ctx.actor,
        ),
        _optional_thread(
            admin_ops.list_payments,
            default=[],
            actor=ctx.actor,
            limit=20,
        ),
        _optional_thread(
            admin_ops.list_publications,
            default=[],
            actor=ctx.actor,
            limit=20,
        ),
        _optional_thread(
            admin_ops.list_offering_prices,
            default=[],
            actor=ctx.actor,
        ),
        _optional_thread(
            admin_ops.interaction_snapshot,
            default=_EMPTY_INTERACTION,
            actor=ctx.actor,
            window_minutes=60,
        ),
        _optional_thread(
            admin_ops.get_admin_setting,
            default="false",
            forward_default=True,
            actor=ctx.actor,
            key="autopilot_enabled",
        ),
    )
    profile, summary, capabilities, slots, customers, programs, progress = base_snapshot
    offerings = await _all_offerings(ctx.actor, capabilities)
    price_by_offering = {item.offering_id: item for item in prices}
    paid_customer_ids = {
        item.customer_id
        for item in payments
        if item.status == "paid" and item.customer_id is not None
    }
    enrolled_ids = {item.customer_id for item in progress}
    completed_ids = {
        item.customer_id
        for item in progress
        if item.total_lessons > 0
        and item.completed_lessons >= item.total_lessons
    }
    stalled_ids = {
        item.customer_id
        for item in progress
        if item.total_lessons > item.completed_lessons
    }
    open_slots = sum(item.slot.status.value == "open" for item in slots)
    published = [item for item in publications if item.status == "published"]
    drafts = [item for item in publications if item.status == "draft"]
    enabled = autopilot_value.strip().lower() in {"1", "true", "yes", "on"}

    if action == "autopilot":
        text = (
            "🤖 Growth Autopilot\n\n"
            f"Статус: {'включён' if enabled else 'выключен'}\n"
            f"Клиентов без программы: "
            f"{max(0, insights.active_customers - len(enrolled_ids))}\n"
            f"Остановились до завершения: {len(stalled_ids)}\n"
            f"Ожидают отправки: {summary.dispatch_pending}\n"
            f"Ошибки отправки: {summary.dispatch_attention}\n\n"
            "Автопилот использует только существующие программы, "
            "подтверждённые клиентские связи и разрешённые отправки."
        )
        extra = [
            (
                "⏸ Выключить" if enabled else "▶️ Включить",
                _ops_callback(ctx, "autopilot-toggle"),
            )
        ]
    elif action == "publications":
        recent = "\n".join(
            f"• {'✅' if item.status == 'published' else '📝'} "
            f"{item.title[:45]} · {item.status}"
            for item in publications[:8]
        ) or "• Публикаций пока нет"
        text = (
            "📣 Публикации\n\n"
            f"Черновики: {len(drafts)}\n"
            f"Опубликовано: {len(published)}\n\n"
            f"{recent}"
        )
        extra = [("➕ Создать черновик", _ops_callback(ctx, "publication-new"))]
        extra.extend(
            (
                f"✅ Отметить опубликованной · {item.title[:18]}",
                _ops_callback(
                    ctx,
                    "publication-publish",
                    admin.control._uuid_token(item.id),
                ),
            )
            for item in drafts[:5]
        )
    elif action == "funnel":
        text = (
            "📉 Путь до заявки\n\n"
            f"Создано приглашений: "
            f"{insights.active_invites + insights.claimed_invites}\n"
            f"Принято приглашений: {insights.claimed_invites} "
            f"({_percent(insights.claimed_invites, insights.active_invites + insights.claimed_invites)})\n"
            f"Клиентов: {insights.active_customers}\n"
            f"В программах: {insights.enrollments} "
            f"({_percent(insights.enrollments, insights.active_customers)})\n"
            f"Завершили: {insights.completed_enrollments} "
            f"({_percent(insights.completed_enrollments, insights.enrollments)})\n"
            f"Оплат: {insights.paid_payments}\n"
            f"Свободных времён: {open_slots}"
        )
        extra = []
    elif action == "money":
        text = (
            "💰 Деньги и клиенты\n\n"
            f"Оплачено: {_payment_totals_text(payments)}\n"
            f"Успешных оплат: {insights.paid_payments}\n"
            f"Средний платёж: {_payment_average_text(payments)}\n"
            f"Платящих клиентов: {len(paid_customer_ids)}\n"
            f"Всего клиентов: {insights.active_customers}\n"
            f"Доля платящих: "
            f"{_percent(len(paid_customer_ids), insights.active_customers)}"
        )
        extra = _finance_write_buttons(admin, ctx, action=action, offerings=offerings)
    elif action == "payments":
        recent = "\n".join(
            f"• {_money(item.amount_minor, item.currency)} · "
            f"{item.note[:35] or item.provider} · {item.status}"
            for item in payments[:10]
        ) or "• Оплат пока нет"
        text = (
            "💰 Оплаты\n\n"
            f"Успешных: {insights.paid_payments}\n"
            f"Сумма: {_payment_totals_text(payments)}\n\n"
            f"{recent}"
        )
        extra = _finance_write_buttons(admin, ctx, action=action, offerings=offerings)
        if _can_write_finance(ctx):
            extra.extend(
                (
                    f"↩️ Возврат · {_money(item.amount_minor, item.currency)}",
                    _ops_callback(
                        ctx,
                        "pay-refund",
                        admin.control._uuid_token(item.id),
                    ),
                )
                for item in payments[:10]
                if item.status == "paid"
                and getattr(item, "outcome_event_id", None) is not None
            )
    elif action == "segments":
        without_program = max(0, insights.active_customers - len(enrolled_ids))
        text = (
            "🧲 Группы клиентов\n\n"
            f"Новые / без программы: {without_program}\n"
            f"Проходят программу: {len(enrolled_ids - completed_ids)}\n"
            f"Завершили: {len(completed_ids)}\n"
            f"Остановились: {len(stalled_ids)}\n"
            f"Платящие: {len(paid_customer_ids)}\n"
            f"Без зафиксированной оплаты: "
            f"{max(0, insights.active_customers - len(paid_customer_ids))}"
        )
        extra = []
    elif action == "offers":
        priced = len(price_by_offering)
        offer_lines = "\n".join(
            f"• {item.title[:40]} — "
            f"{_money(price_by_offering[item.id].amount_minor, price_by_offering[item.id].currency) if item.id in price_by_offering else 'цена не задана'}"
            for item in offerings[:12]
        ) or "• Предложения ещё не созданы"
        text = (
            "🧪 Проверка предложений\n\n"
            f"Активных предложений: {len(offerings)}\n"
            f"С ценой: {priced}\n"
            f"Без цены: {max(0, len(offerings) - priced)}\n"
            f"Клиентов: {insights.active_customers}\n"
            f"Оплат: {insights.paid_payments}\n\n"
            f"{offer_lines}"
        )
        extra = [("💡 Настроить цены", admin._callback(ctx, "prices"))]
    elif action == "copy":
        text = (
            "✍️ Подготовить тексты\n\n"
            f"Основа бренда:\n{profile.activity_description}\n\n"
            "Готовая структура:\n"
            f"1. Кому помогает «{ctx.business_name}».\n"
            "2. Какой конкретный результат получает клиент.\n"
            "3. Как проходит работа или программа.\n"
            "4. Один понятный следующий шаг."
        )
        extra = [
            ("✏️ Изменить деятельность", f"cp:editact:{ctx.business_token}"),
            ("➕ Создать публикацию", _ops_callback(ctx, "publication-new")),
        ]
    elif action == "prices":
        lines = "\n".join(
            f"• {item.title[:36]} — "
            f"{_money(price_by_offering[item.id].amount_minor, price_by_offering[item.id].currency) if item.id in price_by_offering else 'не задана'}"
            for item in offerings[:12]
        ) or "• Сначала создайте предложение"
        text = (
            "💡 Подсказка по ценам\n\n"
            f"Предложений: {len(offerings)}\n"
            f"Цены заполнены: {len(price_by_offering)}/{len(offerings)}\n"
            f"Зафиксированная выручка: {_payment_totals_text(payments)}\n"
            f"Средний платёж: {_payment_average_text(payments)}\n\n"
            f"{lines}"
        )
        extra = _finance_write_buttons(admin, ctx, action=action, offerings=offerings)
    else:
        raise ValueError("unknown enhanced marketing section")

    if interaction.count:
        text += (
            f"\n\nСкорость интерфейса за час: "
            f"p95 {interaction.p95_ms} мс, ошибок {interaction.failures}."
        )
    await admin._safe_edit(
        callback,
        text,
        admin._back_keyboard(ctx, *extra),
    )
    await admin._set_current_section(state, action=action, push=True)


async def _enhanced_admin_report(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: Any,
    action: str,
) -> None:
    admin = importlib.import_module(".clientplatform_admin", __package__)
    (
        base_snapshot,
        insights,
        payments,
        interaction,
        audit,
    ) = await asyncio.gather(
        admin._base_snapshot(ctx),
        _optional_thread(
            admin_ops.business_admin_insights,
            default=_EMPTY_INSIGHTS,
            actor=ctx.actor,
        ),
        _optional_thread(
            admin_ops.list_payments,
            default=[],
            actor=ctx.actor,
            limit=20,
        ),
        _optional_thread(
            admin_ops.interaction_snapshot,
            default=_EMPTY_INTERACTION,
            actor=ctx.actor,
            window_minutes=60,
        ),
        _optional_thread(
            admin_ops.recent_audit_events,
            default=[],
            actor=ctx.actor,
            limit=12,
        ),
    )
    profile, summary, capabilities, slots, customers, programs, progress = base_snapshot
    route = telegram_egress_snapshot()
    alerts = await _optional_thread(
        admin_ops.refresh_interaction_alerts,
        default=[],
        actor=ctx.actor,
        route_redundant=route.egress_redundant,
    )
    active_capabilities = sum(
        item.status == CapabilityStatus.ACTIVE for item in capabilities
    )
    open_slots = sum(item.slot.status.value == "open" for item in slots)
    enrolled = len({item.customer_id for item in progress})
    complete = sum(
        item.total_lessons > 0 and item.completed_lessons >= item.total_lessons
        for item in progress
    )
    stalled = sum(item.completed_lessons < item.total_lessons for item in progress)
    release_ok = (
        profile.status.value == "ready"
        and active_capabilities > 0
        and summary.dispatch_attention == 0
        and route.polling_ready
        and interaction.p95_ms <= 1000
        and not any(item.severity == "critical" for item in alerts)
    )

    if action == "release":
        text = (
            "🚦 Release gate\n\n"
            f"{_status_icon(profile.status.value == 'ready')} Профиль бизнеса\n"
            f"{_status_icon(active_capabilities > 0)} Форматы работы\n"
            f"{_status_icon(summary.dispatch_attention == 0)} Доставка материалов\n"
            f"{_status_icon(route.polling_ready)} Telegram polling\n"
            f"{_status_icon(route.egress_redundant)} Резервный сетевой путь\n"
            f"{_status_icon(interaction.p95_ms <= 1000)} p95 кнопок: {interaction.p95_ms} мс\n"
            f"{_status_icon(not alerts)} Открытые предупреждения: {len(alerts)}\n\n"
            f"Итог: {'ГОТОВО' if release_ok else 'ТРЕБУЕТ ВНИМАНИЯ'}"
        )
        extra = [("🔄 Перепроверить", _ops_callback(ctx, "alerts-refresh"))]
    elif action == "invites":
        text = (
            "🎁 Приглашения и рекомендации\n\n"
            f"Активных ссылок: {insights.active_invites}\n"
            f"Принято ссылок: {insights.claimed_invites}\n"
            f"Конверсия в подключение: "
            f"{_percent(insights.claimed_invites, insights.active_invites + insights.claimed_invites)}\n"
            f"Клиентов всего: {insights.active_customers}"
        )
        extra = [("➕ Подключить клиента", f"cp:invite:{ctx.business_token}")]
    elif action == "funnel2":
        text = (
            "🧲 Воронка 2.0\n\n"
            f"Приглашения: {insights.active_invites + insights.claimed_invites}\n"
            f"Подключения: {insights.claimed_invites}\n"
            f"Клиенты: {insights.active_customers}\n"
            f"В программах: {enrolled}\n"
            f"Завершили: {complete}\n"
            f"Оплатили: {insights.paid_payments}\n"
            f"Оплачено: {_payment_totals_text(payments)}\n"
            f"Свободных времён: {open_slots}"
        )
        extra = []
    elif action == "retention":
        text = (
            "🧩 Удержание\n\n"
            f"Клиентов: {insights.active_customers}\n"
            f"В программах: {enrolled}\n"
            f"Завершили: {complete}\n"
            f"Незавершённых прохождений: {stalled}\n"
            f"Без программы: {max(0, insights.active_customers - enrolled)}\n\n"
            "Приоритет возврата: незавершённые программы, затем клиенты без программы."
        )
        extra = []
    elif action == "recent":
        lines = "\n".join(
            f"• {item.action} · {item.subject_type} · {item.created_at}"
            + (f" · {item.detail[:60]}" if item.detail else "")
            for item in audit
        ) or "• Управляющих действий пока нет"
        text = "🧾 Последние действия\n\n" + lines
        extra = []
    elif action == "system":
        alert_lines = "\n".join(
            f"• {'🔴' if item.severity == 'critical' else '🟠'} {item.message}"
            for item in alerts[:8]
        ) or "• предупреждений нет"
        text = (
            "🧪 Системные проверки\n\n"
            f"Tenant-доступ: ✅\n"
            f"Профиль бизнеса: {_status_icon(profile.status.value == 'ready')}\n"
            f"Telegram polling: {_status_icon(route.polling_ready)}"
            f" ({'активный long poll' if route.polling_in_flight else 'последний ответ'})\n"
            f"UI-маршрут: {route.ui_mode} / {route.ui_route}\n"
            f"Polling-маршрут: {route.polling_mode} / {route.polling_route}\n"
            f"Резервный egress: {_status_icon(route.egress_redundant)}\n"
            f"p50 / p95 / max: {interaction.p50_ms} / "
            f"{interaction.p95_ms} / {interaction.max_ms} мс\n"
            f"Ack p95: {interaction.ack_p95_ms} мс\n"
            f"Lock p95: {interaction.lock_p95_ms} мс\n"
            f"Telegram p95: {interaction.telegram_p95_ms} мс\n"
            f"Ошибок: {interaction.failures}/{interaction.count}\n\n"
            f"{alert_lines}"
        )
        extra = [("🔄 Перепроверить", _ops_callback(ctx, "alerts-refresh"))]
    else:
        raise ValueError("unknown enhanced admin report")

    await admin._safe_edit(
        callback,
        text,
        admin._back_keyboard(ctx, *extra),
    )
    await admin._set_current_section(state, action=action, push=True)


async def _enhanced_tariff(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: Any,
) -> None:
    admin = importlib.import_module(".clientplatform_admin", __package__)
    subscription, insights = await asyncio.gather(
        _optional_thread(
            admin_ops.get_subscription_state,
            default=admin_ops.SubscriptionState(
                business_id=ctx.business_id,
                plan_key="not_configured",
                status="inactive",
                included_staff=0,
                included_customers=0,
                started_at="не активирован",
                renews_at=None,
                updated_at="не активирован",
            ),
            actor=ctx.actor,
        ),
        _optional_thread(
            admin_ops.business_admin_insights,
            default=_EMPTY_INSIGHTS,
            actor=ctx.actor,
        ),
    )
    text = (
        "💳 Тариф ClientPlatform\n\n"
        f"План: {subscription.plan_key}\n"
        f"Статус: {subscription.status}\n"
        f"Сотрудники: {insights.active_staff}/{subscription.included_staff}\n"
        f"Клиенты: {insights.active_customers}/{subscription.included_customers}\n"
        f"Активирован: {subscription.started_at}\n"
        f"Следующее обновление: {subscription.renews_at or 'не назначено'}"
    )
    await admin._safe_edit(callback, text, admin._back_keyboard(ctx))
    await admin._set_current_section(state, action="tariff", push=True)


def _parse_amount(value: str) -> tuple[int, str, str]:
    parts = str(value or "").strip().split(maxsplit=2)
    if not parts:
        raise ValueError("Укажите сумму")
    raw_amount = parts[0].replace(" ", "").replace(",", ".")
    try:
        amount = Decimal(raw_amount)
    except InvalidOperation as exc:
        raise ValueError("Сумма должна быть числом") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    amount_minor = int(
        (amount * Decimal(100)).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )
    currency = "RUB"
    note = ""
    if len(parts) >= 2 and len(parts[1]) == 3 and parts[1].isalpha():
        currency = parts[1].upper()
        note = parts[2] if len(parts) == 3 else ""
    elif len(parts) >= 2:
        note = " ".join(parts[1:])
    return amount_minor, currency, note


async def _context_from_state(message: Message, state: FSMContext) -> Any:
    admin = importlib.import_module(".clientplatform_admin", __package__)
    data = await state.get_data()
    return await admin._load_admin_context(
        user_id=admin.control._user_id(message),
        business_id=str(data["cpao_business_id"]),
    )


async def _open_payment_value_step(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    admin: ModuleType,
    ctx: Any,
    customer_id: str | None,
    offering_id: str | None,
) -> None:
    await state.set_state(ClientPlatformAdminOpsState.payment_value)
    await state.update_data(
        cpao_business_id=ctx.business_id,
        cpao_payment_customer_id=customer_id,
        cpao_payment_offering_id=offering_id,
    )
    await admin._safe_edit(
        callback,
        "💰 Зафиксировать оплату вручную\n\n"
        "Отправьте: сумма, валюта и комментарий.\n"
        "Например: 3500 RUB консультация.\n"
        "Для отмены отправьте /cancel.",
        _flow_keyboard(
            admin,
            ctx,
            return_action="return-payments",
        ),
    )


@router.callback_query(F.data.startswith("cpao:"))
async def admin_ops_gate(callback: CallbackQuery, state: FSMContext) -> None:
    admin = importlib.import_module(".clientplatform_admin", __package__)
    parts = str(callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    business_id = admin.control._token_uuid(parts[1])
    action = parts[2]
    payload = tuple(parts[3:])
    ctx = await admin._load_admin_context(
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )

    if action in {
        "payment-new",
        "payment-customer",
        "pay-customer",
        "pay-offer",
        "pay-refund",
        "pay-refund-ok",
        "price-set",
    } and not _can_write_finance(ctx):
        await callback.answer(
            "Для вашей роли финансовые данные доступны только для просмотра.",
            show_alert=True,
        )
        return
    if action == "autopilot-toggle":
        if ctx.role not in admin._AUTOMATION_ROLES:
            raise TenantPermissionDenied(
                "automation controls are not allowed for this business role"
            )
        await asyncio.to_thread(admin_ops.toggle_autopilot, actor=ctx.actor)
        await asyncio.to_thread(
            admin_ops.refresh_interaction_alerts,
            actor=ctx.actor,
            route_redundant=telegram_egress_snapshot().egress_redundant,
        )
        await _enhanced_marketing(callback, state, ctx, "autopilot")
        return
    if action == "alerts-refresh":
        await asyncio.to_thread(
            admin_ops.refresh_interaction_alerts,
            actor=ctx.actor,
            route_redundant=telegram_egress_snapshot().egress_redundant,
        )
        if ctx.role in admin._ADMIN_ROLES:
            await _enhanced_admin_report(callback, state, ctx, "system")
        else:
            await _enhanced_attention(callback, state, ctx)
        return
    if action == "publication-new":
        await state.clear()
        await state.update_data(cpao_business_id=ctx.business_id)
        await admin._safe_edit(
            callback,
            "📣 Новый черновик\n\nВыберите канал публикации:",
            _flow_keyboard(
                admin,
                ctx,
                return_action="return-publications",
                extra=[
                    ("Telegram", _ops_callback(ctx, "publication-channel", "telegram")),
                    ("VK", _ops_callback(ctx, "publication-channel", "vk")),
                    ("MAX", _ops_callback(ctx, "publication-channel", "max")),
                    ("Другой канал", _ops_callback(ctx, "publication-channel", "other")),
                ],
            ),
        )
        return
    if action == "publication-channel":
        channel = payload[0] if payload else ""
        if channel not in {"telegram", "vk", "max", "other"}:
            raise ValueError("unsupported publication channel")
        await state.set_state(ClientPlatformAdminOpsState.publication_title)
        await state.update_data(
            cpao_business_id=ctx.business_id,
            cpao_publication_channel=channel,
        )
        await admin._safe_edit(
            callback,
            "📣 Новый черновик\n\nНапишите заголовок публикации.\n"
            "Для отмены отправьте /cancel.",
            _flow_keyboard(
                admin,
                ctx,
                return_action="return-publications",
            ),
        )
        return
    if action == "publication-publish":
        publication_id = admin.control._token_uuid(payload[0])
        await asyncio.to_thread(
            admin_ops.publish_publication,
            actor=ctx.actor,
            publication_id=publication_id,
        )
        await _enhanced_marketing(callback, state, ctx, "publications")
        return
    if action == "pay-refund":
        payment_id = admin.control._token_uuid(payload[0])
        await admin._safe_edit(
            callback,
            "↩️ Полный возврат\n\n"
            "Подтвердите возврат. Он изменит статус оплаты и создаст "
            "отдельный канонический факт возврата; повторное нажатие "
            "не создаст дубль.",
            _flow_keyboard(
                admin,
                ctx,
                return_action="return-payments",
                extra=[
                    (
                        "Подтвердить возврат",
                        _ops_callback(
                            ctx,
                            "pay-refund-ok",
                            admin.control._uuid_token(payment_id),
                        ),
                    )
                ],
            ),
        )
        return
    if action == "pay-refund-ok":
        payment_id = admin.control._token_uuid(payload[0])
        try:
            refunded = await asyncio.to_thread(
                admin_ops.refund_payment,
                actor=ctx.actor,
                payment_id=payment_id,
                idempotency_key=f"telegram-refund:{payment_id}",
                reason="owner_confirmed_full_refund",
            )
        except (admin_ops.PaymentIdempotencyConflict, admin_ops.PaymentStateConflict):
            await callback.answer(
                "Возврат уже выполнен или больше недоступен",
                show_alert=True,
            )
            return
        await callback.answer(
            f"Возврат {_money(refunded.amount_minor, refunded.currency)} сохранён"
        )
        await _enhanced_marketing(callback, state, ctx, "payments")
        return
    if action == "payment-new":
        customers = await _optional_thread(
            list_customers,
            default=[],
            actor=ctx.actor,
            include_archived=False,
        )
        await state.clear()
        await state.update_data(cpao_business_id=ctx.business_id)
        customer_buttons = [
            (
                f"👤 {item.display_name or 'Клиент'}",
                _ops_callback(
                    ctx,
                    "pay-customer",
                    admin.control._uuid_token(item.id),
                ),
            )
            for item in customers[:20]
        ]
        customer_buttons.append(
            ("Без привязки к клиенту", _ops_callback(ctx, "pay-customer", "none"))
        )
        await admin._safe_edit(
            callback,
            "💰 Зафиксировать оплату вручную\n\n"
            "Выберите клиента или сохраните оплату без привязки:",
            _flow_keyboard(
                admin,
                ctx,
                return_action="return-payments",
                extra=customer_buttons,
            ),
        )
        return
    if action in {"payment-customer", "pay-customer"}:
        raw_customer = payload[0] if payload else "none"
        customer_id = (
            None
            if raw_customer == "none"
            else admin.control._token_uuid(raw_customer)
        )
        await state.clear()
        await state.update_data(
            cpao_business_id=ctx.business_id,
            cpao_payment_customer_id=customer_id,
        )
        prices = await _optional_thread(
            admin_ops.list_offering_prices,
            default=[],
            actor=ctx.actor,
        )
        if prices:
            buttons = [
                (
                    f"{item.offering_title[:24]} · "
                    f"{_money(item.amount_minor, item.currency)}",
                    _ops_callback(
                        ctx,
                        "pay-offer",
                        admin.control._uuid_token(item.offering_id),
                    ),
                )
                for item in prices[:10]
                if item.status == "active"
            ]
            buttons.append(
                ("Без предложения", _ops_callback(ctx, "pay-offer", "none"))
            )
            await admin._safe_edit(
                callback,
                "💰 Зафиксировать оплату вручную\n\n"
                "Выберите предложение. Его активная цена проверит валюту "
                "перед сохранением факта оплаты.",
                _flow_keyboard(
                    admin,
                    ctx,
                    return_action="return-payments",
                    extra=buttons,
                ),
            )
            return
        await _open_payment_value_step(
            callback,
            state,
            admin=admin,
            ctx=ctx,
            customer_id=customer_id,
            offering_id=None,
        )
        return
    if action == "pay-offer":
        data = await state.get_data()
        if "cpao_payment_customer_id" not in data:
            await callback.answer("Сценарий оплаты устарел", show_alert=True)
            return
        raw_offering = payload[0] if payload else "none"
        offering_id = (
            None
            if raw_offering == "none"
            else admin.control._token_uuid(raw_offering)
        )
        await _open_payment_value_step(
            callback,
            state,
            admin=admin,
            ctx=ctx,
            customer_id=data.get("cpao_payment_customer_id"),
            offering_id=offering_id,
        )
        return
    if action == "price-set":
        offering_id = admin.control._token_uuid(payload[0])
        await state.set_state(ClientPlatformAdminOpsState.price_value)
        await state.update_data(
            cpao_business_id=ctx.business_id,
            cpao_offering_id=offering_id,
        )
        await admin._safe_edit(
            callback,
            "💡 Цена предложения\n\n"
            "Отправьте сумму и валюту. Например: 5000 RUB.\n"
            "Для отмены отправьте /cancel.",
            _flow_keyboard(
                admin,
                ctx,
                return_action="return-prices",
            ),
        )
        return
    if action == "return-publications":
        await state.clear()
        await _enhanced_marketing(callback, state, ctx, "publications")
        return
    if action == "return-payments":
        await state.clear()
        await _enhanced_marketing(callback, state, ctx, "payments")
        return
    if action == "return-prices":
        await state.clear()
        await _enhanced_marketing(callback, state, ctx, "prices")
        return
    await callback.answer("Действие больше недоступно", show_alert=True)


@router.message(ClientPlatformAdminOpsState.publication_title)
async def receive_publication_title(message: Message, state: FSMContext) -> None:
    title = str(message.text or "").strip()
    if not title or title.startswith("/"):
        await message.answer("Напишите обычный заголовок или отправьте /cancel.")
        return
    await state.update_data(cpao_publication_title=title)
    await state.set_state(ClientPlatformAdminOpsState.publication_body)
    await message.answer("Теперь отправьте полный текст публикации.")


@router.message(ClientPlatformAdminOpsState.publication_body)
async def receive_publication_body(message: Message, state: FSMContext) -> None:
    body = str(message.text or "").strip()
    if not body or body.startswith("/"):
        await message.answer("Текст пустой. Отправьте публикацию или /cancel.")
        return
    data = await state.get_data()
    ctx = await _context_from_state(message, state)
    publication = await asyncio.to_thread(
        admin_ops.create_publication_draft,
        actor=ctx.actor,
        title=str(data["cpao_publication_title"]),
        body=body,
        channel=str(data.get("cpao_publication_channel") or "other"),
    )
    await state.clear()
    await message.answer(f"✅ Черновик «{publication.title}» создан.")
    admin = importlib.import_module(".clientplatform_admin", __package__)
    await admin.send_admin_panel(
        message,
        user_id=ctx.user_id,
        business_id=ctx.business_id,
    )


@router.message(ClientPlatformAdminOpsState.payment_value)
async def receive_payment_value(message: Message, state: FSMContext) -> None:
    try:
        amount_minor, currency, note = _parse_amount(str(message.text or ""))
    except ValueError as exc:
        await message.answer(f"{exc}. Пример: 3500 RUB консультация.")
        return
    data = await state.get_data()
    ctx = await _context_from_state(message, state)
    try:
        payment = await asyncio.to_thread(
            admin_ops.record_payment,
            actor=ctx.actor,
            amount_minor=amount_minor,
            idempotency_key=_telegram_payment_idempotency_key(message),
            currency=currency,
            customer_id=data.get("cpao_payment_customer_id"),
            offering_id=data.get("cpao_payment_offering_id"),
            note=note,
        )
    except TenantPermissionDenied:
        await state.clear()
        await message.answer(
            "Для вашей роли финансовые данные доступны только для просмотра."
        )
        return
    except ValueError as exc:
        await message.answer(f"Оплата не сохранена: {exc}.")
        return
    await state.clear()
    outcome_id = getattr(payment, "outcome_event_id", None)
    evidence = (
        " Канонический факт выручки подтверждён."
        if outcome_id is not None
        else ""
    )
    await message.answer(
        f"✅ Оплата сохранена: {_money(payment.amount_minor, payment.currency)}."
        f"{evidence}"
    )
    admin = importlib.import_module(".clientplatform_admin", __package__)
    await admin.send_admin_panel(
        message,
        user_id=ctx.user_id,
        business_id=ctx.business_id,
    )


@router.message(ClientPlatformAdminOpsState.price_value)
async def receive_price_value(message: Message, state: FSMContext) -> None:
    try:
        amount_minor, currency, _note = _parse_amount(str(message.text or ""))
    except ValueError as exc:
        await message.answer(f"{exc}. Пример: 5000 RUB.")
        return
    data = await state.get_data()
    ctx = await _context_from_state(message, state)
    try:
        price = await asyncio.to_thread(
            admin_ops.set_offering_price,
            actor=ctx.actor,
            offering_id=str(data["cpao_offering_id"]),
            amount_minor=amount_minor,
            currency=currency,
        )
    except TenantPermissionDenied:
        await state.clear()
        await message.answer(
            "Для вашей роли финансовые данные доступны только для просмотра."
        )
        return
    except ValueError as exc:
        await message.answer(f"Цена не сохранена: {exc}.")
        return
    await state.clear()
    await message.answer(
        f"✅ Цена «{price.offering_title}»: "
        f"{_money(price.amount_minor, price.currency)}."
    )
    admin = importlib.import_module(".clientplatform_admin", __package__)
    await admin.send_admin_panel(
        message,
        user_id=ctx.user_id,
        business_id=ctx.business_id,
    )


async def _record_trace(
    *,
    event: CallbackQuery,
    trace: _InteractionTrace,
    success: bool,
    error_code: str | None,
    total_ms: int,
    data: dict[str, Any],
) -> None:
    raw = str(event.data or "")
    parts = raw.split(":")
    if len(parts) < 3 or parts[0] not in {"cpa", "cpao"}:
        return
    admin = importlib.import_module(".clientplatform_admin", __package__)
    try:
        business_id = admin.control._token_uuid(parts[1])
    except (TypeError, ValueError):
        return
    bot = data.get("bot") or getattr(event, "bot", None)
    session = getattr(bot, "session", None)
    app_ms = max(
        0,
        total_ms
        - trace.ack_ms
        - trace.lock_wait_ms
        - trace.telegram_ms,
    )
    metric = admin_ops.InteractionMetricInput(
        business_id=business_id,
        actor_user_id=int(event.from_user.id),
        callback_action=parts[2],
        success=success,
        ack_ms=trace.ack_ms,
        lock_wait_ms=trace.lock_wait_ms,
        app_ms=app_ms,
        telegram_ms=trace.telegram_ms,
        total_ms=total_ms,
        transport_role=str(getattr(session, "transport_role", "ui")),
        transport_route=str(getattr(session, "active_route", "unknown")),
        transport_generation=getattr(session, "transport_generation", None),
        error_code=error_code,
    )
    try:
        await asyncio.to_thread(admin_ops.record_interaction_metric, metric)
    except TenancyError:
        return
    except ValueError:
        return
    except RuntimeError:
        log.warning("Failed to persist ClientPlatform interaction metric", exc_info=True)
    except OSError:
        log.warning("Failed to persist ClientPlatform interaction metric", exc_info=True)


def _install_trace_hooks(admin: ModuleType, safety: ModuleType) -> None:
    if bool(getattr(safety, "_clientplatform_admin_trace_installed", False)):
        return

    original_answer = safety._answer_callback
    original_call = safety.ClientPlatformInteractionSafetyMiddleware.__call__
    original_safe_edit = admin._safe_edit

    async def traced_answer(
        callback: CallbackQuery,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        trace = _TRACE.get()
        started = time.perf_counter()
        try:
            await original_answer(
                callback,
                text=text,
                show_alert=show_alert,
            )
        finally:
            if trace is not None:
                trace.ack_ms += max(
                    0,
                    round((time.perf_counter() - started) * 1000),
                )
                trace.ack_finished = time.perf_counter()

    async def traced_safe_edit(
        callback: CallbackQuery,
        text: str,
        reply_markup: Any,
    ) -> None:
        trace = _TRACE.get()
        started = time.perf_counter()
        try:
            await original_safe_edit(callback, text, reply_markup)
        finally:
            if trace is not None:
                trace.telegram_ms += max(
                    0,
                    round((time.perf_counter() - started) * 1000),
                )

    async def traced_call(
        self: BaseMiddleware,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery) or not str(
            event.data or ""
        ).startswith(("cpa:", "cpao:")):
            return await original_call(self, handler, event, data)

        trace = _InteractionTrace(started=time.perf_counter())
        token = _TRACE.set(trace)
        success = False
        handler_invoked = False
        error_code: str | None = None

        async def marked_handler(
            marked_event: TelegramObject,
            marked_data: dict[str, Any],
        ) -> Any:
            nonlocal handler_invoked
            handler_invoked = True
            now = time.perf_counter()
            trace.handler_started = now
            anchor = trace.ack_finished or trace.started
            trace.lock_wait_ms = max(0, round((now - anchor) * 1000))
            return await handler(marked_event, marked_data)

        try:
            result = await original_call(
                self,
                marked_handler,
                event,
                data,
            )
            success = handler_invoked
            if not handler_invoked:
                error_code = "suppressed_callback"
            return result
        finally:
            if error_code is None:
                active_error = sys.exc_info()[1]
                if active_error is not None:
                    error_code = type(active_error).__name__
            total_ms = max(
                0,
                round((time.perf_counter() - trace.started) * 1000),
            )
            try:
                await _record_trace(
                    event=event,
                    trace=trace,
                    success=success,
                    error_code=error_code,
                    total_ms=total_ms,
                    data=data,
                )
            finally:
                _TRACE.reset(token)

    safety._answer_callback = traced_answer
    admin._safe_edit = traced_safe_edit
    safety.ClientPlatformInteractionSafetyMiddleware.__call__ = traced_call
    safety._clientplatform_admin_trace_installed = True


def install_admin_extension(entry_router: Router, _control: ModuleType) -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    install_multi_egress_bot()
    admin_observability.install_health_contract()

    admin = importlib.import_module(".clientplatform_admin", __package__)
    safety = importlib.import_module(
        ".clientplatform_interaction_safety",
        __package__,
    )
    _install_trace_hooks(admin, safety)

    admin._render_attention = _enhanced_attention
    admin._render_marketing = _enhanced_marketing
    admin._render_admin_report = _enhanced_admin_report
    admin._render_tariff = _enhanced_tariff

    entry_router.include_router(router)
    entry_router.startup.register(
        admin_observability.start_admin_observability
    )
    entry_router.shutdown.register(
        admin_observability.stop_admin_observability
    )
    _INSTALLED = True


__all__ = [
    "ClientPlatformAdminOpsState",
    "admin_ops_gate",
    "install_admin_extension",
    "router",
]
