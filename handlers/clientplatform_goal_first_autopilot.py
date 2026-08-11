from __future__ import annotations

"""Goal-first owner UX over the canonical one-click orchestration.

The default path asks for an outcome, not technical advertising objects. Owners
who want control can open one optional customization screen for their own copy,
image or video. Paid generation and real advertising spend remain explicit.
"""

import asyncio
import hashlib
import os
from decimal import Decimal
from io import BytesIO
from types import ModuleType

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from clientplatform.application.ad_goal_autopilot import preview_goal_spend
from clientplatform.application.ad_publication_assets import (
    attach_image_bytes,
    attach_image_file,
    attach_video_bytes,
    remove_asset,
)
from clientplatform.application.ad_publication_customization import (
    update_ad_publication_copy,
)
from clientplatform.application.ad_spend_operations import ad_spend_mutations_enabled
from clientplatform.application.visual_creatives import (
    VisualCreativeError,
    create_ad_visual,
    materialize_ad_visual,
    poll_ad_visual,
)
from clientplatform.domain.ad_connections import AdConnectionError
from clientplatform.domain.ad_publication_assets import (
    AdPublicationAssetError,
    AdPublicationAssetSource,
)
from clientplatform.domain.ad_spend import AdSpendError
from clientplatform.domain.promotions import PromotionChannel, PromotionError
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.integrations.yandex_direct import YandexDirectError

from . import clientplatform_control as control
from . import clientplatform_one_click_experience as one_click


router = Router(name="clientplatform_goal_first_autopilot")
_MAX_TELEGRAM_MEDIA_BYTES = 20_000_000


class GoalFirstAutopilotState(StatesGroup):
    ready = State()
    customizing = State()
    waiting_text = State()
    waiting_image = State()
    waiting_video = State()
    confirming_generation = State()
    generation_pending = State()
    confirming_launch = State()


def _money(minor: int, currency: str) -> str:
    amount = Decimal(int(minor)) / Decimal(100)
    rendered = f"{amount:.2f}".replace(".", ",")
    return f"{rendered} {currency}"


def _goal_keyboard(business_id: str):
    token = control._uuid_token(business_id)
    return control._keyboard(
        [
            [("🚀 Получить клиентов", f"cpo:start:{token}")],
            [
                ("👥 Клиенты и запись", f"cpj:bookings:{token}"),
                ("⚙️ Ещё", f"cpo:more:{token}"),
            ],
        ]
    )


