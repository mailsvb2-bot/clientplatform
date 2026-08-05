from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import ModuleType
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.ad_connections import list_ad_publications
from clientplatform.application.ad_spend import prepare_ad_spend_authorization
from clientplatform.application.ad_spend_consent import (
    grant_ad_spend_consent,
    list_ad_spend_authorizations,
    request_ad_spend_consent,
    revoke_ad_spend_consent,
)
from clientplatform.domain.ad_connections import (
    AdConnectionError,
    AdPublicationStatus,
)
from clientplatform.domain.ad_spend import (
    AdSpendAuthorization,
    AdSpendAuthorizationStatus,
    AdSpendError,
)
from clientplatform.integrations.yandex_direct import YandexDirectError


router = Router(name="clientplatform_ad_spend")
_CONTROL: ModuleType | None = None


class AdSpendConsentState(StatesGroup):
    waiting_hard_cap = State()
    waiting_daily_cap = State()
    confirming_consent = State()


_STATUS_LABELS = {
    AdSpendAuthorizationStatus.DRAFT: "подготовлено",
    AdSpendAuthorizationStatus.AWAITING_CONSENT: "ожидает подтверждения владельца",
    AdSpendAuthorizationStatus.AUTHORIZED: "согласие зафиксировано",
    AdSpendAuthorizationStatus.LAUNCHING: "запускается",
    AdSpendAuthorizationStatus.ACTIVE: "активно",
    AdSpendAuthorizationStatus.STOPPING: "останавливается",
    AdSpendAuthorizationStatus.STOPPED: "остановлено",
    AdSpendAuthorizationStatus.EXPIRED: "истекло",
    AdSpendAuthorizationStatus.REVOKED: "отозвано",
    AdSpendAuthorizationStatus.FAILED: "ошибка",
}
_TERMINAL = {
    AdSpendAuthorizationStatus.STOPPED,
    AdSpendAuthorizationStatus.EXPIRED,
    AdSpendAuthorizationStatus.REVOKED,
    AdSpendAuthorizationStatus.FAILED,
}


def _control() -> ModuleType:
    if _CONTROL is None:
        raise RuntimeError("ad spend Telegram controls are not installed")
    return _CONTROL


def _message(callback: CallbackQuery) -> Message:
    return _control()._callback_message(callback)


def _parse_minor_units(value: object) -> int:
    raw = str(value or "").strip().replace(" ", "").replace(",", ".")
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("amount is not numeric") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be positive")
    minor = amount * 100
    if minor != minor.to_integral_value():
        raise ValueError("amount must have at most two decimal places")
    parsed = int(minor)
    if parsed > 9_000_000_000_000_000:
        raise ValueError("amount is too large")
    return parsed


def _format_minor(value: int, currency: str = "") -> str:
    amount = Decimal(int(value)) / Decimal(100)
    rendered = f"{amount:.2f}".replace(".", ",")
    return f"{rendered} {currency}".strip()


def _authorization_line(item: AdSpendAuthorization) -> str:
    return (
        f"• Кампания {item.external_campaign_id}: {_STATUS_LABELS[item.status]} · "
        f"лимит {_format_minor(item.hard_cap_minor, item.currency)}"
    )


def install_ad_spend_controls(
    control_module: ModuleType,
    simple_module: ModuleType,
) -> None:
    """Compose one explicit owner-consent entry into the advanced dashboard."""

    global _CONTROL
    if bool(getattr(control_module, "_ad_spend_controls_installed", False)):
        return
    _CONTROL = control_module
    original = control_module._dashboard_keyboard

    def _dashboard_with_ad_spend(business_id: str, capabilities: list[object]):
        base = original(business_id, capabilities)
        rows = [list(row) for row in base.inline_keyboard]
        button = [
            InlineKeyboardButton(
                text="💳 Безопасный запуск рекламы",
                callback_data=f"cpsp:home:{control_module._uuid_token(business_id)}",
            )
        ]
        insert_at = max(0, len(rows) - 1)
        rows.insert(insert_at, button)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    control_module._dashboard_keyboard = _dashboard_with_ad_spend
    simple_module._ADVANCED_KEYBOARD = _dashboard_with_ad_spend
    simple_module.router.include_router(router)
    control_module._ad_spend_controls_installed = True


