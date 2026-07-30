from __future__ import annotations

"""Priority handlers that externalize media before any program DB mutation."""

import asyncio
import importlib

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from clientplatform.domain.programs import normalize_content_ref
from handlers.clientplatform_program_media import (
    ProgramMediaIngestError,
    materialize_program_content,
)

control = importlib.import_module(".clientplatform_control", __package__)
builder = importlib.import_module(".clientplatform_program_builder", __package__)
editor = importlib.import_module(".clientplatform_program_lesson_editor", __package__)

router = Router(name="clientplatform_program_media_router")
router.message.filter(control.ClientPlatformControlEnabled())


async def _answer_ingest_error(message: Message, exc: ProgramMediaIngestError) -> None:
    if exc.code == "program_media_too_large":
        text = "Файл слишком большой. Отправьте файл размером не более 20 МБ."
    elif exc.code in {
        "program_media_ingest_disabled",
        "program_media_missing_clientplatform_media_gateway_s3_endpoint",
        "program_media_missing_clientplatform_storage_bucket",
        "program_media_missing_clientplatform_secret_s3_access_key",
        "program_media_missing_clientplatform_secret_s3_secret_key",
    }:
        text = (
            "Безопасное хранилище файлов пока не настроено. "
            "Можно добавить текстовый урок или повторить после настройки хранилища."
        )
    elif exc.retryable:
        text = "Не удалось безопасно сохранить файл. Попробуйте отправить его ещё раз."
    else:
        text = "Не удалось обработать этот файл. Отправьте другой файл или обычный текст."
    await message.answer(text)


async def _materialize(
    message: Message,
    *,
    business_id: str,
) -> tuple[object, str] | None:
    try:
        content_kind, content_ref = await materialize_program_content(
            message,
            business_id=business_id,
        )
    except ProgramMediaIngestError as exc:
        await _answer_ingest_error(message, exc)
        return None
    except ValueError:
        await message.answer(
            "Поддерживаются текст, аудио, голосовое сообщение, видео, "
            "изображение или документ."
        )
        return None
    try:
        return content_kind, normalize_content_ref(content_ref)
    except ValueError:
        await message.answer(
            "Материал слишком длинный. Отправьте не более 2048 символов "
            "или приложите его отдельным файлом."
        )
        return None


@router.message(builder.ClientPlatformProgramBuilderState.lesson_content)
async def capture_persistent_lesson_content(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    session = builder._session_ids(data)
    lesson_title = str(data.get("lesson_title") or "").strip()
    if session is None or not lesson_title:
        await state.clear()
        await message.answer("Конструктор был закрыт. Откройте раздел программ заново.")
        return
    business_id, program_id = session
    record = await builder._load_draft(
        user_id=int(message.from_user.id),
        business_id=business_id,
        program_id=program_id,
    )
    if len(record.lessons) >= builder._MAX_LESSONS_PER_PROGRAM:
        await state.set_state(builder.ClientPlatformProgramBuilderState.review)
        await message.answer(
            "В одной программе может быть не более 100 уроков.",
            reply_markup=builder._draft_keyboard(record),
        )
        return

    material = await _materialize(message, business_id=business_id)
    if material is None:
        return
    content_kind, content_ref = material
    actor = await control._actor(int(message.from_user.id), business_id)
    await asyncio.to_thread(
        builder.add_program_lesson,
        actor=actor,
        program_id=program_id,
        title=lesson_title,
        content_kind=content_kind,
        content_ref=content_ref,
    )
    record = await asyncio.to_thread(
        builder.get_program_draft,
        actor=actor,
        program_id=program_id,
    )
    await state.update_data(lesson_title="")
    await state.set_state(builder.ClientPlatformProgramBuilderState.review)
    await builder._send_draft_review(message, record)


@router.message(editor.ClientPlatformDraftLessonEditorState.content)
async def replace_persistent_lesson_content(
    message: Message,
    state: FSMContext,
) -> None:
    session = editor._editor_session(await state.get_data())
    if session is None:
        await state.clear()
        await message.answer("Редактор был закрыт. Откройте черновик заново.")
        return
    business_id, lesson_id = session
    material = await _materialize(message, business_id=business_id)
    if material is None:
        return
    content_kind, content_ref = material
    actor = await control._actor(int(message.from_user.id), business_id)
    record, lesson = await asyncio.to_thread(
        editor.replace_program_draft_lesson_content,
        actor=actor,
        lesson_id=lesson_id,
        content_kind=content_kind,
        content_ref=content_ref,
    )
    await state.clear()
    await message.answer("Материал урока заменён.")
    await editor._send_lesson_detail(message, record=record, lesson=lesson)


@router.message(control.ClientPlatformControlState.lesson_content)
async def capture_legacy_lesson_content(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    business_id = str(data.get("business_id") or "").strip()
    program_title = str(data.get("program_title") or "").strip()
    lesson_title = str(data.get("lesson_title") or "").strip()
    if not business_id or not program_title or not lesson_title:
        await state.clear()
        await message.answer("Конструктор был закрыт. Откройте раздел программ заново.")
        return
    material = await _materialize(message, business_id=business_id)
    if material is None:
        return
    content_kind, content_ref = material
    actor = await control._actor(control._user_id(message), business_id)
    program = await asyncio.to_thread(
        control.create_single_lesson_program,
        actor=actor,
        program_title=program_title,
        lesson_title=lesson_title,
        content_kind=content_kind,
        content_ref=content_ref,
    )
    await state.clear()
    await message.answer(
        f"Программа «{program.program.title}» создана и готова к выдаче клиентам."
    )
    await control._send_dashboard(
        message,
        user_id=control._user_id(message),
        business_id=business_id,
    )


__all__ = ["router"]
