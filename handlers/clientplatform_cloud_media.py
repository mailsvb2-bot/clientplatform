from __future__ import annotations

"""Cloud-first lesson material flow with optional private file upload."""

import asyncio
import importlib
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.program_media import queue_program_media_cleanup
from clientplatform.application.programs import (
    add_program_lesson,
    get_program_draft,
    replace_program_draft_lesson_content,
)
from clientplatform.domain.external_media import (
    external_delivery_kind,
    normalize_external_media_url,
)
from clientplatform.domain.programs import ContentKind, normalize_lesson_title

control = importlib.import_module(".clientplatform_control", __package__)
builder = importlib.import_module(".clientplatform_program_builder", __package__)
editor = importlib.import_module(".clientplatform_program_lesson_editor", __package__)

router = Router(name="clientplatform_cloud_media")
log = logging.getLogger(__name__)
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ClientPlatformCloudMediaState(StatesGroup):
    choose_kind = State()
    choose_source = State()
    public_url = State()


_KIND_LABELS = {
    ContentKind.VIDEO: "🎬 Видео",
    ContentKind.AUDIO: "🎧 Аудио",
    ContentKind.TEXT: "📝 Текст",
    ContentKind.DOCUMENT: "📄 Документ",
    ContentKind.IMAGE: "🖼 Изображение",
    ContentKind.LINK: "🔗 Ссылка",
}


def _kind_keyboard():
    return control._keyboard(
        [
            [("🎬 Видео", "cpcm:k:video"), ("🎧 Аудио", "cpcm:k:audio")],
            [("📝 Текст", "cpcm:k:text"), ("📄 Документ", "cpcm:k:document")],
            [("🖼 Изображение", "cpcm:k:image"), ("🔗 Ссылка", "cpcm:k:link")],
        ]
    )


def _source_keyboard():
    return control._keyboard(
        [
            [("☁️ В облаке — без расхода места", "cpcm:s:cloud")],
            [("📱 На телефоне или компьютере", "cpcm:s:device")],
        ]
    )


def _cloud_help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть Яндекс Диск", url="https://disk.yandex.ru/client/disk")],
            [InlineKeyboardButton(text="Открыть Google Drive", url="https://drive.google.com/drive/my-drive")],
            [InlineKeyboardButton(text="Открыть Dropbox", url="https://www.dropbox.com/home")],
            [InlineKeyboardButton(text="Открыть OneDrive", url="https://onedrive.live.com/")],
        ]
    )


def _context(data: dict[str, object]) -> tuple[str, str, str]:
    mode = str(data.get("cloud_media_mode") or "").strip()
    business_id = str(data.get("cloud_media_business_id") or "").strip()
    target_id = str(data.get("cloud_media_target_id") or "").strip()
    if mode not in {"add", "replace"} or not business_id or not target_id:
        raise ValueError("cloud media editor context is missing")
    return mode, business_id, target_id


async def _begin_kind_choice(
    target: Message,
    state: FSMContext,
    *,
    mode: str,
    business_id: str,
    target_id: str,
    lesson_title: str = "",
) -> None:
    await state.update_data(
        cloud_media_mode=mode,
        cloud_media_business_id=business_id,
        cloud_media_target_id=target_id,
        cloud_media_lesson_title=lesson_title,
        cloud_media_kind="",
    )
    await state.set_state(ClientPlatformCloudMediaState.choose_kind)
    await target.answer(
        "Что добавить в этот урок?\n\n"
        "Для больших видео и аудио лучше выбрать облако: файл останется у Вас, "
        "а ClientPlatform сохранит только безопасную ссылку — без расхода места на сервере.",
        reply_markup=_kind_keyboard(),
    )


