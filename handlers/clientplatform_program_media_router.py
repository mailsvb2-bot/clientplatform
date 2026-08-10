from __future__ import annotations

"""Priority handlers that externalize media before any program DB mutation."""

import asyncio
import importlib
import logging

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from clientplatform.application.program_media import (
    ProgramMediaCleanupQueueError,
    ProgramMediaStoreError,
    cancel_program_media_cleanup,
    delete_uncommitted_program_media,
    program_media_ingest_policy,
    queue_program_media_cleanup,
    stage_program_media_cleanup,
)
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
log = logging.getLogger(__name__)


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


async def _stage_cleanup(
    *,
    business_id: str,
    media_reference: str,
    reason: str,
) -> bool:
    try:
        return await asyncio.to_thread(
            stage_program_media_cleanup,
            business_id=business_id,
            media_reference=media_reference,
            reason=reason,
        )
    except ProgramMediaCleanupQueueError:
        try:
            await asyncio.to_thread(
                delete_uncommitted_program_media,
                media_reference=media_reference,
            )
        except ProgramMediaStoreError:
            log.exception("Failed to compensate uncommitted program media")
        raise


async def _cancel_cleanup_safely(*, media_reference: str) -> None:
    try:
        await asyncio.to_thread(
            cancel_program_media_cleanup,
            media_reference=media_reference,
        )
    except ProgramMediaCleanupQueueError:
        log.exception("Failed to cancel a referenced program media cleanup intent")


async def _queue_cleanup_safely(
    *,
    business_id: str,
    media_reference: str,
    reason: str,
) -> None:
    try:
        await asyncio.to_thread(
            queue_program_media_cleanup,
            business_id=business_id,
            media_reference=media_reference,
            reason=reason,
        )
    except ProgramMediaCleanupQueueError:
        log.exception("Failed to expedite a program media cleanup intent")


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

    actor = await control._actor(int(message.from_user.id), business_id)
    material = await _materialize(message, business_id=business_id)
    if material is None:
        return
    content_kind, content_ref = material
    staged = await _stage_cleanup(
        business_id=business_id,
        media_reference=content_ref,
        reason="pending_lesson_add",
    )
    mutation_succeeded = False
    try:
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
        mutation_succeeded = True
    finally:
        if staged and not mutation_succeeded:
            await _queue_cleanup_safely(
                business_id=business_id,
                media_reference=content_ref,
                reason="failed_lesson_add",
            )
    if staged:
        await _cancel_cleanup_safely(media_reference=content_ref)
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
    actor = await control._actor(int(message.from_user.id), business_id)
    previous_lesson = None
    cleanup_policy = await asyncio.to_thread(program_media_ingest_policy)
    if cleanup_policy.enabled:
        actor, _previous_record, previous_lesson = await editor._load_lesson(
            user_id=int(message.from_user.id),
            business_id=business_id,
            lesson_id=lesson_id,
        )
    material = await _materialize(message, business_id=business_id)
    if material is None:
        return
    content_kind, content_ref = material
    staged = await _stage_cleanup(
        business_id=business_id,
        media_reference=content_ref,
        reason="pending_lesson_replacement",
    )
    mutation_succeeded = False
    try:
        record, lesson = await asyncio.to_thread(
            editor.replace_program_draft_lesson_content,
            actor=actor,
            lesson_id=lesson_id,
            content_kind=content_kind,
            content_ref=content_ref,
        )
        mutation_succeeded = True
    finally:
        if staged and not mutation_succeeded:
            await _queue_cleanup_safely(
                business_id=business_id,
                media_reference=content_ref,
                reason="failed_lesson_replacement",
            )
    if staged:
        await _cancel_cleanup_safely(media_reference=content_ref)
    if previous_lesson is not None and previous_lesson.content_ref != content_ref:
        await _queue_cleanup_safely(
            business_id=business_id,
            media_reference=previous_lesson.content_ref,
            reason="superseded_lesson_material",
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
    actor = await control._actor(control._user_id(message), business_id)
    material = await _materialize(message, business_id=business_id)
    if material is None:
        return
    content_kind, content_ref = material
    staged = await _stage_cleanup(
        business_id=business_id,
        media_reference=content_ref,
        reason="pending_legacy_program_create",
    )
    mutation_succeeded = False
    try:
        program = await asyncio.to_thread(
            control.create_single_lesson_program,
            actor=actor,
            program_title=program_title,
            lesson_title=lesson_title,
            content_kind=content_kind,
            content_ref=content_ref,
        )
        mutation_succeeded = True
    finally:
        if staged and not mutation_succeeded:
            await _queue_cleanup_safely(
                business_id=business_id,
                media_reference=content_ref,
                reason="failed_legacy_program_create",
            )
    if staged:
        await _cancel_cleanup_safely(media_reference=content_ref)
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
