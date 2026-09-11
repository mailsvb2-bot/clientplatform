from __future__ import annotations

"""Discoverable owner image creation over the canonical visual gateway."""

import asyncio
import os
import tempfile
from types import ModuleType
from typing import Callable, cast

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from clientplatform.application.creative_generation import (
    abandon_creative_generation,
    authorize_creative_generation_redelivery,
    begin_creative_generation_submission,
    claim_creative_generation_delivery,
    get_active_creative_generation,
    get_creative_generation,
    mark_creative_generation_delivered,
    prepare_creative_generation,
    remember_creative_generation_job,
)
from clientplatform.application.creative_studio_publication import load_goal_visual_brand
from clientplatform.application.visual_creatives import (
    VisualCreativeError,
    create_business_image_from_frozen_payload,
    freeze_business_image_payload,
    materialize_ad_visual,
    normalize_business_image_request,
    poll_ad_visual,
)
from clientplatform.domain.creative_generation import (
    CreativeGenerationReceipt,
    CreativeGenerationReceiptStatus,
)
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.presentation import owner_navigation as nav

from . import clientplatform_control as control


router = Router(name="clientplatform_creative_studio")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ClientPlatformCreativeStudioState(StatesGroup):
    waiting_prompt = State()


def _menu_rows(token: str, active: CreativeGenerationReceipt | None = None):
    rows: list[list[tuple[str, str]]] = []
    if active is not None:
        label = (
            "⚠️ Проверить доставку"
            if active.delivery_claimed_at
            else (
                "✅ Получить готовую картинку"
                if active.status == CreativeGenerationReceiptStatus.SUCCEEDED
                else "🔄 Продолжить создание"
            )
        )
        action = "generate" if active.status in {
            CreativeGenerationReceiptStatus.PREPARED,
            CreativeGenerationReceiptStatus.SUBMITTING,
        } else "check"
        rows.append([(label, _receipt_callback(action, token, active))])
        if active.status == CreativeGenerationReceiptStatus.PREPARED:
            rows.append([("✏️ Изменить описание", f"cpc:new:{token}")])
    else:
        rows.append([("✨ Создать картинку", f"cpc:new:{token}")])
    rows.extend(
        [
            [("🚀 Картинка для рекламы", f"cpo:start:{token}")],
            [("🎨 Фирменный стиль", f"cpb:open:{token}")],
            [("🏠 Главная", f"cpj:home:{token}")],
        ]
    )
    return control._keyboard(rows)


def _result_rows(token: str):
    return control._keyboard(
        [
            [("✨ Создать ещё", f"cpc:new:{token}")],
            [("🚀 Перейти к рекламе", f"cpo:start:{token}")],
            [("🏠 Главная", f"cpj:home:{token}")],
        ]
    )