@router.message(builder.ClientPlatformProgramBuilderState.lesson_title)
async def capture_lesson_title_cloud_first(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    session = builder._session_ids(data)
    if session is None:
        await state.clear()
        await message.answer("Конструктор был закрыт. Откройте раздел программ заново.")
        return
    try:
        title = normalize_lesson_title(str(message.text or ""))
    except ValueError:
        await message.answer("Название урока должно содержать от 1 до 200 символов.")
        return
    business_id, program_id = session
    await state.update_data(lesson_title=title)
    await _begin_kind_choice(
        message,
        state,
        mode="add",
        business_id=business_id,
        target_id=program_id,
        lesson_title=title,
    )


@router.callback_query(F.data.startswith("cp:dlmat:"))
async def begin_cloud_first_replacement(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, lesson_id = editor._parse_lesson_callback(callback)
    _actor, _record, lesson = await editor._load_lesson(
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lesson_id=lesson_id,
    )
    await state.clear()
    await callback.answer()
    await _begin_kind_choice(
        control._callback_message(callback),
        state,
        mode="replace",
        business_id=business_id,
        target_id=lesson_id,
        lesson_title=lesson.title,
    )


@router.callback_query(F.data.startswith("cpcm:k:"))
async def choose_material_kind(callback: CallbackQuery, state: FSMContext) -> None:
    raw = str(callback.data).split(":", 2)[2]
    try:
        kind = ContentKind(raw)
    except ValueError:
        await callback.answer("Неизвестный тип материала", show_alert=True)
        return
    try:
        _context(await state.get_data())
    except ValueError:
        await callback.answer("Откройте редактор урока заново", show_alert=True)
        return
    await state.update_data(cloud_media_kind=kind.value)
    await callback.answer()
    message = control._callback_message(callback)
    if kind == ContentKind.LINK:
        await state.set_state(ClientPlatformCloudMediaState.public_url)
        await message.answer("Вставьте публичную https-ссылку на материал.")
        return
    if kind == ContentKind.TEXT:
        await _switch_to_device_input(message, state, kind=kind)
        return
    await state.set_state(ClientPlatformCloudMediaState.choose_source)
    await message.answer(
        f"Вы выбрали: {_KIND_LABELS[kind]}.\n\nГде сейчас находится файл?",
        reply_markup=_source_keyboard(),
    )


async def _switch_to_device_input(
    message: Message,
    state: FSMContext,
    *,
    kind: ContentKind,
) -> None:
    data = await state.get_data()
    mode, _business_id, _target_id = _context(data)
    if mode == "add":
        await state.set_state(builder.ClientPlatformProgramBuilderState.lesson_content)
    else:
        await state.update_data(
            editor_business_id=data["cloud_media_business_id"],
            editor_lesson_id=data["cloud_media_target_id"],
        )
        await state.set_state(editor.ClientPlatformDraftLessonEditorState.content)
    if kind == ContentKind.TEXT:
        prompt = "Напишите текст урока одним сообщением."
    else:
        prompt = (
            "Выберите файл на телефоне или компьютере и отправьте его сюда. "
            "Небольшой файл будет сохранён в защищённом хранилище ClientPlatform."
        )
    await message.answer(prompt)


@router.callback_query(F.data == "cpcm:s:device")
async def choose_device_source(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        kind = ContentKind(str(data.get("cloud_media_kind") or ""))
        _context(data)
    except (ValueError, TypeError):
        await callback.answer("Откройте редактор урока заново", show_alert=True)
        return
    await callback.answer()
    await _switch_to_device_input(control._callback_message(callback), state, kind=kind)


@router.callback_query(F.data == "cpcm:s:cloud")
async def choose_cloud_source(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _context(await state.get_data())
    except ValueError:
        await callback.answer("Откройте редактор урока заново", show_alert=True)
        return
    await state.set_state(ClientPlatformCloudMediaState.public_url)
    await callback.answer()
    await control._callback_message(callback).answer(
        "1. Откройте своё облако.\n"
        "2. Выберите файл и включите доступ «по ссылке».\n"
        "3. Скопируйте полученную https-ссылку и отправьте её сюда.\n\n"
        "Поддерживаются Яндекс Диск, Google Drive, Dropbox, OneDrive и прямые HTTPS-ссылки. "
        "YouTube, Rutube, VK и Vimeo будут добавлены как открываемая ссылка.",
        reply_markup=_cloud_help_keyboard(),
    )


@router.message(ClientPlatformCloudMediaState.public_url)
async def save_public_cloud_url(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        mode, business_id, target_id = _context(data)
        requested_kind = ContentKind(str(data.get("cloud_media_kind") or "link"))
        reference = normalize_external_media_url(str(message.text or ""))
    except ValueError as exc:
        await message.answer(
            f"Не получилось принять ссылку: {exc}.\n\n"
            "Проверьте, что доступ открыт по ссылке и адрес начинается с https://"
        )
        return
    kind = external_delivery_kind(reference, requested_kind)
    actor = await control._actor(int(message.from_user.id), business_id)

    if mode == "add":
        lesson_title = str(data.get("cloud_media_lesson_title") or "").strip()
        await asyncio.to_thread(
            add_program_lesson,
            actor=actor,
            program_id=target_id,
            title=lesson_title,
            content_kind=kind,
            content_ref=reference.url,
        )
        record = await asyncio.to_thread(
            get_program_draft,
            actor=actor,
            program_id=target_id,
        )
        await state.clear()
        await state.set_state(builder.ClientPlatformProgramBuilderState.review)
        note = (
            "Материал добавлен как ссылка на видеосервис."
            if kind == ContentKind.LINK and requested_kind != ContentKind.LINK
            else f"Материал подключён из {reference.provider_label}."
        )
        await message.answer(
            f"✅ {note}\nФайл не копировался на сервер ClientPlatform и не расходует место."
        )
        await builder._send_draft_review(message, record)
        return

    _old_record, old_lesson = await asyncio.to_thread(
        editor.get_program_draft_lesson,
        actor=actor,
        lesson_id=target_id,
    )
    record, lesson = await asyncio.to_thread(
        replace_program_draft_lesson_content,
        actor=actor,
        lesson_id=target_id,
        content_kind=kind,
        content_ref=reference.url,
    )
    if old_lesson.content_ref != reference.url:
        try:
            await asyncio.to_thread(
                queue_program_media_cleanup,
                business_id=business_id,
                media_reference=old_lesson.content_ref,
                reason="superseded_by_external_cloud_material",
            )
        except RuntimeError:
            # The lesson update is already committed. Cleanup is best effort and
            # must never turn a successful user action into a visible failure.
            log.exception("Failed to queue superseded private lesson media cleanup")
    await state.clear()
    await message.answer(
        f"✅ Материал заменён ссылкой из {reference.provider_label}.\n"
        "Новый файл не копировался на сервер ClientPlatform."
    )
    await editor._send_lesson_detail(message, record=record, lesson=lesson)


__all__ = ["ClientPlatformCloudMediaState", "router"]