@router.callback_query(F.data.startswith("cpsp:home:"))
async def open_ad_spend_controls(callback: CallbackQuery, state: FSMContext) -> None:
    c = _control()
    await state.clear()
    business_token = str(callback.data).split(":", 2)[2]
    business_id = c._token_uuid(business_token)
    try:
        actor = await c._actor(int(callback.from_user.id), business_id)
        jobs, authorizations = await asyncio.gather(
            asyncio.to_thread(list_ad_publications, actor=actor),
            asyncio.to_thread(list_ad_spend_authorizations, actor=actor),
        )
    except (AdConnectionError, AdSpendError, RuntimeError, ValueError):
        await callback.answer("Не удалось открыть безопасный запуск", show_alert=True)
        return

    submitted = [item for item in jobs if item.status == AdPublicationStatus.SUBMITTED]
    rows: list[list[tuple[str, str]]] = [
        [
            (
                f"🚀 Настроить · {item.external_campaign_name[:34]}",
                "cpsp:job:"
                f"{business_token}:{c._uuid_token(item.id)}",
            )
        ]
        for item in submitted[:10]
    ]
    for item in authorizations[:10]:
        if item.status not in _TERMINAL:
            rows.append(
                [
                    (
                        f"⛔ Отозвать · {item.external_campaign_id}",
                        "cpsp:revoke:"
                        f"{business_token}:{c._uuid_token(item.id)}",
                    )
                ]
            )
    rows.append([("⬅️ К рекламным кабинетам", f"cpa:home:{business_token}")])

    history = "\n".join(_authorization_line(item) for item in authorizations[:8])
    if not history:
        history = "• разрешений пока нет"
    await callback.answer()
    await _message(callback).answer(
        "💳 Безопасный запуск рекламы\n\n"
        "Здесь подтверждаются именно показы и расходы. Подтверждение создания "
        "черновика DRAFT никогда не считается согласием на списание бюджета.\n\n"
        "Перед отдельным подтверждением ClientPlatform заново прочитает состояние "
        "кампании и расход из Яндекс Директа, проверит владельца бизнеса и покажет "
        "точные лимиты.\n\n"
        f"Последние разрешения:\n{history}\n\n"
        + (
            "Выберите созданный в Яндексе черновик:"
            if submitted
            else "Сначала создайте рекламный черновик и дождитесь статуса «черновик создан в Яндексе»."
        ),
        reply_markup=c._keyboard(rows),
    )


@router.callback_query(F.data.startswith("cpsp:job:"))
async def choose_ad_spend_job(callback: CallbackQuery, state: FSMContext) -> None:
    c = _control()
    try:
        _, _, business_token, job_token = str(callback.data).split(":", 3)
        business_id = c._token_uuid(business_token)
        publication_job_id = c._token_uuid(job_token)
        await c._actor(int(callback.from_user.id), business_id)
    except (ValueError, RuntimeError):
        await callback.answer("Черновик больше не найден", show_alert=True)
        return
    await state.set_state(AdSpendConsentState.waiting_hard_cap)
    await state.set_data(
        {
            "business_id": business_id,
            "business_token": business_token,
            "publication_job_id": publication_job_id,
        }
    )
    await callback.answer()
    await _message(callback).answer(
        "Укажите максимальный общий расход в валюте рекламного кабинета.\n\n"
        "Например: 500 или 500,00. Больше этой суммы разрешение не даст потратить."
    )


@router.message(AdSpendConsentState.waiting_hard_cap)
async def receive_ad_spend_hard_cap(message: Message, state: FSMContext) -> None:
    try:
        hard_cap_minor = _parse_minor_units(message.text)
    except ValueError:
        await message.answer("Введите положительную сумму, например 500 или 500,00.")
        return
    await state.update_data(hard_cap_minor=hard_cap_minor)
    await state.set_state(AdSpendConsentState.waiting_daily_cap)
    await message.answer(
        "Теперь укажите максимальный расход за день.\n\n"
        "Он не может быть больше общего лимита. Например: 100."
    )


