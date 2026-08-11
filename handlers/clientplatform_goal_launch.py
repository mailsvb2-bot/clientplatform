from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from clientplatform.application.ad_goal_autopilot import prepare_goal_spend_consent
from clientplatform.application.ad_goal_publication import (
    GoalPublicationBusy,
    submit_goal_publication,
)
from clientplatform.application.ad_spend_consent import grant_ad_spend_consent
from clientplatform.application.ad_spend_operations import (
    ad_spend_mutations_enabled,
    queue_ad_spend_launch,
)
from clientplatform.domain.ad_connections import AdConnectionError
from clientplatform.domain.ad_spend import AdSpendError
from clientplatform.integrations.yandex_direct import YandexDirectError

from . import clientplatform_control as control
from . import clientplatform_goal_first_autopilot as goal


router = Router(name="clientplatform_goal_launch")


async def _send_success(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    business_token: str,
    authorization: object,
    operation: object,
) -> None:
    await state.clear()
    await callback.answer("Запуск принят")
    auth_token = control._uuid_token(authorization.id)
    await control._callback_message(callback).answer(
        "✅ Готово. Дальше ClientPlatform всё делает сама.\n\n"
        "Запуск поставлен в защищённую очередь. Перед обращением к Яндексу сервер "
        "ещё раз проверит кабинет, расход и лимиты. После достижения лимита будет "
        "поставлена автоматическая остановка.\n\n"
        f"Максимальный расход: {goal._money(authorization.hard_cap_minor, authorization.currency)}\n"
        f"Операция: …{operation.id[-12:]}",
        reply_markup=control._keyboard(
            [
                [("⏹ Остановить рекламу", f"cpsp:stop:{business_token}:{auth_token}")],
                [("🏠 В кабинет", f"cpj:home:{business_token}")],
            ]
        ),
    )


async def _grant_and_queue(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    business_token: str,
    actor: object,
    authorization_id: str,
    expected_terms_hash: str,
    expected_snapshot_hash: str,
) -> bool:
    try:
        granted = await asyncio.to_thread(
            grant_ad_spend_consent,
            actor=actor,
            authorization_id=authorization_id,
            expected_terms_hash=expected_terms_hash,
            expected_snapshot_hash=expected_snapshot_hash,
        )
        operation = await asyncio.to_thread(
            queue_ad_spend_launch,
            actor=actor,
            authorization_id=granted.authorization.id,
        )
    except (AdSpendError, RuntimeError, ValueError):
        return False
    await _send_success(
        callback,
        state,
        business_token=business_token,
        authorization=granted.authorization,
        operation=operation,
    )
    return True


def _recovery_rows(business_token: str):
    return control._keyboard(
        [
            [("🎬 Загрузить другое видео", f"cpo:custom-video:{business_token}")],
            [("🖼 Вместо него своя картинка", f"cpo:custom-image:{business_token}")],
            [("🧹 Продолжить без картинки и видео", f"cpo:custom-clear:{business_token}")],
            [("🏠 Не запускать", f"cpj:home:{business_token}")],
        ]
    )


