from __future__ import annotations

"""Payment router.

Telegram digital packages use native Telegram Stars. VK, MAX and web checkout
remain on YooKassa. Legacy Telegram RUB invoice callback buttons stay disabled
so they cannot create a second in-Telegram payment path.
"""

import asyncio
import logging
import sqlite3

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery

from services.payments.common import is_user_share_message, safe_answer_callback
from services.payments.gift import (
    GiftCancelPendingFilter,
    deliver_gift_message,
    gift_menu,
    gift_pick_cancel,
    gift_pick_target,
    gift_users_shared,
)
from services.payments.hooks import successful_payment as legacy_successful_payment
from services.payments.subscription import cmd_subscribe, sub_menu
from services.payments.telegram_stars import (
    STARS_CURRENCY,
    StarsPaymentError,
    record_successful_stars_payment,
    send_stars_invoice,
    validate_stars_pre_checkout,
)
from services.payments.telegram_stars_refunds import (
    StarsRefundError,
    StarsRefundPlan,
    cancel_prepared_stars_refund,
    complete_stars_refund,
    mark_stars_refund_provider_succeeded,
    prepare_stars_refund,
    preview_stars_refund,
    refund_plan_text,
)
from services.payments.terms import (
    payment_support_contact,
    payment_terms_keyboard,
    payment_terms_text,
)
from services.payments.ui import (
    kb_after_paid,
    kb_back,
    kb_telegram_payment_methods,
    telegram_payment_method_text,
)
from services.admin import is_platform_admin
from services.events import log_event

router = Router()
log = logging.getLogger(__name__)

_DISABLED = "Этот старый способ оплаты отключён. Откройте тарифы заново и выберите актуальный пакет."


def _message_user_id(message: Message) -> int | None:
    user = message.from_user
    return int(user.id) if user is not None else None


def _already_refunded_error(exc: BaseException) -> bool:
    text = str(exc).strip().lower().replace("_", " ").replace("-", " ")
    return "already refunded" in text or "payment was refunded" in text


async def _cancel_refund_hold(charge_id: str, error: str) -> bool:
    try:
        await asyncio.to_thread(cancel_prepared_stars_refund, charge_id, error=error)
        return True
    except (StarsRefundError, sqlite3.Error):
        log.exception("Telegram Stars refund hold release failed: charge_id=%s", charge_id)
        return False


def _refund_command_args(text: str | None) -> tuple[str, bool] | None:
    parts = (text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].strip():
        return None
    return parts[1].strip(), len(parts) == 3 and parts[2].strip().upper() == "CONFIRM"


async def _load_refund_plan(message: Message, charge_id: str) -> StarsRefundPlan | None:
    try:
        return await asyncio.to_thread(preview_stars_refund, charge_id)
    except (StarsRefundError, sqlite3.Error):
        log.exception("Telegram Stars refund preview failed: charge_id=%s", charge_id)
        await message.answer("Не удалось проверить платёж. Повторите позже или проверьте журнал сервера.")
        return None


async def _prepare_refund_plan(message: Message, charge_id: str, admin_id: int) -> StarsRefundPlan | None:
    try:
        return await asyncio.to_thread(prepare_stars_refund, charge_id, requested_by=admin_id)
    except (StarsRefundError, sqlite3.Error):
        log.exception("Telegram Stars refund preparation failed: charge_id=%s", charge_id)
        await message.answer("Не удалось безопасно подготовить возврат. Доступ пользователя не изменён.")
        return None


async def _provider_refund_failure(message: Message, charge_id: str, error: str, lead: str) -> bool:
    restored = await _cancel_refund_hold(charge_id, error)
    tail = "Удержанные практики восстановлены." if restored else "Нужна ручная проверка удержания практик."
    await message.answer(f"{lead} {tail}")
    return False


async def _provider_refund_ambiguous(message: Message, charge_id: str, exc: BaseException) -> bool:
    log.exception("Telegram Stars refund result is ambiguous: charge_id=%s", charge_id)
    await message.answer(
        "Telegram не вернул однозначный результат возврата. Доступ остаётся временно удержанным: "
        "запустите эту же команду ещё раз. Повтор безопасен и завершит или подтвердит возврат."
    )
    return False