async def send_goal_dashboard(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    _actor, access, profile, _caps, customers, programs, slots = (
        await one_click.simple._business_snapshot(
            user_id=user_id,
            business_id=business_id,
        )
    )
    open_count = sum(
        item.slot.status == one_click.BookingSlotStatus.OPEN for item in slots
    )
    readiness = (
        f"Свободных времён: {open_count}."
        if open_count
        else "Свободных времён пока нет — если понадобится, я попрошу добавить одно."
    )
    await message.answer(
        f"🏠 {access.business.name}\n\n"
        f"{profile.activity_description}\n\n"
        f"{readiness}\n"
        f"Клиентов: {len(customers)} · материалов и программ: {len(programs)}\n\n"
        "Главное действие — «🚀 Получить клиентов». ClientPlatform сама выберет "
        "ближайшее свободное время, подходящий рекламный путь и сохранённые "
        "настройки. Технические кабинеты и кампании знать не нужно.\n\n"
        "Если захотите, перед запуском можно заменить текст и добавить свою "
        "картинку или видео. Действия с возможными расходами подтверждаются отдельно.",
        reply_markup=_goal_keyboard(business_id),
    )


def _custom_keyboard(business_token: str):
    return control._keyboard(
        [
            [("✍️ Свой текст", f"cpo:custom-text:{business_token}")],
            [
                ("🖼 Своя картинка", f"cpo:custom-image:{business_token}"),
                ("🎬 Своё видео", f"cpo:custom-video:{business_token}"),
            ],
            [("✨ Сделать картинку автоматически", f"cpo:genask:{business_token}")],
            [("🧹 Без картинки и видео", f"cpo:custom-clear:{business_token}")],
            [("✅ Готово", f"cpo:custom-done:{business_token}")],
            [("🧰 Другие настройки", f"cpo:ads:{business_token}")],
        ]
    )


def _launch_label(data: dict) -> str:
    if not ad_spend_mutations_enabled():
        return "🚀 Подготовить в Яндексе"
    hard = data.get("preview_hard_cap_minor")
    currency = str(data.get("preview_currency") or "").strip()
    if hard not in {None, ""} and currency:
        return f"🚀 Запустить · максимум {_money(int(hard), currency)}"
    return "🚀 Проверить и запустить"


def _result_keyboard(business_token: str, data: dict):
    return control._keyboard(
        [
            [(_launch_label(data), f"cpo:launch:{business_token}")],
            [("🎨 Настроить под себя", f"cpo:custom:{business_token}")],
            [("🏠 Не запускать", f"cpj:home:{business_token}")],
        ]
    )


def _state_matches(data: dict, business_token: str) -> bool:
    return str(data.get("business_token") or "") == str(business_token or "")


async def _prepare_goal_result(
    event: CallbackQuery | Message,
    state: FSMContext,
    *,
    data: dict,
    region_ids: tuple[int, ...],
) -> None:
    """Prepare the local draft and, when possible, the exact safe launch cap."""

    actor = await control._actor(
        one_click._user_id(event),
        str(data["business_id"]),
    )
    try:
        promotion = await asyncio.to_thread(
            one_click.create_slot_promotion,
            actor=actor,
            slot_id=str(data["slot_id"]),
            channel=PromotionChannel.WEBSITE,
        )
    except (PromotionError, TenantPermissionDenied):
        await one_click._draft_failure(
            event,
            state,
            business_token=str(data["business_token"]),
        )
        return

    try:
        source_url = one_click._link(
            await one_click._username(event),
            promotion.campaign.source_token,
        )
    except (RuntimeError, ValueError):
        await one_click._draft_failure(
            event,
            state,
            business_token=str(data["business_token"]),
        )
        return

    try:
        draft = await asyncio.to_thread(
            one_click.create_ad_publication_draft,
            actor=actor,
            promotion_campaign_id=promotion.campaign.id,
            connection_id=str(data["connection_id"]),
            external_campaign_id=str(data["external_campaign_id"]),
            external_campaign_name=str(data["external_campaign_name"]),
            region_ids=region_ids,
            source_url=source_url,
        )
    except (AdConnectionError, TenantPermissionDenied):
        await one_click._draft_failure(
            event,
            state,
            business_token=str(data["business_token"]),
        )
        return

    next_data = {
        **data,
        "promotion_campaign_id": promotion.campaign.id,
        "source_url": source_url,
        "job_id": draft.job.id,
        "creative_title": draft.job.title,
        "creative_body": draft.job.text,
        "creative_job_id": "",
    }
    if ad_spend_mutations_enabled():
        try:
            preview = await asyncio.to_thread(
                preview_goal_spend,
                actor=actor,
                connection_id=str(data["connection_id"]),
                external_campaign_id=str(data["external_campaign_id"]),
            )
        except (
            AdConnectionError,
            AdSpendError,
            TenantPermissionDenied,
            YandexDirectError,
            RuntimeError,
            ValueError,
        ):
            preview = None
        if preview is not None:
            next_data.update(
                {
                    "preview_currency": preview.currency,
                    "preview_hard_cap_minor": preview.recommended_hard_cap_minor,
                    "preview_daily_cap_minor": preview.recommended_daily_cap_minor,
                }
            )

    await state.set_state(GoalFirstAutopilotState.ready)
    await state.set_data(next_data)
    launch_hint = (
        f"Нажатие «{_launch_label(next_data)}» — это единственное подтверждение, "
        "после которого могут начаться рекламные расходы."
        if ad_spend_mutations_enabled()
        else "Пока запуск расходов отключён защитным переключателем; черновик можно подготовить без списаний."
    )
    await one_click._target(event).answer(
        "✅ Реклама подготовлена — всё готово\n\n"
        "ClientPlatform сама выбрала ближайшее свободное время и подходящие "
        "сохранённые настройки.\n\n"
        f"{draft.job.title}\n\n{draft.job.text}\n\n"
        "Если всё устраивает — больше технических шагов нет. Если хотите свой "
        "текст, картинку или видео, откройте «Настроить под себя».\n\n"
        f"{launch_hint}",
        reply_markup=_result_keyboard(str(data["business_token"]), next_data),
    )


async def _choose_goal_region(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    data: dict,
    campaign_id: str,
    campaign_name: str,
) -> None:
    actor = await control._actor(
        int(callback.from_user.id),
        str(data["business_id"]),
    )
    try:
        jobs = await asyncio.to_thread(one_click.list_ad_publications, actor=actor)
    except (AdConnectionError, TenantPermissionDenied):
        jobs = []

    saved = one_click._recent(
        jobs,
        connection_id=str(data["connection_id"]),
        campaign_id=campaign_id,
    )
    next_data = {
        **data,
        "external_campaign_id": campaign_id,
        "external_campaign_name": campaign_name,
    }
    regions = tuple(getattr(saved, "region_ids", ()) or ()) if saved else ()
    if regions:
        await _prepare_goal_result(
            callback,
            state,
            data=next_data,
            region_ids=regions,
        )
        return

    await state.set_state(one_click.OneClickOwnerState.waiting_region)
    await state.set_data(next_data)
    await control._callback_message(callback).answer(
        "Осталось только указать регион: где искать клиентов? Это нужно спросить "
        "только в первый раз — дальше ClientPlatform запомнит выбор.",
        reply_markup=control._keyboard(
            [
                [("Нижний Новгород", "cpo:region:47"), ("Москва", "cpo:region:213")],
                [("Санкт-Петербург", "cpo:region:2")],
                [("Другой город", "cpo:region:other")],
                [("🏠 Не сейчас", f"cpj:home:{data['business_token']}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpo:custom:"))
async def open_customization(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not _state_matches(data, business_token) or not data.get("job_id"):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    await state.set_state(GoalFirstAutopilotState.customizing)
    await callback.answer()
    await control._callback_message(callback).answer(
        "🎨 Настроить под себя\n\n"
        "Это необязательно. Можно заменить только то, что хотите; остальное "
        "ClientPlatform оставит готовым. Свои файлы я сама подготовлю и прикреплю "
        "к объявлению.",
        reply_markup=_custom_keyboard(business_token),
    )


@router.callback_query(F.data.startswith("cpo:custom-text:"))
async def ask_custom_text(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    await state.set_state(GoalFirstAutopilotState.waiting_text)
    await callback.answer()
    await control._callback_message(callback).answer(
        "Отправьте свой рекламный текст одним сообщением:\n\n"
        "первая строка — заголовок;\n"
        "со второй строки — основной текст.\n\n"
        "Например:\nКонсультация для родителей\nПомогу спокойно разобрать сложную ситуацию."
    )


@router.message(GoalFirstAutopilotState.waiting_text)
async def receive_custom_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw = str(message.text or "").strip()
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) < 2:
        await message.answer(
            "Нужно две части: первая строка — заголовок, дальше — текст объявления."
        )
        return
    title, body = lines[0], " ".join(lines[1:])
    try:
        actor = await control._actor(control._user_id(message), str(data["business_id"]))
        updated = await asyncio.to_thread(
            update_ad_publication_copy,
            actor=actor,
            publication_job_id=str(data["job_id"]),
            title=title,
            text=body,
        )
    except (KeyError, ValueError, AdConnectionError, TenantPermissionDenied):
        await message.answer(
            "Не получилось сохранить текст. Заголовок — до 56 символов, основной "
            "текст — до 81. Попробуйте ещё раз."
        )
        return
    await state.update_data(creative_title=updated.title, creative_body=updated.text)
    await state.set_state(GoalFirstAutopilotState.customizing)
    await message.answer(
        "✅ Свой текст поставил. Больше ничего делать с ним не нужно.",
        reply_markup=_custom_keyboard(str(data["business_token"])),
    )


@router.callback_query(F.data.startswith("cpo:custom-image:"))
async def ask_custom_image(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    await state.set_state(GoalFirstAutopilotState.waiting_image)
    await callback.answer()
    await control._callback_message(callback).answer(
        "Пришлите картинку сюда как фото или файл. Я сама приведу её к формату "
        "Яндекс Директа и прикреплю к объявлению."
    )


async def _download_telegram_file(message: Message, *, file_id: str, reported_size: int) -> bytes:
    if reported_size > _MAX_TELEGRAM_MEDIA_BYTES:
        raise AdPublicationAssetError("telegram media size is unsupported")
    telegram_file = await message.bot.get_file(file_id)
    remote_size = int(getattr(telegram_file, "file_size", 0) or 0)
    if remote_size > _MAX_TELEGRAM_MEDIA_BYTES:
        raise AdPublicationAssetError("telegram media size is unsupported")
    file_path = str(getattr(telegram_file, "file_path", "") or "").strip()
    if not file_path:
        raise AdPublicationAssetError("telegram media path is unavailable")
    target = BytesIO()
    await message.bot.download_file(file_path, destination=target, timeout=30)
    payload = target.getvalue()
    if not payload or len(payload) > _MAX_TELEGRAM_MEDIA_BYTES:
        raise AdPublicationAssetError("telegram media download is invalid")
    if reported_size > 0 and len(payload) != reported_size and remote_size in {0, reported_size}:
        raise AdPublicationAssetError("telegram media size changed")
    return payload


@router.message(GoalFirstAutopilotState.waiting_image)
async def receive_custom_image(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    file_id = ""
    size = 0
    name = "image.jpg"
    if message.photo:
        selected = message.photo[-1]
        file_id = str(selected.file_id)
        size = int(selected.file_size or 0)
    elif message.document and str(message.document.mime_type or "").startswith("image/"):
        file_id = str(message.document.file_id)
        size = int(message.document.file_size or 0)
        name = str(message.document.file_name or "image.jpg")
    if not file_id:
        await message.answer("Пришлите именно изображение — как фото или графический файл.")
        return
    try:
        payload = await _download_telegram_file(message, file_id=file_id, reported_size=size)
        actor = await control._actor(control._user_id(message), str(data["business_id"]))
        await asyncio.to_thread(
            attach_image_bytes,
            actor=actor,
            publication_job_id=str(data["job_id"]),
            payload=payload,
            source=AdPublicationAssetSource.UPLOAD,
            original_name=name,
        )
    except (
        KeyError,
        ValueError,
        OSError,
        asyncio.TimeoutError,
        AdPublicationAssetError,
        TenantPermissionDenied,
    ):
        await message.answer(
            "Не удалось принять картинку. Пришлите JPG, PNG или обычное фото размером до 20 МБ."
        )
        return
    await state.set_state(GoalFirstAutopilotState.customizing)
    await message.answer(
        "✅ Картинку добавил. Я сама подготовлю и прикреплю её к объявлению — "
        "скачивать или загружать её в Яндекс вручную не понадобится.",
        reply_markup=_custom_keyboard(str(data["business_token"])),
    )


@router.callback_query(F.data.startswith("cpo:custom-video:"))
async def ask_custom_video(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    await state.set_state(GoalFirstAutopilotState.waiting_video)
    await callback.answer()
    await control._callback_message(callback).answer(
        "Пришлите видео сюда обычным видеофайлом. Для Яндекс Директа длительность "
        "должна быть от 5 до 60 секунд. После загрузки ClientPlatform сама дождётся "
        "обработки Яндексом и прикрепит ролик."
    )


@router.message(GoalFirstAutopilotState.waiting_video)
async def receive_custom_video(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    video = message.video
    if video is None:
        await message.answer("Пришлите ролик именно как видео, чтобы я увидела его длительность.")
        return
    try:
        payload = await _download_telegram_file(
            message,
            file_id=str(video.file_id),
            reported_size=int(video.file_size or 0),
        )
        actor = await control._actor(control._user_id(message), str(data["business_id"]))
        await asyncio.to_thread(
            attach_video_bytes,
            actor=actor,
            publication_job_id=str(data["job_id"]),
            payload=payload,
            content_type=str(video.mime_type or "video/mp4"),
            original_name=str(video.file_name or "video.mp4"),
            duration_seconds=int(video.duration or 0),
            source=AdPublicationAssetSource.UPLOAD,
        )
    except (
        KeyError,
        ValueError,
        OSError,
        asyncio.TimeoutError,
        AdPublicationAssetError,
        TenantPermissionDenied,
    ):
        await message.answer(
            "Не удалось принять видео. Нужен поддерживаемый ролик 5–60 секунд "
            "размером до 20 МБ."
        )
        return
    await state.set_state(GoalFirstAutopilotState.customizing)
    await message.answer(
        "✅ Видео добавил. Дальше всё автоматически: ClientPlatform загрузит его "
        "в Яндекс, дождётся конвертации и прикрепит к объявлению.",
        reply_markup=_custom_keyboard(str(data["business_token"])),
    )


@router.callback_query(F.data.startswith("cpo:custom-clear:"))
async def clear_custom_media(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    try:
        actor = await control._actor(int(callback.from_user.id), str(data["business_id"]))
        await asyncio.to_thread(
            remove_asset,
            actor=actor,
            publication_job_id=str(data["job_id"]),
        )
    except (KeyError, ValueError, AdPublicationAssetError, TenantPermissionDenied):
        await callback.answer("Не удалось убрать медиа", show_alert=True)
        return
    await callback.answer("Медиа убрано")
    await state.set_state(GoalFirstAutopilotState.customizing)
    await control._callback_message(callback).answer(
        "✅ Оставил объявление без картинки и видео.",
        reply_markup=_custom_keyboard(business_token),
    )


@router.callback_query(F.data.startswith("cpo:genask:"))
async def ask_generated_image_confirmation(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    await state.set_state(GoalFirstAutopilotState.confirming_generation)
    await callback.answer()
    await control._callback_message(callback).answer(
        "✨ Сделать картинку автоматически?\n\n"
        "Это отдельная генерация через подключённый AI-провайдер и она может "
        "расходовать платную квоту. Будет создана ровно одна картинка; после "
        "готовности ClientPlatform сама прикрепит её к объявлению.",
        reply_markup=control._keyboard(
            [
                [("✅ Создать 1 картинку", f"cpo:gen:{business_token}")],
                [("⬅️ Не создавать", f"cpo:custom:{business_token}")],
            ]
        ),
    )


async def _finish_generated_image(
    event: CallbackQuery,
    state: FSMContext,
    *,
    job: object,
    data: dict,
) -> bool:
    status = str(getattr(job, "status", "") or "")
    if status != "succeeded" or not bool(getattr(job, "asset_ready", False)):
        return False
    try:
        path = await asyncio.to_thread(materialize_ad_visual, job)
        actor = await control._actor(int(event.from_user.id), str(data["business_id"]))
        await asyncio.to_thread(
            attach_image_file,
            actor=actor,
            publication_job_id=str(data["job_id"]),
            path=path,
            source=AdPublicationAssetSource.GENERATED,
        )
    except (KeyError, OSError, ValueError, VisualCreativeError, AdPublicationAssetError):
        return False
    await state.update_data(creative_job_id="")
    await state.set_state(GoalFirstAutopilotState.customizing)
    await control._callback_message(event).answer_photo(
        FSInputFile(path),
        caption="✅ Картинка готова и уже привязана к рекламному черновику ClientPlatform.",
    )
    await control._callback_message(event).answer(
        "В Яндекс вручную её загружать не нужно.",
        reply_markup=_custom_keyboard(str(data["business_token"])),
    )
    return True


@router.callback_query(F.data.startswith("cpo:gen:"))
async def generate_custom_image(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    await callback.answer("Создаю картинку…")
    try:
        business_id = str(data["business_id"])
        publication_job_id = str(data["job_id"])
        copy_digest = hashlib.sha256(
            (
                str(data.get("creative_title") or "")
                + "\n"
                + str(data.get("creative_body") or "")
            ).encode("utf-8")
        ).hexdigest()
        idempotency_key = "clientplatform:" + hashlib.sha256(
            f"{business_id}|{publication_job_id}|image|{copy_digest}".encode("utf-8")
        ).hexdigest()
        job = await asyncio.to_thread(
            create_ad_visual,
            title=str(data.get("creative_title") or ""),
            body=str(data.get("creative_body") or ""),
            kind="image",
            scope_id=business_id,
            idempotency_key=idempotency_key,
            country_code=os.getenv("VISUAL_DEPLOYMENT_COUNTRY", ""),
            wait_seconds=20,
        )
    except (KeyError, ValueError, VisualCreativeError):
        await control._callback_message(callback).answer(
            "Не удалось создать картинку. Повторная платная генерация автоматически не запускается.",
            reply_markup=_custom_keyboard(business_token),
        )
        await state.set_state(GoalFirstAutopilotState.customizing)
        return
    if await _finish_generated_image(callback, state, job=job, data=data):
        return
    job_id = str(getattr(job, "job_id", "") or getattr(job, "id", "") or "")
    if not job_id:
        await state.set_state(GoalFirstAutopilotState.customizing)
        await control._callback_message(callback).answer(
            "Генератор не вернул результат. Можно продолжить без картинки.",
            reply_markup=_custom_keyboard(business_token),
        )
        return
    await state.update_data(creative_job_id=job_id)
    await state.set_state(GoalFirstAutopilotState.generation_pending)
    await control._callback_message(callback).answer(
        "⏳ Картинка ещё создаётся. Ничего загружать заново не нужно.",
        reply_markup=control._keyboard(
            [[("🔄 Проверить готовность", f"cpo:gencheck:{business_token}")]]
        ),
    )


@router.callback_query(F.data.startswith("cpo:gencheck:"))
async def check_generated_image(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    try:
        job = await asyncio.to_thread(
            poll_ad_visual,
            job_id=str(data["creative_job_id"]),
            scope_id=str(data["business_id"]),
        )
    except (KeyError, VisualCreativeError):
        await callback.answer("Пока не удалось проверить картинку", show_alert=True)
        return
    await callback.answer()
    if await _finish_generated_image(callback, state, job=job, data=data):
        return
    if str(getattr(job, "status", "") or "") in {"queued", "running"}:
        await control._callback_message(callback).answer(
            "⏳ Ещё создаётся. Повторно генерация не запускается.",
            reply_markup=control._keyboard(
                [[("🔄 Проверить готовность", f"cpo:gencheck:{business_token}")]]
            ),
        )
        return
    await state.update_data(creative_job_id="")
    await state.set_state(GoalFirstAutopilotState.customizing)
    await control._callback_message(callback).answer(
        "Генерация не удалась. Можно загрузить свою картинку или продолжить без неё.",
        reply_markup=_custom_keyboard(business_token),
    )


@router.callback_query(F.data.startswith("cpo:custom-done:"))
async def finish_customization(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    await state.set_state(GoalFirstAutopilotState.ready)
    await callback.answer("Готово")
    await control._callback_message(callback).answer(
        "✅ Изменения сохранены. Дальше ClientPlatform всё сделает сама.",
        reply_markup=_result_keyboard(business_token, data),
    )


def install_goal_first_autopilot(
    *,
    owner_module: ModuleType,
    simple_module: ModuleType,
    control_module: ModuleType,
) -> None:
    if bool(getattr(owner_module, "_goal_first_autopilot_installed", False)):
        return

    one_click._home_keyboard = _goal_keyboard
    one_click._prepare_draft = _prepare_goal_result
    one_click._choose_campaign = _choose_goal_region

    owner_module._owner_keyboard = _goal_keyboard
    owner_module.send_owner_dashboard = send_goal_dashboard
    simple_module.send_simple_dashboard = send_goal_dashboard
    control_module._send_dashboard = send_goal_dashboard
    if not bool(getattr(simple_module, "_goal_first_autopilot_composed", False)):
        simple_module.router.include_router(router)
        simple_module._goal_first_autopilot_composed = True
    owner_module._goal_first_autopilot_installed = True


__all__ = [
    "GoalFirstAutopilotState",
    "install_goal_first_autopilot",
    "router",
    "send_goal_dashboard",
]