@router.callback_query(F.data.startswith("cpo:launch:"))
async def prepare_real_launch(callback: CallbackQuery, state: FSMContext) -> None:
    """Treat the normal launch button as the one explicit spend confirmation."""

    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not goal._state_matches(data, business_token) or not data.get("job_id"):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    await callback.answer("Проверяю и готовлю запуск…")
    try:
        actor = await control._actor(int(callback.from_user.id), str(data["business_id"]))
        submitted = await asyncio.to_thread(
            submit_goal_publication,
            actor=actor,
            job_id=str(data["job_id"]),
        )
    except GoalPublicationBusy:
        await control._callback_message(callback).answer(
            "⏳ ClientPlatform уже готовит этот же черновик. Повторное нажатие не создаст дубль.",
            reply_markup=control._keyboard(
                [[("🔄 Проверить", f"cpo:launch:{business_token}")]]
            ),
        )
        return
    except (AdConnectionError, YandexDirectError, RuntimeError, ValueError):
        await control._callback_message(callback).answer(
            "Яндекс пока не дал безопасно продолжить. Ничего не запущено и деньги "
            "не расходуются. Когда кабинет будет готов, достаточно нажать ещё раз.",
            reply_markup=control._keyboard(
                [
                    [("🔄 Попробовать снова", f"cpo:launch:{business_token}")],
                    [("🏠 В кабинет", f"cpj:home:{business_token}")],
                ]
            ),
        )
        return

    if bool(getattr(submitted, "media_failed", False)):
        await state.set_state(goal.GoalFirstAutopilotState.customizing)
        await control._callback_message(callback).answer(
            "🎬 Яндекс не принял этот видеофайл.\n\n"
            "Реклама НЕ запущена, бюджет не расходуется. Не нужно разбираться в "
            "ошибке Яндекса: просто выберите, что сделать дальше. ClientPlatform "
            "сама заменит материал в уже подготовленном черновике.",
            reply_markup=_recovery_rows(business_token),
        )
        return

    if submitted.media_pending:
        await state.set_state(goal.GoalFirstAutopilotState.ready)
        await control._callback_message(callback).answer(
            "🎬 Видео уже загружено в Яндекс и сейчас конвертируется. ClientPlatform "
            "сама дождётся готовности и прикрепит его к объявлению. До этого показы "
            "не запускаю и деньги не расходую.",
            reply_markup=control._keyboard(
                [
                    [("🔄 Проверить позже", f"cpo:launch:{business_token}")],
                    [("🎨 Изменить материал", f"cpo:custom:{business_token}")],
                    [("🏠 Не запускать", f"cpj:home:{business_token}")],
                ]
            ),
        )
        return

    if not ad_spend_mutations_enabled():
        await state.set_state(goal.GoalFirstAutopilotState.ready)
        await control._callback_message(callback).answer(
            "✅ Черновик уже подготовлен в Яндексе, но реальный запуск сейчас "
            "отключён защитным переключателем ClientPlatform. Деньги не расходуются. "
            "Когда запуск будет разрешён оператором, повторное нажатие продолжит "
            "с того же места без дублей.",
            reply_markup=goal._result_keyboard(business_token, data),
        )
        return

    try:
        prepared = await asyncio.to_thread(
            prepare_goal_spend_consent,
            actor=actor,
            publication_job_id=submitted.job.id,
        )
    except (AdSpendError, YandexDirectError, RuntimeError, ValueError):
        await state.set_state(goal.GoalFirstAutopilotState.ready)
        await control._callback_message(callback).answer(
            "Реклама подготовлена, но свежая проверка бюджета Яндекса не позволила "
            "безопасно запустить её. Ничего не списывается.",
            reply_markup=goal._result_keyboard(business_token, data),
        )
        return

    authorization = prepared.authorization
    preview_matches = (
        str(data.get("preview_currency") or "") == authorization.currency
        and int(data.get("preview_hard_cap_minor") or 0) == authorization.hard_cap_minor
        and int(data.get("preview_daily_cap_minor") or 0) == authorization.daily_cap_minor
    )
    if preview_matches:
        launched = await _grant_and_queue(
            callback,
            state,
            business_token=business_token,
            actor=actor,
            authorization_id=authorization.id,
            expected_terms_hash=authorization.terms_hash,
            expected_snapshot_hash=authorization.snapshot.snapshot_hash,
        )
        if launched:
            return
        await callback.answer(
            "Запуск не состоялся. Ничего не списано.",
            show_alert=True,
        )
        return

    await state.update_data(
        preview_currency=authorization.currency,
        preview_hard_cap_minor=authorization.hard_cap_minor,
        preview_daily_cap_minor=authorization.daily_cap_minor,
        authorization_id=authorization.id,
        expected_terms_hash=authorization.terms_hash,
        expected_snapshot_hash=authorization.snapshot.snapshot_hash,
    )
    await state.set_state(goal.GoalFirstAutopilotState.confirming_launch)
    await control._callback_message(callback).answer(
        "💳 Сумма изменилась после свежей проверки Яндекса\n\n"
        "Я не запускаю рекламу по условиям, которых вы не видели. Подтвердите "
        "обновлённый максимум:\n\n"
        f"Максимум всего: {goal._money(authorization.hard_cap_minor, authorization.currency)}\n"
        f"Максимум за день: {goal._money(authorization.daily_cap_minor, authorization.currency)}",
        reply_markup=control._keyboard(
            [
                [("🚀 Да, запустить с этим лимитом", f"cpo:launch-confirm:{business_token}")],
                [("🏠 Нет, не запускать", f"cpj:home:{business_token}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpo:launch-confirm:"))
async def confirm_real_launch(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not goal._state_matches(data, business_token):
        await callback.answer("Подтверждение устарело", show_alert=True)
        return
    try:
        actor = await control._actor(int(callback.from_user.id), str(data["business_id"]))
        authorization_id = str(data["authorization_id"])
        expected_terms_hash = str(data["expected_terms_hash"])
        expected_snapshot_hash = str(data["expected_snapshot_hash"])
    except (KeyError, RuntimeError, ValueError):
        await callback.answer("Подтверждение устарело", show_alert=True)
        return
    launched = await _grant_and_queue(
        callback,
        state,
        business_token=business_token,
        actor=actor,
        authorization_id=authorization_id,
        expected_terms_hash=expected_terms_hash,
        expected_snapshot_hash=expected_snapshot_hash,
    )
    if not launched:
        await callback.answer(
            "Условия успели измениться. Я не буду запускать рекламу по устаревшему разрешению.",
            show_alert=True,
        )


__all__ = ["confirm_real_launch", "prepare_real_launch", "router"]
