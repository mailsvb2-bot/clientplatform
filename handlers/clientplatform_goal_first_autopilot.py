from __future__ import annotations

"""Goal-first owner UX over the canonical one-click orchestration.

The default path asks for an outcome, not technical advertising objects. Owners
who want control can open one optional customization screen for their own copy,
image or video. Paid generation and real advertising spend remain explicit.
"""

import asyncio
import hashlib
import logging
import os
import secrets
import tempfile
from decimal import Decimal
from io import BytesIO
from types import ModuleType

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from clientplatform.application.ad_goal_autopilot import preview_goal_spend
from clientplatform.application.ad_publication_assets import (
    attach_image_bytes,
    attach_image_file,
    attach_video_bytes,
    list_reusable_images,
    remove_asset,
    reuse_image_reference,
)
from clientplatform.application.ad_publication_customization import (
    update_ad_publication_copy,
)
from clientplatform.application.ad_spend_operations import ad_spend_mutations_enabled
from clientplatform.application.editable_advertising import (
    EditableAdvertisingError,
    EditableAdvertisingSourceExpired,
    advance_failed_editable_ad_source,
    bind_editable_ad_source,
    create_editable_ad_project,
    finish_editable_ad_project,
    get_editable_ad_project,
    prepare_editable_ad_source,
    render_editable_ad_project,
    update_editable_ad_composition,
)
from clientplatform.application.creative_studio_publication import (
    CreativeStudioPublicationError,
    GoalStudioPublicationResult,
    build_goal_image_variants,
    goal_variant_labels,
    load_goal_visual_brand,
    poll_goal_image_variant,
    render_format_for_placement,
    selected_goal_variant,
    start_goal_image_variant,
)
from clientplatform.application.visual_creatives import (
    VisualCreativeError,
    create_ad_visual,
    materialize_ad_visual,
    poll_ad_visual,
    visual_generation_ready,
)
from clientplatform.domain.ad_connections import AdConnectionError
from clientplatform.domain.ad_publication_assets import (
    AdPublicationAssetError,
    AdPublicationAssetSource,
)
from clientplatform.domain.ad_spend import AdSpendError
from clientplatform.domain.editable_advertising import (
    EDITABLE_AD_FONT_LABELS_RU,
    EDITABLE_AD_FONT_PRESETS,
    EditableAdProjectStatus,
)
from clientplatform.domain.promotions import PromotionChannel, PromotionError
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.integrations.yandex_direct import YandexDirectError
from services.visual_creative_gateway import VisualCreativeGatewayError, download_render_asset
from clientplatform.presentation.visual_generation import (
    visual_failure_message,
    visual_provider_unavailable_message,
)

from . import clientplatform_control as control
from . import clientplatform_one_click_experience as one_click


router = Router(name="clientplatform_goal_first_autopilot")
log = logging.getLogger(__name__)
_MAX_TELEGRAM_MEDIA_BYTES = 20_000_000
_GENERATED_VIDEO_DURATION_SECONDS = 8


class GoalFirstAutopilotState(StatesGroup):
    ready = State()
    customizing = State()
    waiting_text = State()
    waiting_image = State()
    waiting_video = State()
    waiting_editable_headline = State()
    waiting_editable_body = State()
    waiting_editable_cta = State()
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
        "Главное действие — «🚀 Получить клиентов». ClientPlatform проведёт "
        "через выбор услуги и нужного времени, затем использует подходящий рекламный "
        "путь и сохранённые настройки. Технические кабинеты и кампании знать не нужно.\n\n"
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
            [("🗂 Ранее использованная картинка", f"cpo:reuse-image:{business_token}")],
            [
                ("✨ AI-картинка", f"cpo:genask:{business_token}"),
                ("🎬 AI-видео", f"cpo:genvideoask:{business_token}"),
            ],
            [(
                "картинка для рекламы (возможность редактирования)",
                f"cpo:editask:image:{business_token}",
            )],
            [(
                "видео для рекламы (возможность редактирования)",
                f"cpo:editask:video:{business_token}",
            )],
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
            [
                ("🖼 Создать картинку", f"cpo:genask:{business_token}"),
                ("🎬 Создать видео", f"cpo:genvideoask:{business_token}"),
            ],
            [(
                "картинка для рекламы (возможность редактирования)",
                f"cpo:editask:image:{business_token}",
            )],
            [(
                "видео для рекламы (возможность редактирования)",
                f"cpo:editask:video:{business_token}",
            )],
            [(_launch_label(data), f"cpo:launch:{business_token}")],
            [("🎨 Настроить под себя", f"cpo:custom:{business_token}")],
            [("🏠 Не запускать", f"cpj:home:{business_token}")],
        ]
    )


def _editable_keyboard(business_token: str, font_preset: str = "auto"):
    label = EDITABLE_AD_FONT_LABELS_RU.get(
        str(font_preset or "auto"),
        EDITABLE_AD_FONT_LABELS_RU["auto"],
    )
    return control._keyboard(
        [
            [
                ("✍️ Заголовок", f"cpo:editfield:headline:{business_token}"),
                ("📝 Текст", f"cpo:editfield:body:{business_token}"),
            ],
            [("🔘 CTA", f"cpo:editfield:cta:{business_token}")],
            [("🔤 Шрифт: " + label, f"cpo:editfont:{business_token}")],
            [("↕️ Переместить текстовый блок", f"cpo:editlayout:{business_token}")],
            [("🔄 Обновить превью", f"cpo:editpreview:{business_token}")],
            [("✅ Завершить редактирование", f"cpo:editdone:{business_token}")],
            [("↩️ Назад к настройкам", f"cpo:custom:{business_token}")],
        ]
    )


def _editable_font_keyboard(business_token: str, current: str):
    rows: list[list[tuple[str, str]]] = []
    pairs = (
        ("auto", "modern"),
        ("strict", "friendly"),
        ("premium", "editorial"),
        ("elegant", "bold_ad"),
    )
    for left, right in pairs:
        row: list[tuple[str, str]] = []
        for preset in (left, right):
            marker = "✓ " if preset == current else ""
            row.append(
                (
                    marker + EDITABLE_AD_FONT_LABELS_RU[preset],
                    f"cpo:editfontset:{preset}:{business_token}",
                )
            )
        rows.append(row)
    rows.append([("⬅️ К редактору", f"cpo:editpreview:{business_token}")])
    return control._keyboard(rows)