async def _run_provider_refund(message: Message, prepared: StarsRefundPlan) -> bool:
    if prepared.status == "provider_refunded":
        return True
    charge_id = prepared.telegram_charge_id
    try:
        provider_ok = bool(await message.bot.refund_star_payment(
            user_id=prepared.payment_user_id,
            telegram_payment_charge_id=charge_id,
        ))
    except TelegramBadRequest as exc:
        if not _already_refunded_error(exc):
            log.exception("Telegram Stars refund rejected by Telegram: charge_id=%s", charge_id)
            return await _provider_refund_failure(message, charge_id, str(exc), "Telegram отклонил возврат.")
        provider_ok = True
    except (TelegramAPIError, asyncio.TimeoutError) as exc:
        return await _provider_refund_ambiguous(message, charge_id, exc)

    if not provider_ok:
        return await _provider_refund_failure(
            message,
            charge_id,
            "refundStarPayment returned false",
            "Telegram не подтвердил возврат.",
        )
    try:
        await asyncio.to_thread(mark_stars_refund_provider_succeeded, charge_id)
        return True
    except (StarsRefundError, sqlite3.Error):
        log.exception("Telegram refund succeeded but local provider state failed: charge_id=%s", charge_id)
        await message.answer(
            "⚠️ Telegram подтвердил возврат, но локальная финализация не завершилась. "
            "Не повторяйте новый возврат; запустите эту же команду ещё раз."
        )
        return False


async def _complete_refund(message: Message, charge_id: str) -> StarsRefundPlan | None:
    try:
        return await asyncio.to_thread(complete_stars_refund, charge_id)
    except (StarsRefundError, sqlite3.Error):
        log.exception("Telegram Stars local refund finalization failed: charge_id=%s", charge_id)
        await message.answer(
            "⚠️ Telegram подтвердил возврат, но локальный доступ ещё не отозван. "
            "Запустите эту же команду ещё раз для безопасной финализации."
        )
        return None


async def _refund_plan_allows_execution(
    message: Message,
    plan: StarsRefundPlan,
    *,
    confirmed: bool,
) -> bool:
    if plan.status == "completed":
        await message.answer(refund_plan_text(plan) + "\n\n✅ Возврат уже завершён.")
        return False
    if not confirmed:
        tail = f"\n\nДля выполнения: /refundstars {plan.telegram_charge_id} CONFIRM" if plan.refundable else ""
        await message.answer(refund_plan_text(plan) + tail)
        return False
    if not plan.refundable:
        await message.answer(refund_plan_text(plan) + "\n\n⛔ Автоматический возврат заблокирован.")
        return False
    return True


@router.message(Command("refundstars"))
async def cmd_refund_stars(message: Message) -> None:
    admin_id = _message_user_id(message)
    if admin_id is None or not is_platform_admin(admin_id):
        return

    command = _refund_command_args(message.text)
    if command is None:
        await message.answer(
            "Использование:\n"
            "/refundstars <telegram_charge_id> — проверить\n"
            "/refundstars <telegram_charge_id> CONFIRM — выполнить возврат"
        )
        return

    charge_id, confirmed = command
    plan = await _load_refund_plan(message, charge_id)
    if plan is None:
        return
    if not await _refund_plan_allows_execution(message, plan, confirmed=confirmed):
        return

    prepared = await _prepare_refund_plan(message, charge_id, admin_id)
    if prepared is None:
        return
    if not await _run_provider_refund(message, prepared):
        return
    completed = await _complete_refund(message, charge_id)
    if completed is None:
        return

    await asyncio.to_thread(
        log_event,
        completed.payment_user_id,
        "telegram_stars_refunded",
        {"charge_id": charge_id, "package_id": completed.package_id, "by": admin_id},
    )
    await message.answer(refund_plan_text(completed) + "\n\n✅ Stars возвращены, доступ отозван.")


def _callback_message(cb: CallbackQuery) -> Message | None:
    message = cb.message
    return message if isinstance(message, Message) else None


def _package_id(data: str | None, prefix: str) -> str:
    raw = str(data or "")
    if not raw.startswith(prefix):
        raise ValueError("stars_package_callback_invalid")
    package_id = raw[len(prefix):].strip()
    if not package_id:
        raise ValueError("stars_package_callback_invalid")
    return package_id


async def _show_stars_terms(cb: CallbackQuery, *, as_gift: bool) -> None:
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    prefix = "stars:gift_terms:" if as_gift else "stars:terms:"
    try:
        package_id = _package_id(cb.data, prefix)
    except ValueError:
        log.exception("Telegram Stars terms callback invalid")
        await message.answer("Пакет не найден. Откройте тарифы заново.", reply_markup=kb_back("sub:menu"))
        return
    await message.answer(
        payment_terms_text(),
        reply_markup=payment_terms_keyboard(package_id=package_id, as_gift=as_gift),
    )