async def _actor_for_callback(callback: CallbackQuery, token: str):
    business_id = control._token_uuid(token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    actor.assert_can_manage_promotions()
    return actor


async def _active(actor) -> CreativeGenerationReceipt | None:
    return await asyncio.to_thread(get_active_creative_generation, actor=actor)


def _receipt_callback(
    action: str, token: str, receipt: CreativeGenerationReceipt
) -> str:
    callback = (
        f"cpc:{action}:{token}:{control._uuid_token(receipt.id)}"
    )
    if len(callback.encode("utf-8")) > 64:
        raise ValueError("creative generation callback is too long")
    return callback


async def _receipt_for_callback(actor, receipt_token: str) -> CreativeGenerationReceipt:
    receipt_id = control._token_uuid(receipt_token)
    return await asyncio.to_thread(
        get_creative_generation,
        actor=actor,
        receipt_id=receipt_id,
    )


async def send_creative_studio_menu(
    message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    actor = await control._actor(user_id, business_id)
    actor.assert_can_manage_promotions()
    active = await _active(actor)
    token = control._uuid_token(business_id)
    if active is None:
        body = (
            "🎨 Картинки и креативы\n\n"
            "Опишите картинку обычными словами. ClientPlatform использует уже "
            "подключённый генератор и сохранённый фирменный стиль бизнеса."
        )
    elif active.status == CreativeGenerationReceiptStatus.PREPARED:
        body = (
            "🎨 Картинки и креативы\n\n"
            "У Вас уже подготовлен запрос:\n"
            f"{active.request_text}\n\n"
            "Платный AI-вызов ещё не начинался. Можно продолжить или изменить описание."
        )
    elif active.delivery_claimed_at:
        body = (
            "🎨 Картинки и креативы\n\n"
            "Отправка готовой картинки уже начиналась и могла завершиться. "
            "ClientPlatform не повторит её автоматически, чтобы не прислать дубль."
        )
    else:
        body = (
            "🎨 Картинки и креативы\n\n"
            "У Вас уже есть незавершённая генерация. ClientPlatform продолжит именно "
            "её — новый платный job автоматически не создаётся."
        )
    await message.answer(body, reply_markup=_menu_rows(token, active))


@router.callback_query(F.data.startswith("cpc:open:"))
async def open_creative_studio(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).split(":", 2)[2]
    try:
        actor = await _actor_for_callback(callback, token)
    except (TypeError, ValueError, TenantPermissionDenied):
        await callback.answer(
            "Создание картинок недоступно для Вашей роли", show_alert=True
        )
        return
    await state.clear()
    await callback.answer()
    await send_creative_studio_menu(
        control._callback_message(callback),
        user_id=actor.user_id,
        business_id=actor.business_id,
    )


@router.callback_query(F.data.startswith("cpc:new:"))
async def ask_creative_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).split(":", 2)[2]
    try:
        actor = await _actor_for_callback(callback, token)
        active = await _active(actor)
    except (TypeError, ValueError, TenantPermissionDenied):
        await callback.answer(
            "Создание картинок недоступно для Вашей роли", show_alert=True
        )
        return
    if active is not None and active.status != CreativeGenerationReceiptStatus.PREPARED:
        await callback.answer("Сначала продолжите уже начатую генерацию", show_alert=True)
        return
    await state.set_state(ClientPlatformCreativeStudioState.waiting_prompt)
    await state.set_data(
        {
            "creative_business_id": actor.business_id,
            "creative_business_token": token,
        }
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        "Какую картинку создать?\n\n"
        "Напишите обычными словами, например: «спокойная реалистичная фотография "
        "кабинета психолога, светлая, без текста»."
    )


@router.message(ClientPlatformCreativeStudioState.waiting_prompt)
async def receive_creative_prompt(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        business_id = str(data["creative_business_id"])
        token = str(data["creative_business_token"])
        actor = await control._actor(control._user_id(message), business_id)
        actor.assert_can_manage_promotions()
        prompt = normalize_business_image_request(str(message.text or ""))
    except KeyError:
        await state.clear()
        await message.answer(
            "Сессия устарела. Откройте «Картинки и креативы» ещё раз."
        )
        return
    except (TypeError, ValueError, TenantPermissionDenied):
        await message.answer(
            "Опишите картинку одним сообщением до 1500 символов. "
            "Технический промпт составлять не нужно."
        )
        return
    try:
        brand = await asyncio.to_thread(load_goal_visual_brand, actor=actor)
        brand_context = brand.prompt_context()
        country_code = os.getenv("VISUAL_DEPLOYMENT_COUNTRY", "")
        provider_payload_json = freeze_business_image_payload(
            request=prompt,
            brand_context=brand_context,
            country_code=country_code,
        )
        receipt = await asyncio.to_thread(
            prepare_creative_generation,
            actor=actor,
            request_text=prompt,
            brand_context=brand_context,
            country_code=country_code,
            provider_payload_json=provider_payload_json,
        )
    except (OSError, ValueError):
        await message.answer("Не удалось безопасно подготовить генерацию. Попробуйте позже.")
        return
    await state.clear()
    if receipt.request_text != prompt:
        await message.answer(
            "У Вас уже есть незавершённая генерация. Продолжите её — новый платный "
            "job автоматически не создаётся.",
            reply_markup=_menu_rows(token, receipt),
        )
        return
    await message.answer(
        "✨ Всё готово к генерации\n\n"
        f"Задача: {receipt.request_text}\n\n"
        "Генерация может расходовать платную AI-квоту. Платный вызов начнётся "
        "только после кнопки ниже. Даже после перезапуска ClientPlatform продолжит "
        "этот же запрос, а не создаст новый платный job.",
        reply_markup=control._keyboard(
            [
                [("✅ Создать 1 картинку", _receipt_callback("generate", token, receipt))],
                [("✏️ Изменить описание", f"cpc:new:{token}")],
                [("⬅️ Не создавать", f"cpc:open:{token}")],
            ]
        ),
    )


async def _finish_image(
    callback: CallbackQuery,
    *,
    actor,
    receipt: CreativeGenerationReceipt,
    job,
) -> bool:
    if str(getattr(job, "status", "") or "") != "succeeded" or not bool(
        getattr(job, "asset_ready", False)
    ):
        return False
    target = control._callback_message(callback)
    token = control._uuid_token(actor.business_id)
    try:
        with tempfile.TemporaryDirectory(prefix="clientplatform-creative-") as directory:
            path = await asyncio.to_thread(
                materialize_ad_visual,
                job,
                output_dir=directory,
            )
            claimed = await asyncio.to_thread(
                claim_creative_generation_delivery,
                actor=actor,
                receipt_id=receipt.id,
            )
            if not claimed:
                try:
                    latest = await asyncio.to_thread(
                        get_creative_generation,
                        actor=actor,
                        receipt_id=receipt.id,
                    )
                except LookupError:
                    await target.answer(
                        "Эта картинка уже была отправлена или завершена.",
                        reply_markup=_result_rows(token),
                    )
                    return True
                await target.answer(
                    "⚠️ Отправка этой картинки уже началась и могла завершиться. "
                    "Автоматически повторять её не буду, чтобы не прислать дубль.",
                    reply_markup=_delivery_recovery_rows(token, latest, ambiguous=True),
                )
                return True
            try:
                await target.answer_photo(FSInputFile(path), caption="✅ Картинка готова")
            except TelegramAPIError:
                await target.answer(
                    "⚠️ Telegram не дал однозначного подтверждения доставки. "
                    "Автоматический повтор заблокирован, чтобы не прислать дубль.",
                    reply_markup=_delivery_recovery_rows(token, receipt, ambiguous=True),
                )
                return True
    except (OSError, VisualCreativeError):
        await target.answer(
            "Генератор завершил картинку, но файл сейчас не удалось получить. "
            "Можно проверить файл ещё раз или завершить этот результат и создать новый.",
            reply_markup=_delivery_recovery_rows(token, receipt),
        )
        return True
    await asyncio.to_thread(
        mark_creative_generation_delivered,
        actor=actor,
        receipt_id=receipt.id,
    )
    await target.answer(
        "Можно сохранить её из чата, создать ещё одну или перейти к рекламе.",
        reply_markup=_result_rows(token),
    )
    return True


async def _remember_job(actor, receipt: CreativeGenerationReceipt, job):
    return await asyncio.to_thread(
        remember_creative_generation_job,
        actor=actor,
        receipt_id=receipt.id,
        source_job_id=str(job.id),
        provider_status=str(job.status),
    )


async def _poll_existing(actor, receipt: CreativeGenerationReceipt):
    job = await asyncio.to_thread(
        poll_ad_visual,
        job_id=receipt.source_job_id,
        scope_id=actor.business_id,
    )
    current = await _remember_job(actor, receipt, job)
    return current, job


async def _submit_or_recover(actor, receipt: CreativeGenerationReceipt):
    current = await asyncio.to_thread(
        begin_creative_generation_submission,
        actor=actor,
        receipt_id=receipt.id,
    )
    if current.source_job_id:
        return await _poll_existing(actor, current)
    job = await asyncio.to_thread(
        create_business_image_from_frozen_payload,
        provider_payload_json=current.provider_payload_json,
        scope_id=actor.business_id,
        idempotency_key=current.idempotency_key,
    )
    saved = await _remember_job(actor, current, job)
    return saved, job


def _pending_rows(token: str, receipt: CreativeGenerationReceipt):
    return control._keyboard(
        [
            [("🔄 Проверить готовность", _receipt_callback("check", token, receipt))],
            [("🏠 Главная", f"cpj:home:{token}")],
        ]
    )


def _delivery_recovery_rows(
    token: str,
    receipt: CreativeGenerationReceipt,
    *,
    ambiguous: bool = False,
):
    rows: list[list[tuple[str, str]]] = []
    if ambiguous or receipt.delivery_claimed_at:
        rows.append(
            [(
                "📤 Отправить ещё раз (возможен дубль)",
                _receipt_callback("redeliver", token, receipt),
            )]
        )
    else:
        rows.append(
            [("🔄 Проверить файл ещё раз", _receipt_callback("check", token, receipt))]
        )
    rows.append(
        [(
            "🗑 Завершить и создать новую",
            _receipt_callback("abandon", token, receipt),
        )]
    )
    rows.append([("🏠 Главная", f"cpj:home:{token}")])
    return control._keyboard(rows)


async def _continue_generation(
    callback: CallbackQuery,
    *,
    actor,
    receipt: CreativeGenerationReceipt,
) -> None:
    token = control._uuid_token(actor.business_id)
    if receipt.delivery_claimed_at:
        await control._callback_message(callback).answer(
            "⚠️ Отправка этой картинки могла уже пройти. Автоматический повтор "
            "заблокирован, чтобы не прислать дубль.",
            reply_markup=_delivery_recovery_rows(token, receipt, ambiguous=True),
        )
        return
    try:
        if receipt.status in {
            CreativeGenerationReceiptStatus.PREPARED,
            CreativeGenerationReceiptStatus.SUBMITTING,
        }:
            current, job = await _submit_or_recover(actor, receipt)
        else:
            current, job = await _poll_existing(actor, receipt)
    except VisualCreativeError:
        await control._callback_message(callback).answer(
            "Не удалось получить подтверждение от генератора. Запрос сохранён: "
            "следующая проверка продолжит тот же idempotent job и не создаст новый.",
            reply_markup=_pending_rows(token, receipt),
        )
        return
    except (LookupError, ValueError):
        await control._callback_message(callback).answer(
            "Состояние генерации изменилось. Откройте «Картинки и креативы» заново."
        )
        return
    if current.status == CreativeGenerationReceiptStatus.FAILED:
        await control._callback_message(callback).answer(
            "Генерация завершилась ошибкой. Можно создать новый запрос.",
            reply_markup=_result_rows(token),
        )
        return
    if await _finish_image(callback, actor=actor, receipt=current, job=job):
        return
    if current.status == CreativeGenerationReceiptStatus.SUCCEEDED:
        await control._callback_message(callback).answer(
            "Генератор завершил задачу, но готовый файл пока недоступен. "
            "Можно проверить ещё раз или завершить этот результат и создать новый.",
            reply_markup=_delivery_recovery_rows(token, current),
        )
        return
    await control._callback_message(callback).answer(
        "⏳ Картинка ещё создаётся. Повторно платная генерация не запускается.",
        reply_markup=_pending_rows(token, current),
    )


@router.callback_query(F.data.startswith("cpc:generate:"))
async def generate_creative_image(callback: CallbackQuery, state: FSMContext) -> None:
    del state
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Это подтверждение устарело", show_alert=True)
        return
    token, receipt_token = parts[2], parts[3]
    try:
        actor = await _actor_for_callback(callback, token)
    except (TypeError, ValueError, TenantPermissionDenied):
        await callback.answer(
            "Создание картинок недоступно для Вашей роли", show_alert=True
        )
        return
    try:
        receipt = await _receipt_for_callback(actor, receipt_token)
    except (LookupError, TypeError, ValueError):
        await callback.answer(
            "Это подтверждение относится к другому или уже устаревшему запросу",
            show_alert=True,
        )
        return
    await callback.answer("Создаю картинку…")
    await _continue_generation(callback, actor=actor, receipt=receipt)


@router.callback_query(F.data.startswith("cpc:abandon:"))
async def abandon_creative_image(callback: CallbackQuery, state: FSMContext) -> None:
    del state
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Это действие устарело", show_alert=True)
        return
    token, receipt_token = parts[2], parts[3]
    try:
        actor = await _actor_for_callback(callback, token)
        receipt = await _receipt_for_callback(actor, receipt_token)
        abandoned = await asyncio.to_thread(
            abandon_creative_generation,
            actor=actor,
            receipt_id=receipt.id,
        )
    except TenantPermissionDenied:
        await callback.answer("Создание картинок недоступно для Вашей роли", show_alert=True)
        return
    except (LookupError, TypeError, ValueError):
        await callback.answer("Этот результат уже недоступен", show_alert=True)
        return
    if not abandoned:
        await callback.answer("Состояние результата уже изменилось", show_alert=True)
        return
    await callback.answer()
    await control._callback_message(callback).answer(
        "Результат завершён. Теперь можно создать новую картинку.",
        reply_markup=_result_rows(token),
    )


@router.callback_query(F.data.startswith("cpc:redeliver:"))
async def redeliver_creative_image(callback: CallbackQuery, state: FSMContext) -> None:
    del state
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Это действие устарело", show_alert=True)
        return
    token, receipt_token = parts[2], parts[3]
    try:
        actor = await _actor_for_callback(callback, token)
        receipt = await _receipt_for_callback(actor, receipt_token)
        authorized = await asyncio.to_thread(
            authorize_creative_generation_redelivery,
            actor=actor,
            receipt_id=receipt.id,
        )
    except TenantPermissionDenied:
        await callback.answer("Создание картинок недоступно для Вашей роли", show_alert=True)
        return
    except (LookupError, TypeError, ValueError):
        await callback.answer("Этот результат уже недоступен", show_alert=True)
        return
    if not authorized:
        await callback.answer(
            "Повторная отправка уже запущена или больше не нужна",
            show_alert=True,
        )
        return
    try:
        current = await _receipt_for_callback(actor, receipt_token)
    except LookupError:
        await callback.answer("Этот результат уже завершён", show_alert=True)
        return
    await callback.answer("Повторяю отправку по Вашему запросу…")
    await _continue_generation(callback, actor=actor, receipt=current)


@router.callback_query(F.data.startswith("cpc:check:"))
async def check_creative_image(callback: CallbackQuery, state: FSMContext) -> None:
    del state
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Эта проверка устарела", show_alert=True)
        return
    token, receipt_token = parts[2], parts[3]
    try:
        actor = await _actor_for_callback(callback, token)
    except (TypeError, ValueError, TenantPermissionDenied):
        await callback.answer(
            "Создание картинок недоступно для Вашей роли", show_alert=True
        )
        return
    try:
        receipt = await _receipt_for_callback(actor, receipt_token)
    except (LookupError, TypeError, ValueError):
        await callback.answer("Незавершённая генерация не найдена", show_alert=True)
        return
    await callback.answer("Проверяю готовность…")
    await _continue_generation(callback, actor=actor, receipt=receipt)


def _allowed(actor, check) -> bool:
    try:
        check()
    except TenantPermissionDenied:
        return False
    return True


def install_creative_studio_visibility(one_click: ModuleType) -> None:
    if bool(getattr(one_click, "_creative_studio_visibility_installed", False)):
        return
    original_more_rows = one_click._more_rows
    original_content_rows = one_click._content_tools_rows

    def more_rows(token: str, actor):
        rows = list(original_more_rows(token, actor))
        if _allowed(actor, actor.assert_can_manage_promotions):
            creative_row = [(nav.CREATIVES.label, f"cpc:open:{token}")]
            if creative_row not in rows:
                content_index = next(
                    (
                        index
                        for index, row in enumerate(rows)
                        if row and row[0][0] == nav.CONTENT_PROMOTION.label
                    ),
                    max(0, len(rows) - 2),
                )
                rows.insert(content_index, creative_row)
        return rows

    def content_tools_rows(token: str, actor):
        rows, help_lines = original_content_rows(token, actor)
        rows = [list(row) for row in rows]
        help_lines = list(help_lines)
        if _allowed(actor, actor.assert_can_manage_promotions):
            creative_button = (nav.CREATIVES.label, f"cpc:open:{token}")
            if not any(creative_button in row for row in rows):
                publication_index = next(
                    (
                        index
                        for index, row in enumerate(rows)
                        if row and str(row[0][1]).endswith(":publications")
                    ),
                    None,
                )
                if publication_index is None:
                    rows.insert(0, [creative_button])
                else:
                    rows[publication_index].insert(0, creative_button)
            help_line = (
                f"• создать изображение для поста или рекламы → «{nav.CREATIVES.label}»"
            )
            if help_line not in help_lines:
                help_lines.insert(0, help_line)
        return rows, help_lines

    one_click._more_rows = more_rows
    one_click._content_tools_rows = content_tools_rows
    one_click._creative_studio_visibility_installed = True


def _extend_tuple(module: ModuleType, name: str, *values: str) -> None:
    current = tuple(getattr(module, name))
    setattr(module, name, tuple(dict.fromkeys((*current, *values))))


def install_creative_studio_safety(safety: ModuleType) -> None:
    if bool(getattr(safety, "_creative_studio_safety_installed", False)):
        return
    _extend_tuple(safety, "_CLIENTPLATFORM_CALLBACK_PREFIXES", "cpc:")
    _extend_tuple(safety, "_STATE_ESCAPE_PREFIXES", "cpc:open:", "cpc:new:")
    _extend_tuple(
        safety,
        "_REPEATABLE_NAVIGATION_PREFIXES",
        "cpc:open:",
        "cpc:new:",
        "cpc:check:",
    )
    _extend_tuple(
        safety,
        "_ONE_SHOT_PREFIXES",
        "cpc:generate:",
        "cpc:abandon:",
        "cpc:redeliver:",
    )

    original_escape = cast(
        Callable[[str, str], bool],
        getattr(safety, "_callback_can_escape_state"),
    )

    def callback_can_escape_state(current_state: str, callback_data: str) -> bool:
        if current_state.startswith("ClientPlatformCreativeStudioState:"):
            if callback_data.startswith(("cpc:open:", "cpc:new:", "cpj:home:")):
                return True
        return original_escape(current_state, callback_data)

    setattr(safety, "_callback_can_escape_state", callback_can_escape_state)
    setattr(safety, "_creative_studio_safety_installed", True)


__all__ = [
    "ClientPlatformCreativeStudioState",
    "install_creative_studio_safety",
    "install_creative_studio_visibility",
    "router",
    "send_creative_studio_menu",
]