def _editable_source_keyboard(kind: str, business_token: str):
    noun = "видео" if kind == "video" else "картинку"
    return control._keyboard(
        [
            [("✅ Создать AI-основу: " + noun, f"cpo:editgen:{kind}:{business_token}")],
            [("⬅️ Не создавать", f"cpo:custom:{business_token}")],
        ]
    )


def _editable_format(_kind: str) -> str:
    # Canonical Yandex Direct media bridge currently owns a square asset.
    return render_format_for_placement("yandex_direct")


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
        source_url = one_click._acquisition_link(
            promotion.campaign.source_token
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
        "Если всё устраивает — больше технических шагов нет. Картинку или видео "
        "можно создать сразу кнопками ниже; свой текст и свои файлы доступны через "
        "«Настроить под себя».\n\n"
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


@router.callback_query(F.data.startswith("cpo:reuse-image:"))
async def choose_reusable_image(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not _state_matches(data, business_token) or not data.get("job_id"):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    try:
        actor = await control._actor(
            int(callback.from_user.id),
            str(data["business_id"]),
        )
        reusable = await asyncio.to_thread(
            list_reusable_images,
            actor=actor,
            publication_job_id=str(data["job_id"]),
            limit=6,
        )
    except (KeyError, AdPublicationAssetError, TenantPermissionDenied):
        await callback.answer("Не удалось открыть прошлые картинки", show_alert=True)
        return
    if not reusable:
        await callback.answer()
        await control._callback_message(callback).answer(
            "Ранее загруженных картинок для этого рекламного кабинета пока нет. "
            "Можно добавить свою или создать новую AI-картинку.",
            reply_markup=_custom_keyboard(business_token),
        )
        return
    reuse_token = secrets.token_hex(3)
    await state.update_data(
        reusable_image_job_ids=[item.publication_job_id for item in reusable],
        reusable_image_token=reuse_token,
    )
    rows = []
    for index, item in enumerate(reusable):
        origin = "AI" if item.source == AdPublicationAssetSource.GENERATED else "своя"
        date = str(item.updated_at or "")[:10]
        name = str(item.original_name or "картинка").strip()
        if len(name) > 20:
            name = name[:17] + "..."
        rows.append(
            [(f"🖼 {origin} · {date} · {name}", f"cpo:reusepick:{reuse_token}:{index}:{business_token}")]
        )
    rows.append([("↩️ Назад", f"cpo:custom:{business_token}")])
    await callback.answer()
    await control._callback_message(callback).answer(
        "Выберите ранее использованную картинку. ClientPlatform возьмёт уже "
        "существующий provider reference — повторной загрузки и хранения файла не будет.",
        reply_markup=control._keyboard(rows),
    )


@router.callback_query(F.data.startswith("cpo:reusepick:"))
async def apply_reusable_image(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":", 4)
    if len(parts) != 5:
        await callback.answer("Картинка больше не доступна", show_alert=True)
        return
    _, _, callback_token, raw_index, business_token = parts
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    if callback_token != str(data.get("reusable_image_token") or ""):
        await callback.answer("Список картинок устарел. Откройте его заново.", show_alert=True)
        return
    try:
        index = int(raw_index)
        reusable_ids = list(data.get("reusable_image_job_ids") or [])
        source_job_id = str(reusable_ids[index])
        actor = await control._actor(
            int(callback.from_user.id),
            str(data["business_id"]),
        )
        await asyncio.to_thread(
            reuse_image_reference,
            actor=actor,
            source_publication_job_id=source_job_id,
            target_publication_job_id=str(data["job_id"]),
        )
    except (IndexError, KeyError, ValueError):
        await callback.answer("Картинка больше не доступна", show_alert=True)
        return
    except (AdPublicationAssetError, TenantPermissionDenied):
        await callback.answer("Картинка больше не доступна", show_alert=True)
        return
    await state.update_data(reusable_image_job_ids=[], reusable_image_token="")
    await state.set_state(GoalFirstAutopilotState.customizing)
    await callback.answer("Картинка выбрана")
    await control._callback_message(callback).answer(
        "✅ Ранее использованная картинка привязана к новому рекламному черновику "
        "через существующий provider reference. Файл повторно не загружался и на "
        "сервере ClientPlatform не сохранялся.",
        reply_markup=_custom_keyboard(business_token),
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
        AdConnectionError,
        YandexDirectError,
        RuntimeError,
        TenantPermissionDenied,
    ):
        await message.answer(
            "Не удалось принять картинку. Пришлите JPG, PNG или обычное фото размером до 20 МБ."
        )
        return
    await state.set_state(GoalFirstAutopilotState.customizing)
    await message.answer(
        "✅ Картинка уже загружена в Яндекс Директ и привязана к этому "
        "рекламному черновику через provider reference — загружать её в Яндекс "
        "вручную не понадобится. Постоянную копию файла ClientPlatform не хранит.",
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
        "✅ Видео уже передано в Яндекс. ClientPlatform хранит только provider "
        "reference, дождётся конвертации и прикрепит ролик к объявлению без "
        "постоянной серверной копии.",
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


def _studio_country() -> str:
    return str(os.getenv("VISUAL_DEPLOYMENT_COUNTRY", "") or "").strip().upper()


def _studio_variant(data: dict, index: int):
    return selected_goal_variant(
        business_id=str(data["business_id"]),
        publication_job_id=str(data["job_id"]),
        title=str(data.get("creative_title") or ""),
        body=str(data.get("creative_body") or ""),
        country_code=_studio_country(),
        index=index,
    )


async def _studio_variant_for_actor(data: dict, index: int, *, user_id: int):
    actor = await control._actor(user_id, str(data["business_id"]))
    brand = await asyncio.to_thread(load_goal_visual_brand, actor=actor)
    return selected_goal_variant(
        business_id=str(data["business_id"]),
        publication_job_id=str(data["job_id"]),
        title=str(data.get("creative_title") or ""),
        body=str(data.get("creative_body") or ""),
        country_code=_studio_country(),
        index=index,
        brand=brand,
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
        "расходовать платную квоту. Можно создать ровно одну картинку сразу или "
        "сначала бесплатно выбрать одну из трёх концепций Creative Studio. "
        "Платная генерация начнётся только после явного выбора.",
        reply_markup=control._keyboard(
            [
                [("✅ Создать 1 картинку", f"cpo:gen:{business_token}")],
                [("🎨 Выбрать из 3 концепций", f"cpo:genstudio:{business_token}")],
                [("⬅️ Не создавать", f"cpo:custom:{business_token}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpo:genvideoask:"))
async def ask_generated_video_confirmation(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    await state.update_data(creative_generation_kind="video")
    await state.set_state(GoalFirstAutopilotState.confirming_generation)
    await callback.answer()
    await control._callback_message(callback).answer(
        "🎬 Создать короткое видео автоматически?\n\n"
        "Это отдельная генерация через подключённый AI-провайдер и она может "
        "расходовать платную квоту. ClientPlatform запустит ровно один "
        "идемпотентный запрос только после явного подтверждения.",
        reply_markup=control._keyboard(
            [
                [("✅ Создать 1 видео", f"cpo:genvideo:{business_token}")],
                [("⬅️ Не создавать", f"cpo:custom:{business_token}")],
            ]
        ),
    )


async def _preview_editable_project(
    target: Message,
    *,
    actor,
    data: dict,
    project_id: str,
    kind: str,
) -> None:
    project, pack = await asyncio.to_thread(
        render_editable_ad_project,
        actor=actor,
        project_id=project_id,
        formats=(_editable_format(kind),),
    )
    del project
    with tempfile.TemporaryDirectory(prefix="clientplatform-editable-preview-") as directory:
        path = await asyncio.to_thread(
            download_render_asset,
            pack,
            _editable_format(kind),
            output_dir=directory,
        )
        if kind == "video":
            await target.answer_video(
                FSInputFile(path),
                caption="👁 Превью редактируемого рекламного видео",
                supports_streaming=True,
            )
        else:
            await target.answer_photo(
                FSInputFile(path),
                caption="👁 Превью редактируемой рекламной картинки",
            )


async def _show_editable_editor(
    target: Message,
    *,
    actor,
    data: dict,
    project_id: str,
    kind: str,
) -> None:
    font_preset = str(data.get("editable_font_preset") or "auto")
    try:
        await _preview_editable_project(
            target,
            actor=actor,
            data=data,
            project_id=project_id,
            kind=kind,
        )
        note = (
            "Это превью. Правки текста, CTA, шрифта и положения блока не запускают новую "
            "AI-генерацию и пока не меняют asset в рекламном provider. "
            "Нажмите «Завершить редактирование», когда макет готов."
        )
    except EditableAdvertisingSourceExpired:
        note = (
            "AI-основа уже исчезла из временного хранилища. Текст и настройки "
            "редактора сохранены; новая платная генерация автоматически не запускается."
        )
        await target.answer(
            note,
            reply_markup=_editable_source_keyboard(kind, str(data["business_token"])),
        )
        return
    except (EditableAdvertisingError, VisualCreativeGatewayError, OSError):
        note = (
            "Редактор и все правки сохранены, но превью сейчас не удалось пересобрать. "
            "Новая AI-генерация не запускалась."
        )
    except ValueError:
        note = (
            "Редактор и все правки сохранены, но превью сейчас не удалось пересобрать. "
            "Новая AI-генерация не запускалась."
        )
    await target.answer(
        note,
        reply_markup=_editable_keyboard(str(data["business_token"]), font_preset),
    )


@router.callback_query(F.data.startswith("cpo:editask:"))
async def ask_editable_ad_confirmation(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, kind, business_token = str(callback.data).split(":", 3)
    except ValueError:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    if kind not in {"image", "video"}:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    data = await state.get_data()
    if not _state_matches(data, business_token) or not data.get("job_id"):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    try:
        actor = await control._actor(int(callback.from_user.id), str(data.get("business_id") or ""))
        brand = await asyncio.to_thread(load_goal_visual_brand, actor=actor)
        project = await asyncio.to_thread(
            create_editable_ad_project,
            actor=actor,
            publication_job_id=str(data.get("job_id") or ""),
            kind=kind,
            headline=str(data.get("creative_title") or ""),
            body=str(data.get("creative_body") or ""),
            brand=brand.render_brand(),
            font_preset=str(getattr(brand, "font_preset", "auto") or "auto"),
        )
    except (LookupError, ValueError, TenantPermissionDenied):
        await callback.answer("Не удалось открыть редактор рекламы", show_alert=True)
        return

    await state.update_data(
        editable_ad_project_id=project.id,
        editable_ad_kind=kind,
        editable_generation_active=False,
        editable_source_revision=project.revision,
        editable_font_preset=project.font_preset,
        creative_variant_id="",
        creative_variant_index="",
    )
    target = control._callback_message(callback)
    if (
        project.status in {
            EditableAdProjectStatus.SOURCE_READY,
            EditableAdProjectStatus.FINISHED,
        }
        and project.source_job_id
    ):
        await state.set_state(GoalFirstAutopilotState.customizing)
        await callback.answer()
        await _show_editable_editor(
            target,
            actor=actor,
            data={
                **data,
                "business_token": business_token,
                "editable_font_preset": project.font_preset,
            },
            project_id=project.id,
            kind=kind,
        )
        return

    await state.set_state(GoalFirstAutopilotState.confirming_generation)
    await callback.answer()
    expired = project.status == EditableAdProjectStatus.SOURCE_EXPIRED
    noun = "видео" if kind == "video" else "картинки"
    prefix = (
        "Предыдущая AI-основа уже удалена из временного хранилища. "
        "Ваши правки текста и макета сохранены.\n\n"
        if expired
        else ""
    )
    await target.answer(
        prefix
        + f"Для редактируемой рекламы сначала нужна AI-основа {noun}. "
        "Это отдельный платный AI-вызов. После него заголовок, текст, CTA, шрифт и "
        "положение блока можно менять сколько угодно без новой AI-генерации.\n\n"
        "Сами пользовательские image/video bytes ClientPlatform постоянно не хранит.",
        reply_markup=_editable_source_keyboard(kind, business_token),
    )


@router.callback_query(F.data.startswith("cpo:editgen:"))
async def generate_editable_ad_source(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, kind, business_token = str(callback.data).split(":", 3)
    except ValueError:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    data = await state.get_data()
    project_id = str(data.get("editable_ad_project_id") or "").strip()
    if (
        kind not in {"image", "video"}
        or not project_id
        or not _state_matches(data, business_token)
    ):
        await callback.answer("Редактируемый макет уже недоступен", show_alert=True)
        return
    try:
        actor = await control._actor(int(callback.from_user.id), str(data.get("business_id") or ""))
        project = await asyncio.to_thread(
            get_editable_ad_project,
            actor=actor,
            project_id=project_id,
        )
        if project.kind != kind or project.publication_job_id != str(data.get("job_id") or ""):
            raise ValueError("editable_ad_project_mismatch")
        if (
            project.status in {
                EditableAdProjectStatus.SOURCE_READY,
                EditableAdProjectStatus.FINISHED,
            }
            and project.source_job_id
        ):
            await state.set_state(GoalFirstAutopilotState.customizing)
            await callback.answer("AI-основа уже готова")
            await _show_editable_editor(
                control._callback_message(callback),
                actor=actor,
                data=data,
                project_id=project.id,
                kind=kind,
            )
            return
        project = await asyncio.to_thread(
            prepare_editable_ad_source,
            actor=actor,
            project_id=project.id,
        )
    except (LookupError, ValueError, TenantPermissionDenied):
        await callback.answer("Не удалось подготовить AI-основу", show_alert=True)
        return

    await state.update_data(
        editable_generation_active=True,
        editable_source_revision=project.revision,
        editable_ad_kind=kind,
    )
    await _generate_custom_visual(
        callback,
        state,
        business_token=business_token,
        kind=kind,
        editable_project_id=project.id,
        editable_revision=project.revision,
        source_title=project.headline,
        source_body=project.body,
    )


async def _finish_editable_source_generation(
    event: CallbackQuery,
    state: FSMContext,
    *,
    data: dict,
    kind: str,
    project_id: str,
    job: object,
) -> bool:
    if str(getattr(job, "status", "") or "").strip().lower() != "succeeded":
        return False
    source_job_id = str(
        getattr(job, "id", "") or getattr(job, "job_id", "") or ""
    ).strip()
    if not source_job_id:
        return False
    try:
        actor = await control._actor(int(event.from_user.id), str(data.get("business_id") or ""))
        project = await asyncio.to_thread(
            bind_editable_ad_source,
            actor=actor,
            project_id=project_id,
            source_job_id=source_job_id,
        )
    except (LookupError, ValueError, TenantPermissionDenied):
        return False

    await state.update_data(
        creative_job_id="",
        creative_generation_kind=kind,
        editable_generation_active=False,
        editable_source_revision=project.revision,
        editable_font_preset=project.font_preset,
    )
    await state.set_state(GoalFirstAutopilotState.customizing)
    await _show_editable_editor(
        control._callback_message(event),
        actor=actor,
        data={**data, "editable_font_preset": project.font_preset},
        project_id=project.id,
        kind=kind,
    )
    return True


@router.callback_query(F.data.startswith("cpo:editfield:"))
async def ask_editable_field(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, field, business_token = str(callback.data).split(":", 3)
    except ValueError:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    data = await state.get_data()
    if (
        field not in {"headline", "body", "cta"}
        or not _state_matches(data, business_token)
        or not str(data.get("editable_ad_project_id") or "").strip()
    ):
        await callback.answer("Редактируемый макет уже недоступен", show_alert=True)
        return
    target_state = {
        "headline": GoalFirstAutopilotState.waiting_editable_headline,
        "body": GoalFirstAutopilotState.waiting_editable_body,
        "cta": GoalFirstAutopilotState.waiting_editable_cta,
    }[field]
    await state.set_state(target_state)
    await callback.answer()
    labels = {
        "headline": "новый заголовок",
        "body": "новый основной текст",
        "cta": "новую надпись CTA",
    }
    await control._callback_message(callback).answer(
        f"Отправьте {labels[field]} одним сообщением."
    )


async def _receive_editable_field(
    message: Message,
    state: FSMContext,
    *,
    field: str,
) -> None:
    data = await state.get_data()
    project_id = str(data.get("editable_ad_project_id") or "").strip()
    if not project_id:
        await state.set_state(GoalFirstAutopilotState.customizing)
        await message.answer("Редактируемый макет уже недоступен.")
        return
    try:
        actor = await control._actor(control._user_id(message), str(data.get("business_id") or ""))
        project = await asyncio.to_thread(
            update_editable_ad_composition,
            actor=actor,
            project_id=project_id,
            **{field: str(message.text or "")},
        )
    except (LookupError, ValueError, TenantPermissionDenied):
        await message.answer("Не удалось сохранить правку. Проверьте длину текста.")
        return
    await state.update_data(editable_source_revision=project.revision)
    await state.set_state(GoalFirstAutopilotState.customizing)
    await _show_editable_editor(
        message,
        actor=actor,
        data=data,
        project_id=project.id,
        kind=project.kind,
    )


@router.message(GoalFirstAutopilotState.waiting_editable_headline)
async def receive_editable_headline(message: Message, state: FSMContext) -> None:
    await _receive_editable_field(message, state, field="headline")


@router.message(GoalFirstAutopilotState.waiting_editable_body)
async def receive_editable_body(message: Message, state: FSMContext) -> None:
    await _receive_editable_field(message, state, field="body")


@router.message(GoalFirstAutopilotState.waiting_editable_cta)
async def receive_editable_cta(message: Message, state: FSMContext) -> None:
    await _receive_editable_field(message, state, field="cta")


@router.callback_query(F.data.startswith("cpo:editfont:"))
async def choose_editable_font(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    project_id = str(data.get("editable_ad_project_id") or "").strip()
    if not project_id or not _state_matches(data, business_token):
        await callback.answer("Редактируемый макет уже недоступен", show_alert=True)
        return
    try:
        actor = await control._actor(
            int(callback.from_user.id),
            str(data.get("business_id") or ""),
        )
        project = await asyncio.to_thread(
            get_editable_ad_project,
            actor=actor,
            project_id=project_id,
        )
    except (LookupError, ValueError, TenantPermissionDenied):
        await callback.answer("Не удалось открыть выбор шрифта", show_alert=True)
        return
    await callback.answer()
    await control._callback_message(callback).answer(
        "Выберите типографику. «Автоматически» подбирает вариант по объёму "
        "заголовка и текста. Шрифт накладывает ClientPlatform после AI-генерации, "
        "поэтому смена шрифта не расходует новую AI-квоту.",
        reply_markup=_editable_font_keyboard(business_token, project.font_preset),
    )


@router.callback_query(F.data.startswith("cpo:editfontset:"))
async def set_editable_font(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, font_preset, business_token = str(callback.data).split(":", 3)
    except ValueError:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    data = await state.get_data()
    project_id = str(data.get("editable_ad_project_id") or "").strip()
    if (
        font_preset not in EDITABLE_AD_FONT_PRESETS
        or not project_id
        or not _state_matches(data, business_token)
    ):
        await callback.answer("Редактируемый макет уже недоступен", show_alert=True)
        return
    try:
        actor = await control._actor(
            int(callback.from_user.id),
            str(data.get("business_id") or ""),
        )
        project = await asyncio.to_thread(
            update_editable_ad_composition,
            actor=actor,
            project_id=project_id,
            font_preset=font_preset,
        )
    except (LookupError, ValueError, TenantPermissionDenied):
        await callback.answer("Не удалось изменить шрифт", show_alert=True)
        return
    await state.update_data(
        editable_source_revision=project.revision,
        editable_font_preset=project.font_preset,
    )
    await state.set_state(GoalFirstAutopilotState.customizing)
    await callback.answer("Шрифт изменён")
    await _show_editable_editor(
        control._callback_message(callback),
        actor=actor,
        data={**data, "editable_font_preset": project.font_preset},
        project_id=project.id,
        kind=project.kind,
    )


@router.callback_query(F.data.startswith("cpo:editlayout:"))
async def toggle_editable_layout(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    project_id = str(data.get("editable_ad_project_id") or "").strip()
    if not project_id or not _state_matches(data, business_token):
        await callback.answer("Редактируемый макет уже недоступен", show_alert=True)
        return
    try:
        actor = await control._actor(int(callback.from_user.id), str(data.get("business_id") or ""))
        current = await asyncio.to_thread(
            get_editable_ad_project,
            actor=actor,
            project_id=project_id,
        )
        next_layout = "top_card" if current.layout == "lower_card" else "lower_card"
        project = await asyncio.to_thread(
            update_editable_ad_composition,
            actor=actor,
            project_id=project_id,
            layout=next_layout,
        )
    except (LookupError, ValueError, TenantPermissionDenied):
        await callback.answer("Не удалось переместить текстовый блок", show_alert=True)
        return
    await state.update_data(
        editable_source_revision=project.revision,
        editable_font_preset=project.font_preset,
    )
    await state.set_state(GoalFirstAutopilotState.customizing)
    await callback.answer("Положение блока изменено")
    await _show_editable_editor(
        control._callback_message(callback),
        actor=actor,
        data={**data, "editable_font_preset": project.font_preset},
        project_id=project.id,
        kind=project.kind,
    )


@router.callback_query(F.data.startswith("cpo:editpreview:"))
async def refresh_editable_preview(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    project_id = str(data.get("editable_ad_project_id") or "").strip()
    if not project_id or not _state_matches(data, business_token):
        await callback.answer("Редактируемый макет уже недоступен", show_alert=True)
        return
    try:
        actor = await control._actor(int(callback.from_user.id), str(data.get("business_id") or ""))
        project = await asyncio.to_thread(
            get_editable_ad_project,
            actor=actor,
            project_id=project_id,
        )
    except (LookupError, ValueError, TenantPermissionDenied):
        await callback.answer("Редактируемый макет уже недоступен", show_alert=True)
        return
    await callback.answer("Обновляю превью…")
    await state.update_data(editable_font_preset=project.font_preset)
    await _show_editable_editor(
        control._callback_message(callback),
        actor=actor,
        data={**data, "editable_font_preset": project.font_preset},
        project_id=project.id,
        kind=project.kind,
    )


@router.callback_query(F.data.startswith("cpo:editdone:"))
async def finish_editable_ad(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    project_id = str(data.get("editable_ad_project_id") or "").strip()
    if not project_id or not _state_matches(data, business_token):
        await callback.answer("Редактируемый макет уже недоступен", show_alert=True)
        return
    try:
        actor = await control._actor(int(callback.from_user.id), str(data.get("business_id") or ""))
        project, pack = await asyncio.to_thread(
            render_editable_ad_project,
            actor=actor,
            project_id=project_id,
            formats=(_editable_format(str(data.get("editable_ad_kind") or "image")),),
        )
        with tempfile.TemporaryDirectory(prefix="clientplatform-editable-final-") as directory:
            path = await asyncio.to_thread(
                download_render_asset,
                pack,
                _editable_format(project.kind),
                output_dir=directory,
            )
            if project.kind == "video":
                payload = await asyncio.to_thread(path.read_bytes)
                await asyncio.to_thread(
                    attach_video_bytes,
                    actor=actor,
                    publication_job_id=str(data.get("job_id") or ""),
                    payload=payload,
                    content_type="video/mp4",
                    original_name=path.name or "editable-ad.mp4",
                    duration_seconds=_GENERATED_VIDEO_DURATION_SECONDS,
                    source=AdPublicationAssetSource.GENERATED,
                )
            else:
                await asyncio.to_thread(
                    attach_image_file,
                    actor=actor,
                    publication_job_id=str(data.get("job_id") or ""),
                    path=path,
                    source=AdPublicationAssetSource.GENERATED,
                )
            # Provider attachment is the commit boundary. Owner delivery is a
            # best-effort copy of the exact committed render and must not turn a
            # successful provider upload into a retryable provider operation.
            try:
                target = control._callback_message(callback)
                if project.kind == "video":
                    await target.answer_video(
                        FSInputFile(path),
                        caption="✅ Редактируемое рекламное видео готово",
                        supports_streaming=True,
                    )
                else:
                    await target.answer_photo(
                        FSInputFile(path),
                        caption="✅ Редактируемая рекламная картинка готова",
                    )
            except TelegramAPIError:
                log.warning(
                    "Editable ad provider commit succeeded but Telegram owner delivery failed "
                    "business_id=%s project_id=%s kind=%s",
                    str(data.get("business_id") or ""),
                    project.id,
                    project.kind,
                    exc_info=True,
                )
        finished = await asyncio.to_thread(
            finish_editable_ad_project,
            actor=actor,
            project_id=project.id,
        )
    except EditableAdvertisingSourceExpired:
        await callback.answer()
        await control._callback_message(callback).answer(
            "AI-основа уже удалена из временного хранилища. Ваши правки сохранены; "
            "новую AI-основу можно создать только отдельным подтверждением.",
            reply_markup=_editable_source_keyboard(
                str(data.get("editable_ad_kind") or "image"),
                business_token,
            ),
        )
        return
    except AdPublicationAssetError as exc:
        await callback.answer()
        ambiguous = "ambiguous" in str(exc).lower()
        text = (
            "Яндекс мог принять файл, но подтверждение загрузки неоднозначно. "
            "ClientPlatform не повторяет upload автоматически, чтобы не создать дубль."
            if ambiguous
            else "Не удалось подтвердить загрузку готового макета в рекламный provider."
        )
        await control._callback_message(callback).answer(
            text,
            reply_markup=_editable_keyboard(business_token),
        )
        return
    except (
        KeyError,
        LookupError,
        OSError,
        ValueError,
        EditableAdvertisingError,
        VisualCreativeGatewayError,
        TenantPermissionDenied,
    ):
        await callback.answer("Не удалось завершить редактирование", show_alert=True)
        return

    await state.update_data(
        editable_ad_project_id="",
        editable_ad_kind="",
        editable_generation_active=False,
        editable_source_revision=finished.revision,
    )
    await state.set_state(GoalFirstAutopilotState.customizing)
    await callback.answer("Готово")
    await control._callback_message(callback).answer(
        "✅ Редактируемый рекламный макет передан в рекламный provider. "
        "ClientPlatform хранит только проект и provider reference — постоянной "
        "копии image/video bytes на сервере нет.",
        reply_markup=_custom_keyboard(business_token),
    )


@router.callback_query(F.data.startswith("cpo:genstudio:"))
async def open_generated_image_studio(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    try:
        actor = await control._actor(int(callback.from_user.id), str(data["business_id"]))
        brand = await asyncio.to_thread(load_goal_visual_brand, actor=actor)
        variants = build_goal_image_variants(
            business_id=str(data["business_id"]),
            publication_job_id=str(data["job_id"]),
            title=str(data.get("creative_title") or ""),
            body=str(data.get("creative_body") or ""),
            country_code=_studio_country(),
            brand=brand,
        )
        labels = goal_variant_labels(variants)
    except (KeyError, TypeError, ValueError):
        await callback.answer("Не удалось подготовить варианты", show_alert=True)
        return
    await state.update_data(
        creative_experiment_id=variants[0].experiment_id,
        creative_variant_id="",
        creative_variant_index="",
    )
    await state.set_state(GoalFirstAutopilotState.confirming_generation)
    await callback.answer()
    await control._callback_message(callback).answer(
        "✨ Выберите направление картинки\n\n"
        "Три концепции подготовлены без платного AI-вызова. Платная генерация "
        "запустится только после выбора одного варианта и останется идемпотентной "
        "при повторной проверке. После готовности ClientPlatform сама создаст "
        "форматы и прикрепит квадратный рекламный asset к черновику.",
        reply_markup=control._keyboard(
            [
                [(labels[index], f"cpo:genvariant:{index}:{business_token}")]
                for index in range(len(labels))
            ]
            + [[("⬅️ Назад", f"cpo:genask:{business_token}")]],
        ),
    )

async def _finish_generated_visual(
    event: CallbackQuery,
    state: FSMContext,
    *,
    data: dict,
    kind: str,
    result: GoalStudioPublicationResult | None = None,
    job: object | None = None,
) -> bool:
    visual_kind = "video" if str(kind or "").strip().lower() == "video" else "image"
    if result is not None:
        if not result.attached or result.asset is None:
            return False
        await state.update_data(
            creative_job_id="",
            creative_generation_kind="image",
            creative_experiment_id=result.binding.experiment_id,
            creative_variant_id=result.binding.variant_id,
        )
        await state.set_state(GoalFirstAutopilotState.customizing)
        preview_available = False
        if result.render is not None:
            try:
                with tempfile.TemporaryDirectory(prefix="clientplatform-preview-") as directory:
                    preview = await asyncio.to_thread(
                        download_render_asset,
                        result.render,
                        render_format_for_placement("yandex_direct"),
                        output_dir=directory,
                    )
                    await control._callback_message(event).answer_photo(
                        FSInputFile(preview),
                        caption="✅ Картинка готова и загружена в рекламный provider.",
                    )
                    preview_available = True
            except (OSError, VisualCreativeGatewayError):
                preview_available = False
        preview_note = (
            ""
            if preview_available
            else " Превью сейчас не удалось повторно получить, но рекламный provider уже подтвердил картинку."
        )
        await control._callback_message(event).answer(
            "Квадратный формат уже передан в Yandex Direct по provider reference. "
            "Постоянную копию картинки ClientPlatform не хранит." + preview_note,
            reply_markup=_custom_keyboard(str(data["business_token"])),
        )
        return True

    if job is None:
        return False
    status = str(getattr(job, "status", "") or "")
    if status != "succeeded" or not bool(getattr(job, "asset_ready", False)):
        return False
    try:
        path = await asyncio.to_thread(materialize_ad_visual, job)
        actor = await control._actor(int(event.from_user.id), str(data["business_id"]))
        if visual_kind == "video":
            payload = await asyncio.to_thread(path.read_bytes)
            await asyncio.to_thread(
                attach_video_bytes,
                actor=actor,
                publication_job_id=str(data["job_id"]),
                payload=payload,
                content_type=str(getattr(job, "mime_type", "") or "video/mp4"),
                original_name=path.name or "generated.mp4",
                duration_seconds=_GENERATED_VIDEO_DURATION_SECONDS,
                source=AdPublicationAssetSource.GENERATED,
            )
        else:
            await asyncio.to_thread(
                attach_image_file,
                actor=actor,
                publication_job_id=str(data["job_id"]),
                path=path,
                source=AdPublicationAssetSource.GENERATED,
            )
    except (KeyError, OSError, ValueError):
        return False
    except (VisualCreativeError, AdPublicationAssetError):
        return False
    await state.update_data(
        creative_job_id="",
        creative_generation_kind=visual_kind,
    )
    await state.set_state(GoalFirstAutopilotState.customizing)
    if visual_kind == "video":
        await control._callback_message(event).answer_video(
            FSInputFile(path),
            caption="✅ Видео готово и передано в рекламный provider.",
        )
        follow_up = "В Яндекс вручную его загружать не нужно; локальной постоянной копии нет."
    else:
        await control._callback_message(event).answer_photo(
            FSInputFile(path),
            caption="✅ Картинка готова и загружена в рекламный provider.",
        )
        follow_up = "В Яндекс вручную её загружать не нужно; локальной постоянной копии нет."
    await control._callback_message(event).answer(
        follow_up,
        reply_markup=_custom_keyboard(str(data["business_token"])),
    )
    return True


async def _finish_generated_image(
    event: CallbackQuery,
    state: FSMContext,
    *,
    data: dict,
    result: GoalStudioPublicationResult | None = None,
    job: object | None = None,
) -> bool:
    """Compatibility wrapper for the image-only Creative Studio variant path."""

    return await _finish_generated_visual(
        event,
        state,
        data=data,
        kind="image",
        result=result,
        job=job,
    )


async def _generate_studio_variant(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    business_token: str,
    index: int,
) -> None:
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    await callback.answer("Создаю выбранный вариант…")
    try:
        variant = await _studio_variant_for_actor(
            data, index, user_id=int(callback.from_user.id)
        )
        actor = await control._actor(int(callback.from_user.id), str(data["business_id"]))
        result = await asyncio.to_thread(
            start_goal_image_variant,
            actor=actor,
            publication_job_id=str(data["job_id"]),
            variant=variant,
            wait_seconds=20,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        CreativeStudioPublicationError,
        TenantPermissionDenied,
    ):
        await control._callback_message(callback).answer(
            "Не удалось создать выбранный вариант. Повторный запрос использует "
            "тот же idempotency key и не должен автоматически создавать второй платный job.",
            reply_markup=_custom_keyboard(business_token),
        )
        await state.set_state(GoalFirstAutopilotState.customizing)
        return
    await state.update_data(
        creative_variant_index=index,
        creative_variant_id=variant.variant_id,
        creative_experiment_id=variant.experiment_id,
    )
    if await _finish_generated_image(callback, state, result=result, data=data):
        return
    status = result.binding.status.value
    if status not in {"generating", "rendering"}:
        await state.update_data(creative_job_id="")
        await state.set_state(GoalFirstAutopilotState.customizing)
        await control._callback_message(callback).answer(
            "Генерация или подготовка форматов завершилась ошибкой. Можно выбрать "
            "другой вариант, загрузить свою картинку или продолжить без неё.",
            reply_markup=_custom_keyboard(business_token),
        )
        return
    await state.update_data(creative_job_id=result.job.id)
    await state.set_state(GoalFirstAutopilotState.generation_pending)
    await control._callback_message(callback).answer(
        "⏳ Вариант ещё создаётся. Повторно генерация не запускается.",
        reply_markup=control._keyboard(
            [[("🔄 Проверить готовность", f"cpo:gencheck:{business_token}")]]
        ),
    )


@router.callback_query(F.data.startswith("cpo:genvariant:"))
async def generate_selected_studio_image(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _prefix, _action, raw_index, business_token = str(callback.data).split(":", 3)
        index = int(raw_index)
        if index not in {0, 1, 2}:
            raise ValueError("variant index")
    except (TypeError, ValueError):
        await callback.answer("Вариант больше не найден", show_alert=True)
        return
    await _generate_studio_variant(
        callback,
        state,
        business_token=business_token,
        index=index,
    )


async def _generate_custom_visual(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    business_token: str,
    kind: str,
    editable_project_id: str = "",
    editable_revision: int = 0,
    source_title: str | None = None,
    source_body: str | None = None,
) -> None:
    visual_kind = "video" if str(kind or "").strip().lower() == "video" else "image"
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    object_noun = "видео" if visual_kind == "video" else "картинку"
    subject_noun = "Видео" if visual_kind == "video" else "Картинка"
    without_noun = "видео" if visual_kind == "video" else "картинки"
    await callback.answer(f"Создаю {object_noun}…")
    try:
        business_id = str(data["business_id"])
        publication_job_id = str(data["job_id"])
        country_code = os.getenv("VISUAL_DEPLOYMENT_COUNTRY", "")
        ready = await asyncio.to_thread(
            visual_generation_ready,
            kind=visual_kind,
            country_code=country_code,
        )
        if not ready:
            if editable_project_id:
                await state.update_data(editable_generation_active=False)
            await control._callback_message(callback).answer(
                visual_provider_unavailable_message(visual_kind),
                reply_markup=_custom_keyboard(business_token),
            )
            await state.set_state(GoalFirstAutopilotState.customizing)
            return
        source_title_value = (
            str(data.get("creative_title") or "")
            if source_title is None
            else str(source_title)
        )
        source_body_value = (
            str(data.get("creative_body") or "")
            if source_body is None
            else str(source_body)
        )
        copy_digest = hashlib.sha256(
            (source_title_value + "\n" + source_body_value).encode("utf-8")
        ).hexdigest()
        if editable_project_id:
            if editable_revision < 1:
                raise ValueError("editable_ad_source_revision_missing")
            identity = (
                f"{business_id}|{publication_job_id}|editable|{editable_project_id}|"
                f"{editable_revision}|{visual_kind}|{copy_digest}"
            )
        else:
            identity = f"{business_id}|{publication_job_id}|{visual_kind}|{copy_digest}"
        idempotency_key = "clientplatform:" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()
        job = await asyncio.to_thread(
            create_ad_visual,
            title=source_title_value,
            body=source_body_value,
            kind=visual_kind,
            scope_id=business_id,
            idempotency_key=idempotency_key,
            country_code=country_code,
            wait_seconds=20,
        )
    except (KeyError, ValueError, VisualCreativeError):
        if editable_project_id:
            await state.update_data(editable_generation_active=False)
        await control._callback_message(callback).answer(
            "Не удалось проверить или запустить генератор. "
            "Повторная платная генерация автоматически не запускается.",
            reply_markup=_custom_keyboard(business_token),
        )
        await state.set_state(GoalFirstAutopilotState.customizing)
        return
    if editable_project_id and await _finish_editable_source_generation(
        callback,
        state,
        job=job,
        data=data,
        kind=visual_kind,
        project_id=editable_project_id,
    ):
        return
    if not editable_project_id and await _finish_generated_visual(
        callback,
        state,
        job=job,
        data=data,
        kind=visual_kind,
    ):
        return
    if str(getattr(job, "status", "") or "").strip().lower() == "failed":
        if editable_project_id:
            try:
                actor = await control._actor(
                    int(callback.from_user.id),
                    str(data.get("business_id") or ""),
                )
                await asyncio.to_thread(
                    advance_failed_editable_ad_source,
                    actor=actor,
                    project_id=editable_project_id,
                )
            except (LookupError, ValueError, TenantPermissionDenied):
                log.warning(
                    "Failed to advance editable source revision after definitive provider failure "
                    "business_id=%s project_id=%s",
                    str(data.get("business_id") or ""),
                    editable_project_id,
                    exc_info=True,
                )
            await state.update_data(editable_generation_active=False)
        await state.update_data(creative_job_id="", creative_generation_kind=visual_kind)
        await state.set_state(GoalFirstAutopilotState.customizing)
        await control._callback_message(callback).answer(
            visual_failure_message(job),
            reply_markup=_custom_keyboard(business_token),
        )
        return
    job_id = str(getattr(job, "job_id", "") or getattr(job, "id", "") or "")
    if not job_id:
        if editable_project_id:
            await state.update_data(editable_generation_active=False)
        await state.set_state(GoalFirstAutopilotState.customizing)
        await control._callback_message(callback).answer(
            f"Генератор не вернул результат. Можно продолжить без {without_noun}.",
            reply_markup=_custom_keyboard(business_token),
        )
        return
    await state.update_data(
        creative_job_id=job_id,
        creative_generation_kind=visual_kind,
        editable_generation_active=bool(editable_project_id),
        editable_ad_project_id=editable_project_id
        or str(data.get("editable_ad_project_id") or ""),
        editable_source_revision=editable_revision
        or int(data.get("editable_source_revision") or 0),
    )
    await state.set_state(GoalFirstAutopilotState.generation_pending)
    await control._callback_message(callback).answer(
        f"⏳ {subject_noun} ещё создаётся. Ничего загружать заново не нужно.",
        reply_markup=control._keyboard(
            [[("🔄 Проверить готовность", f"cpo:gencheck:{business_token}")]]
        ),
    )


@router.callback_query(F.data.startswith("cpo:gen:"))
async def generate_custom_image(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    await state.update_data(editable_generation_active=False)
    await _generate_custom_visual(
        callback,
        state,
        business_token=business_token,
        kind="image",
    )


@router.callback_query(F.data.startswith("cpo:genvideo:"))
async def generate_custom_video(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    await state.update_data(editable_generation_active=False)
    await _generate_custom_visual(
        callback,
        state,
        business_token=business_token,
        kind="video",
    )


async def _check_studio_generated_image(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    business_token: str,
    data: dict,
) -> None:
    try:
        index = int(data.get("creative_variant_index") or 0)
        variant = await _studio_variant_for_actor(
            data, index, user_id=int(callback.from_user.id)
        )
        actor = await control._actor(int(callback.from_user.id), str(data["business_id"]))
        result = await asyncio.to_thread(
            poll_goal_image_variant,
            actor=actor,
            publication_job_id=str(data["job_id"]),
            job_id=str(data["creative_job_id"]),
            variant=variant,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        CreativeStudioPublicationError,
        TenantPermissionDenied,
    ):
        await callback.answer("Пока не удалось проверить картинку", show_alert=True)
        return
    await callback.answer()
    if await _finish_generated_image(callback, state, result=result, data=data):
        return
    if result.binding.status.value in {"generating", "rendering"}:
        await control._callback_message(callback).answer(
            "⏳ Ещё создаётся. Повторно платная генерация не запускается.",
            reply_markup=control._keyboard(
                [[("🔄 Проверить готовность", f"cpo:gencheck:{business_token}")]]
            ),
        )
        return
    await state.update_data(creative_job_id="")
    await state.set_state(GoalFirstAutopilotState.customizing)
    await control._callback_message(callback).answer(
        "Генерация или render-pack завершились ошибкой. Можно выбрать другой "
        "вариант, загрузить свою картинку или продолжить без неё.",
        reply_markup=_custom_keyboard(business_token),
    )


@router.callback_query(F.data.startswith("cpo:gencheck:"))
async def check_generated_image(callback: CallbackQuery, state: FSMContext) -> None:
    """Poll the canonical generated visual job; old callback name stays stable."""

    business_token = str(callback.data).split(":", 2)[2]
    data = await state.get_data()
    if not _state_matches(data, business_token):
        await callback.answer("Этот черновик уже устарел", show_alert=True)
        return
    if str(data.get("creative_variant_id") or "").strip():
        await _check_studio_generated_image(
            callback,
            state,
            business_token=business_token,
            data=data,
        )
        return
    visual_kind = (
        "video"
        if str(data.get("creative_generation_kind") or "").strip().lower() == "video"
        else "image"
    )
    check_noun = "видео" if visual_kind == "video" else "картинку"
    own_noun = "своё видео" if visual_kind == "video" else "свою картинку"
    pronoun = "него" if visual_kind == "video" else "неё"
    try:
        job = await asyncio.to_thread(
            poll_ad_visual,
            job_id=str(data["creative_job_id"]),
            scope_id=str(data["business_id"]),
        )
    except (KeyError, VisualCreativeError):
        await callback.answer(f"Пока не удалось проверить {check_noun}", show_alert=True)
        return
    await callback.answer()
    editable_project_id = (
        str(data.get("editable_ad_project_id") or "").strip()
        if bool(data.get("editable_generation_active"))
        else ""
    )
    if editable_project_id and await _finish_editable_source_generation(
        callback,
        state,
        job=job,
        data=data,
        kind=visual_kind,
        project_id=editable_project_id,
    ):
        return
    if not editable_project_id and await _finish_generated_visual(
        callback,
        state,
        job=job,
        data=data,
        kind=visual_kind,
    ):
        return
    if str(getattr(job, "status", "") or "") in {"queued", "running"}:
        await control._callback_message(callback).answer(
            "⏳ Ещё создаётся. Повторно генерация не запускается.",
            reply_markup=control._keyboard(
                [[("🔄 Проверить готовность", f"cpo:gencheck:{business_token}")]]
            ),
        )
        return
    if editable_project_id:
        try:
            actor = await control._actor(
                int(callback.from_user.id),
                str(data.get("business_id") or ""),
            )
            await asyncio.to_thread(
                advance_failed_editable_ad_source,
                actor=actor,
                project_id=editable_project_id,
            )
        except (LookupError, ValueError, TenantPermissionDenied):
            log.warning(
                "Failed to advance editable source revision after polled provider failure "
                "business_id=%s project_id=%s",
                str(data.get("business_id") or ""),
                editable_project_id,
                exc_info=True,
            )
    await state.update_data(
        creative_job_id="",
        creative_generation_kind=visual_kind,
        editable_generation_active=False,
    )
    await state.set_state(GoalFirstAutopilotState.customizing)
    await control._callback_message(callback).answer(
        visual_failure_message(job)
        + f"\n\nМожно загрузить {own_noun} или продолжить без {pronoun}.",
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