async def _show_payment_methods(cb: CallbackQuery, *, as_gift: bool) -> None:
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    prefix = "pay:gift_methods:" if as_gift else "pay:methods:"
    try:
        package_id = _package_id(cb.data, prefix)
        user_id = int(cb.from_user.id)
        text = telegram_payment_method_text(package_id)
        markup = kb_telegram_payment_methods(
            user_id=user_id,
            package_id=package_id,
            gift=as_gift,
        )
    except (TypeError, ValueError):
        log.exception("Payment method choice callback invalid")
        await message.answer(
            "Не удалось открыть способы оплаты. Выберите пакет заново.",
            reply_markup=kb_back("gift:menu" if as_gift else "sub:menu"),
        )
        return
    await asyncio.to_thread(
        log_event,
        user_id,
        "payment_method_choice_opened",
        {"package_id": package_id, "gift": bool(as_gift), "surface": "telegram"},
    )
    await message.answer(text, reply_markup=markup)


async def _send_stars_from_callback(cb: CallbackQuery, *, as_gift: bool) -> None:
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    prefix = "stars:gift:" if as_gift else "stars:buy:"
    try:
        package_id = _package_id(cb.data, prefix)
        await send_stars_invoice(message, package_id=package_id, as_gift=as_gift)
    except (StarsPaymentError, ValueError):
        log.exception("Telegram Stars invoice creation failed")
        await message.answer(
            "Не удалось создать счёт в Stars. Выберите пакет ещё раз.",
            reply_markup=kb_back("sub:menu" if not as_gift else "gift:menu"),
        )
    except (TelegramAPIError, asyncio.TimeoutError):
        log.exception("Telegram Stars API invoice request failed")
        await message.answer(
            "Telegram временно не создал счёт в Stars. Попробуйте ещё раз позже.",
            reply_markup=kb_back("sub:menu" if not as_gift else "gift:menu"),
        )


async def _answer_stars_manual_recovery(message: Message) -> None:
    log.exception("Telegram Stars payment requires manual recovery")
    await message.answer(
        "Оплата в Stars получена, но автоматическое начисление не завершилось. "
        "Пожалуйста, отправьте /paysupport и сохраните сообщение Telegram об оплате — "
        "поддержка проверит платёж по его идентификатору."
    )


@router.message(F.text == "❌ Отмена", GiftCancelPendingFilter())
async def _gift_pick_cancel(message: Message, gift_cancel_pending=None):
    await gift_pick_cancel(message, gift_cancel_pending=gift_cancel_pending)


@router.message(Command("subscribe"))
async def _cmd_subscribe(message: Message):
    await cmd_subscribe(message)


@router.message(Command("terms"))
async def _terms(message: Message):
    await message.answer(payment_terms_text())


@router.message(Command("paysupport"))
async def _pay_support(message: Message):
    await message.answer(
        "Поддержка по оплате\n\n"
        f"Напишите {payment_support_contact()} и приложите:\n"
        "• дату и примерное время оплаты;\n"
        "• выбранный пакет;\n"
        "• способ оплаты и сумму;\n"
        "• скриншот чека, если он есть.\n\n"
        "Telegram и его служба поддержки не обрабатывают споры по покупкам у бота. "
        "Не отправляйте данные банковской карты, коды из SMS и пароли."
    )


@router.callback_query(F.data == "sub:menu")
async def _sub_menu(cb: CallbackQuery):
    await safe_answer_callback(cb)
    await sub_menu(cb)