@router.message(AdSpendConsentState.waiting_daily_cap)
async def receive_ad_spend_daily_cap(message: Message, state: FSMContext) -> None:
    c = _control()
    data = await state.get_data()
    try:
        daily_cap_minor = _parse_minor_units(message.text)
        hard_cap_minor = int(data["hard_cap_minor"])
        if daily_cap_minor > hard_cap_minor:
            raise ValueError("daily cap exceeds hard cap")
        business_id = str(data["business_id"])
        actor = await c._actor(c._user_id(message), business_id)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        prepared = await asyncio.to_thread(
            prepare_ad_spend_authorization,
            actor=actor,
            publication_job_id=str(data["publication_job_id"]),
            hard_cap_minor=hard_cap_minor,
            daily_cap_minor=daily_cap_minor,
            authorization_expires_at=now + timedelta(minutes=5),
            provider_report_date=now.date(),
            now=now,
        )
        authorization = await asyncio.to_thread(
            request_ad_spend_consent,
            actor=actor,
            authorization_id=prepared.authorization.id,
            now=now,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        AdConnectionError,
        AdSpendError,
        YandexDirectError,
        RuntimeError,
    ):
        await message.answer(
            "Не удалось подготовить безопасное разрешение. Проверьте суммы, "
            "подключение кабинета и статус кампании, затем попробуйте снова."
        )
        return

    await state.update_data(
        authorization_id=authorization.id,
        authorization_token=c._uuid_token(authorization.id),
    )
    await state.set_state(AdSpendConsentState.confirming_consent)
    expiry = authorization.authorization_expires_at.replace("T", " ").replace("+00:00", " UTC")
    await message.answer(
        "⚠️ Отдельное согласие на показы и расходы\n\n"
        f"Кампания Яндекса: {authorization.external_campaign_id}\n"
        f"Регионы: {', '.join(str(item) for item in authorization.region_ids)}\n"
        f"Валюта: {authorization.currency}\n"
        f"Доступный бюджет по данным Яндекса: "
        f"{_format_minor(authorization.snapshot.available_budget_minor, authorization.currency)}\n"
        f"Уже потрачено сегодня: "
        f"{_format_minor(authorization.snapshot.spent_today_minor, authorization.currency)}\n"
        f"Максимальный общий расход: "
        f"{_format_minor(authorization.hard_cap_minor, authorization.currency)}\n"
        f"Максимальный расход за день: "
        f"{_format_minor(authorization.daily_cap_minor, authorization.currency)}\n"
        f"Разрешение действительно до: {expiry}\n\n"
        "Подтверждая, Вы разрешаете ClientPlatform в дальнейшем поставить только "
        "этот конкретный DRAFT на модерацию и остановить его при достижении лимита. "
        "Кампания, регионы, стратегия и лимиты автоматически расширяться не будут.\n\n"
        "На текущем этапе подтверждение только фиксируется: запуск в Яндексе ещё не выполняется.",
        reply_markup=c._keyboard(
            [
                [
                    (
                        "✅ Подтверждаю точные условия",
                        "cpsp:confirm:"
                        f"{data['business_token']}:{c._uuid_token(authorization.id)}",
                    )
                ],
                [
                    (
                        "Отмена",
                        f"cpsp:home:{data['business_token']}",
                    )
                ],
            ]
        ),
    )


@router.callback_query(
    AdSpendConsentState.confirming_consent,
    F.data.startswith("cpsp:confirm:"),
)
async def confirm_ad_spend_consent(callback: CallbackQuery, state: FSMContext) -> None:
    c = _control()
    try:
        _, _, business_token, authorization_token = str(callback.data).split(":", 3)
        business_id = c._token_uuid(business_token)
        authorization_id = c._token_uuid(authorization_token)
        actor = await c._actor(int(callback.from_user.id), business_id)
        granted = await asyncio.to_thread(
            grant_ad_spend_consent,
            actor=actor,
            authorization_id=authorization_id,
        )
    except (AdSpendError, RuntimeError, ValueError):
        await callback.answer(
            "Разрешение устарело или изменилось. Подготовьте его заново.",
            show_alert=True,
        )
        return
    await state.clear()
    await callback.answer("Согласие зафиксировано")
    await _message(callback).answer(
        "✅ Согласие владельца сохранено неизменяемой квитанцией\n\n"
        f"Кампания: {granted.authorization.external_campaign_id}\n"
        f"Общий лимит: {_format_minor(granted.authorization.hard_cap_minor, granted.authorization.currency)}\n"
        f"Дневной лимит: {_format_minor(granted.authorization.daily_cap_minor, granted.authorization.currency)}\n"
        f"Квитанция: …{granted.receipt.receipt_hash[-12:]}\n\n"
        "Показы и расходы не запущены. Следующий защищённый слой — отдельная "
        "идемпотентная очередь запуска и остановки с повторной сверкой Яндекс Директа.",
        reply_markup=c._keyboard(
            [[("💳 К разрешениям", f"cpsp:home:{business_token}")]]
        ),
    )


@router.callback_query(F.data.startswith("cpsp:revoke:"))
async def revoke_ad_spend(callback: CallbackQuery, state: FSMContext) -> None:
    c = _control()
    try:
        _, _, business_token, authorization_token = str(callback.data).split(":", 3)
        business_id = c._token_uuid(business_token)
        authorization_id = c._token_uuid(authorization_token)
        actor = await c._actor(int(callback.from_user.id), business_id)
        revoked = await asyncio.to_thread(
            revoke_ad_spend_consent,
            actor=actor,
            authorization_id=authorization_id,
        )
    except (AdSpendError, RuntimeError, ValueError):
        await callback.answer("Не удалось отозвать разрешение", show_alert=True)
        return
    await state.clear()
    await callback.answer("Разрешение отозвано")
    await _message(callback).answer(
        f"⛔ Разрешение для кампании {revoked.external_campaign_id} отозвано.\n\n"
        "Отозванное согласие нельзя использовать для запуска.",
        reply_markup=c._keyboard(
            [[("💳 К разрешениям", f"cpsp:home:{business_token}")]]
        ),
    )


__all__ = [
    "AdSpendConsentState",
    "confirm_ad_spend_consent",
    "install_ad_spend_controls",
    "open_ad_spend_controls",
    "receive_ad_spend_daily_cap",
    "receive_ad_spend_hard_cap",
    "revoke_ad_spend",
    "router",
]