@router.callback_query(F.data == "stars:terms")
async def _stars_terms_overview(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is not None:
        await message.answer(payment_terms_text(), reply_markup=kb_back("sub:menu"))


@router.callback_query(F.data.regexp(r"^pay:methods:[a-z0-9_]+$"))
async def _payment_methods(cb: CallbackQuery):
    await _show_payment_methods(cb, as_gift=False)


@router.callback_query(F.data.regexp(r"^pay:gift_methods:[a-z0-9_]+$"))
async def _gift_payment_methods(cb: CallbackQuery):
    await _show_payment_methods(cb, as_gift=True)


@router.callback_query(F.data.regexp(r"^stars:terms:[a-z0-9_]+$"))
async def _stars_terms(cb: CallbackQuery):
    await _show_stars_terms(cb, as_gift=False)


@router.callback_query(F.data.regexp(r"^stars:gift_terms:[a-z0-9_]+$"))
async def _stars_gift_terms(cb: CallbackQuery):
    await _show_stars_terms(cb, as_gift=True)


@router.callback_query(F.data.regexp(r"^stars:buy:[a-z0-9_]+$"))
async def _stars_buy(cb: CallbackQuery):
    await _send_stars_from_callback(cb, as_gift=False)


@router.callback_query(F.data.regexp(r"^stars:gift:[a-z0-9_]+$"))
async def _stars_gift(cb: CallbackQuery):
    await _send_stars_from_callback(cb, as_gift=True)


@router.callback_query(F.data.regexp(r"^yookassa:gift:[a-z0-9_]+$"))
async def _yookassa_gift(cb: CallbackQuery):
    await safe_answer_callback(
        cb,
        "Этот старый способ оплаты цифрового пакета в Telegram отключён. Выберите оплату Stars.",
        show_alert=True,
    )


@router.callback_query(F.data == "tariffs:stars_disabled")
async def _stars_disabled(cb: CallbackQuery):
    await safe_answer_callback(
        cb,
        "Оплата цифровых пакетов в Telegram временно недоступна. Попробуйте позже.",
        show_alert=True,
    )


@router.callback_query(F.data == "tariffs:public_base_missing")
async def _public_base_missing(cb: CallbackQuery):
    await safe_answer_callback(
        cb,
        "Оплата банковской картой временно недоступна. Попробуйте позже или напишите /paysupport.",
        show_alert=True,
    )


@router.callback_query(F.data == "tariffs:yookassa_disabled")
async def _yookassa_disabled(cb: CallbackQuery):
    await safe_answer_callback(
        cb,
        "Оплата банковской картой временно недоступна. Попробуйте Stars или напишите /paysupport.",
        show_alert=True,
    )


@router.callback_query(F.data.regexp(r"^sub:buy:\d+:\d+$"))
async def _sub_pick_disabled(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    await message.answer(_DISABLED, reply_markup=kb_back("sub:menu"))


@router.callback_query(F.data == "pay:selected")
async def _pay_selected_disabled(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    await message.answer(_DISABLED, reply_markup=kb_back("sub:menu"))


@router.callback_query(F.data == "gift:menu")
async def _gift_menu(cb: CallbackQuery):
    await safe_answer_callback(cb)
    await gift_menu(cb)


@router.callback_query(F.data == "gift:pick_target")
async def _gift_pick_target(cb: CallbackQuery):
    await safe_answer_callback(cb)
    await gift_pick_target(cb)


@router.message(is_user_share_message)
async def _gift_users_shared(message: Message, state: FSMContext):
    await gift_users_shared(message, state)


@router.callback_query(F.data.regexp(r"^gift:buy:\d+:\d+$"))
async def _gift_buy_disabled(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    await message.answer(_DISABLED, reply_markup=kb_back("gift:menu"))


@router.pre_checkout_query()
async def _pre_checkout(pre: PreCheckoutQuery):
    if str(pre.currency or "").upper() != STARS_CURRENCY:
        try:
            await pre.answer(
                ok=False,
                error_message=(
                    "Этот способ оплаты отключён. Цифровые пакеты внутри Telegram "
                    "можно оплатить только Telegram Stars. Откройте тарифы заново."
                ),
            )
        except (TelegramAPIError, asyncio.TimeoutError):
            log.exception("Legacy Telegram pre-checkout rejection failed")
        return
    error = await asyncio.to_thread(
        validate_stars_pre_checkout,
        payload=pre.invoice_payload,
        user_id=int(pre.from_user.id),
        currency=pre.currency,
        total_amount=pre.total_amount,
    )
    try:
        await pre.answer(ok=error is None, error_message=error)
    except (TelegramAPIError, asyncio.TimeoutError):
        log.exception("Telegram Stars pre-checkout answer failed")


@router.message(F.successful_payment)
async def _successful_payment(message: Message):
    payment = message.successful_payment
    if payment is None or str(payment.currency or "").upper() != STARS_CURRENCY:
        await legacy_successful_payment(message)
        return
    user = message.from_user
    if user is None:
        return
    try:
        result = await asyncio.to_thread(
            record_successful_stars_payment,
            user_id=int(user.id),
            payload=str(payment.invoice_payload or ""),
            total_amount=int(payment.total_amount or 0),
            currency=str(payment.currency or ""),
            telegram_charge_id=str(payment.telegram_payment_charge_id or ""),
            provider_charge_id=str(payment.provider_payment_charge_id or ""),
        )
    except StarsPaymentError:
        await _answer_stars_manual_recovery(message)
        return
    except sqlite3.Error:
        await _answer_stars_manual_recovery(message)
        return
    except (ValueError, RuntimeError):
        await _answer_stars_manual_recovery(message)
        return
    except OSError:
        await _answer_stars_manual_recovery(message)
        return

    if result.duplicate:
        if result.gift_token:
            code = result.gift_token.removeprefix("gift_")
            await deliver_gift_message(message, code)
            return
        await message.answer(
            "✅ Этот платёж Telegram Stars уже обработан. Повторного начисления не было.",
            reply_markup=kb_after_paid(),
        )
        return
    if result.gift_token:
        code = result.gift_token.removeprefix("gift_")
        await deliver_gift_message(message, code)
        return

    balance = ""
    if result.wallet_balance is not None:
        balance = f" На балансе: {result.wallet_balance} практик."
    await message.answer(
        f"✅ Оплата Telegram Stars прошла. Практики начислены.{balance}",
        reply_markup=kb_after_paid(),
    )
