from __future__ import annotations

import asyncio
import importlib
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from clientplatform.application.programs import (
    archive_program_draft_lesson,
    get_program_draft,
    get_program_draft_lesson,
    move_program_draft_lesson,
    replace_program_draft_lesson_content,
    update_program_draft_lesson_title,
)
from clientplatform.domain.external_media import normalize_external_media_url
from clientplatform.domain.programs import (
    ContentKind,
    Lesson,
    ProgramRecord,
    normalize_content_ref,
    normalize_lesson_title,
)

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_program_lesson_editor")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_PAGE_SIZE = 8


class ClientPlatformDraftLessonEditorState(StatesGroup):
    title = State()
    content = State()


def _program_callback(
    action: str,
    business_id: str,
    program_id: str,
    page: int | None = None,
) -> str:
    value = (
        f"cp:{action}:{control._uuid_token(business_id)}:"
        f"{control._uuid_token(program_id)}"
    )
    return value if page is None else f"{value}:{max(0, int(page))}"


def _lesson_callback(action: str, business_id: str, lesson_id: str) -> str:
    return (
        f"cp:{action}:{control._uuid_token(business_id)}:"
        f"{control._uuid_token(lesson_id)}"
    )


def _parse_lesson_callback(callback: CallbackQuery) -> tuple[str, str]:
    parts = str(callback.data or "").split(":", 3)
    return control._token_uuid(parts[2]), control._token_uuid(parts[3])


def _parse_program_callback(callback: CallbackQuery) -> tuple[str, str, int]:
    parts = str(callback.data or "").split(":", 4)
    page = 0
    if len(parts) == 5:
        try:
            page = max(0, int(parts[4]))
        except ValueError:
            page = 0
    return control._token_uuid(parts[2]), control._token_uuid(parts[3]), page


def _kind_label(kind: ContentKind) -> str:
    return {
        ContentKind.TEXT: "текст",
        ContentKind.AUDIO: "аудио",
        ContentKind.VIDEO: "видео",
        ContentKind.IMAGE: "изображение",
        ContentKind.DOCUMENT: "документ",
        ContentKind.LINK: "ссылка",
        ContentKind.TASK: "задание",
        ContentKind.MIXED: "смешанный материал",
    }.get(kind, kind.value)


def _lesson_detail_text(record: ProgramRecord, lesson: Lesson) -> str:
    lines = [
        f"Черновик «{record.program.title}»",
        "",
        f"Урок {lesson.position} из {len(record.lessons)}",
        f"Название: {lesson.title}",
        f"Тип материала: {_kind_label(lesson.content_kind)}",
    ]
    if lesson.content_kind == ContentKind.TEXT:
        preview = lesson.content_ref[:500]
        if len(lesson.content_ref) > len(preview):
            preview += "…"
        lines.extend(["", f"Материал:\n{preview}"])
    elif lesson.content_ref.startswith("https://"):
        external = None
        try:
            external = normalize_external_media_url(lesson.content_ref)
        except ValueError:
            external = None
        if external is not None:
            lines.extend(
                [
                    "",
                    f"Источник: {external.provider_label}",
                    "Хранение: внешний файл — место ClientPlatform не расходуется.",
                ]
            )
    return "\n".join(lines)


def _lesson_detail_keyboard(record: ProgramRecord, lesson: Lesson):
    business_id = record.program.business_id
    lesson_id = lesson.id
    movement: list[tuple[str, str]] = []
    if lesson.position > 1:
        movement.append(("⬆️ Выше", _lesson_callback("dlup", business_id, lesson_id)))
    if lesson.position < len(record.lessons):
        movement.append(("⬇️ Ниже", _lesson_callback("dldown", business_id, lesson_id)))
    page = (lesson.position - 1) // _PAGE_SIZE
    rows: list[list[tuple[str, str]]] = [
        [("✏️ Переименовать", _lesson_callback("dlname", business_id, lesson_id))],
        [("🔄 Заменить материал", _lesson_callback("dlmat", business_id, lesson_id))],
    ]
    if movement:
        rows.append(movement)
    rows.extend(
        [
            [("🗑 Удалить урок", _lesson_callback("dlask", business_id, lesson_id))],
            [
                (
                    "К списку уроков",
                    _program_callback(
                        "dless",
                        business_id,
                        record.program.id,
                        page,
                    ),
                )
            ],
        ]
    )
    markup = control._keyboard(rows)
    if lesson.content_ref.startswith("https://"):
        try:
            external = normalize_external_media_url(lesson.content_ref)
        except ValueError:
            return markup
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Открыть материал", url=external.url)],
                *markup.inline_keyboard,
            ]
        )
    return markup


def _lesson_list_keyboard(record: ProgramRecord, *, page: int):
    page_count = max(1, (len(record.lessons) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    safe_page = min(max(0, page), page_count - 1)
    start = safe_page * _PAGE_SIZE
    visible = record.lessons[start : start + _PAGE_SIZE]
    rows: list[list[tuple[str, str]]] = [
        [
            (
                f"{lesson.position}. {lesson.title[:38]}",
                _lesson_callback(
                    "dled",
                    record.program.business_id,
                    lesson.id,
                ),
            )
        ]
        for lesson in visible
    ]
    navigation: list[tuple[str, str]] = []
    if safe_page > 0:
        navigation.append(
            (
                "← Назад",
                _program_callback(
                    "dless",
                    record.program.business_id,
                    record.program.id,
                    safe_page - 1,
                ),
            )
        )
    if safe_page + 1 < page_count:
        navigation.append(
            (
                "Дальше →",
                _program_callback(
                    "dless",
                    record.program.business_id,
                    record.program.id,
                    safe_page + 1,
                ),
            )
        )
    if navigation:
        rows.append(navigation)
    rows.append(
        [
            (
                "К черновику",
                _program_callback(
                    "dopen",
                    record.program.business_id,
                    record.program.id,
                ),
            )
        ]
    )
    return control._keyboard(rows), safe_page, page_count


def _edit_cancel_keyboard(*, business_id: str, lesson_id: str):
    return control._keyboard(
        [
            [
                (
                    "Отменить изменение",
                    _lesson_callback("dlcancel", business_id, lesson_id),
                )
            ]
        ]
    )


async def _load_lesson(
    *,
    user_id: int,
    business_id: str,
    lesson_id: str,
) -> tuple[Any, ProgramRecord, Lesson]:
    actor = await control._actor(user_id, business_id)
    record, lesson = await asyncio.to_thread(
        get_program_draft_lesson,
        actor=actor,
        lesson_id=lesson_id,
    )
    return actor, record, lesson


async def _send_lesson_detail(
    message: Message,
    *,
    record: ProgramRecord,
    lesson: Lesson,
) -> None:
    await message.answer(
        _lesson_detail_text(record, lesson),
        reply_markup=_lesson_detail_keyboard(record, lesson),
    )


async def _send_lesson_list(
    message: Message,
    *,
    record: ProgramRecord,
    page: int,
) -> None:
    keyboard, safe_page, page_count = _lesson_list_keyboard(record, page=page)
    text = (
        f"Уроки черновика «{record.program.title}»\n"
        f"Всего: {len(record.lessons)}. Страница {safe_page + 1} из {page_count}."
    )
    if not record.lessons:
        text += "\n\nУроков пока нет."
    await message.answer(text, reply_markup=keyboard)


def _editor_session(data: dict[str, Any]) -> tuple[str, str] | None:
    business_id = str(data.get("editor_business_id") or "").strip()
    lesson_id = str(data.get("editor_lesson_id") or "").strip()
    if not business_id or not lesson_id:
        return None
    return business_id, lesson_id


@router.callback_query(F.data.startswith("cp:dless:"))
async def open_lesson_list(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, program_id, page = _parse_program_callback(callback)
    actor = await control._actor(int(callback.from_user.id), business_id)
    record = await asyncio.to_thread(
        get_program_draft,
        actor=actor,
        program_id=program_id,
    )
    await state.clear()
    await callback.answer()
    await _send_lesson_list(
        control._callback_message(callback),
        record=record,
        page=page,
    )


@router.callback_query(F.data.startswith("cp:dled:"))
async def open_lesson(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, lesson_id = _parse_lesson_callback(callback)
    _actor, record, lesson = await _load_lesson(
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lesson_id=lesson_id,
    )
    await state.clear()
    await callback.answer()
    await _send_lesson_detail(
        control._callback_message(callback),
        record=record,
        lesson=lesson,
    )


@router.callback_query(F.data.startswith("cp:dlname:"))
async def begin_lesson_title_edit(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, lesson_id = _parse_lesson_callback(callback)
    _actor, _record, lesson = await _load_lesson(
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lesson_id=lesson_id,
    )
    await state.clear()
    await state.update_data(
        editor_business_id=business_id,
        editor_lesson_id=lesson_id,
    )
    await state.set_state(ClientPlatformDraftLessonEditorState.title)
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Текущее название: {lesson.title}\n\nНапишите новое название урока.",
        reply_markup=_edit_cancel_keyboard(
            business_id=business_id,
            lesson_id=lesson_id,
        ),
    )


@router.message(ClientPlatformDraftLessonEditorState.title)
async def save_lesson_title(message: Message, state: FSMContext) -> None:
    session = _editor_session(await state.get_data())
    if session is None:
        await state.clear()
        await message.answer("Редактор был закрыт. Откройте черновик заново.")
        return
    business_id, lesson_id = session
    try:
        title = normalize_lesson_title(str(message.text or ""))
    except ValueError:
        await message.answer("Название урока должно содержать от 1 до 200 символов.")
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    record, lesson = await asyncio.to_thread(
        update_program_draft_lesson_title,
        actor=actor,
        lesson_id=lesson_id,
        title=title,
    )
    await state.clear()
    await message.answer("Название урока сохранено.")
    await _send_lesson_detail(message, record=record, lesson=lesson)


@router.callback_query(F.data.startswith("cp:dlmat:"))
async def begin_lesson_content_edit(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, lesson_id = _parse_lesson_callback(callback)
    _actor, _record, lesson = await _load_lesson(
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lesson_id=lesson_id,
    )
    await state.clear()
    await state.update_data(
        editor_business_id=business_id,
        editor_lesson_id=lesson_id,
    )
    await state.set_state(ClientPlatformDraftLessonEditorState.content)
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Сейчас используется: {_kind_label(lesson.content_kind)}.\n\n"
        "Отправьте новый текст, аудио, голосовое сообщение, видео, "
        "изображение или документ.",
        reply_markup=_edit_cancel_keyboard(
            business_id=business_id,
            lesson_id=lesson_id,
        ),
    )


@router.message(ClientPlatformDraftLessonEditorState.content)
async def save_lesson_content(message: Message, state: FSMContext) -> None:
    session = _editor_session(await state.get_data())
    if session is None:
        await state.clear()
        await message.answer("Редактор был закрыт. Откройте черновик заново.")
        return
    business_id, lesson_id = session
    try:
        content_kind, content_ref = control._message_content(message)
    except ValueError:
        await message.answer(
            "Поддерживаются текст, аудио, голосовое сообщение, видео, "
            "изображение или документ."
        )
        return
    try:
        normalized_ref = normalize_content_ref(content_ref)
    except ValueError:
        await message.answer(
            "Материал слишком длинный. Отправьте не более 2048 символов "
            "или приложите его отдельным файлом."
        )
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    record, lesson = await asyncio.to_thread(
        replace_program_draft_lesson_content,
        actor=actor,
        lesson_id=lesson_id,
        content_kind=content_kind,
        content_ref=normalized_ref,
    )
    await state.clear()
    await message.answer("Материал урока заменён.")
    await _send_lesson_detail(message, record=record, lesson=lesson)


async def _move_lesson(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    direction: str,
) -> None:
    business_id, lesson_id = _parse_lesson_callback(callback)
    actor = await control._actor(int(callback.from_user.id), business_id)
    record = await asyncio.to_thread(
        move_program_draft_lesson,
        actor=actor,
        lesson_id=lesson_id,
        direction=direction,
    )
    lesson = next(item for item in record.lessons if item.id == lesson_id)
    await state.clear()
    await callback.answer("Порядок уроков обновлён")
    await _send_lesson_detail(
        control._callback_message(callback),
        record=record,
        lesson=lesson,
    )


@router.callback_query(F.data.startswith("cp:dlup:"))
async def move_lesson_up(callback: CallbackQuery, state: FSMContext) -> None:
    await _move_lesson(callback, state, direction="up")


@router.callback_query(F.data.startswith("cp:dldown:"))
async def move_lesson_down(callback: CallbackQuery, state: FSMContext) -> None:
    await _move_lesson(callback, state, direction="down")


@router.callback_query(F.data.startswith("cp:dlask:"))
async def ask_lesson_delete(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, lesson_id = _parse_lesson_callback(callback)
    _actor, record, lesson = await _load_lesson(
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lesson_id=lesson_id,
    )
    await state.clear()
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Удалить урок {lesson.position} «{lesson.title}»?\n"
        "Остальные уроки будут автоматически перенумерованы.",
        reply_markup=control._keyboard(
            [
                [
                    (
                        "Да, удалить урок",
                        _lesson_callback("dldel", business_id, lesson_id),
                    )
                ],
                [
                    (
                        "Нет, вернуться",
                        _lesson_callback("dled", business_id, lesson_id),
                    )
                ],
                [
                    (
                        "К черновику",
                        _program_callback(
                            "dopen",
                            business_id,
                            record.program.id,
                        ),
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cp:dldel:"))
async def confirm_lesson_delete(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, lesson_id = _parse_lesson_callback(callback)
    actor, _record, lesson = await _load_lesson(
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lesson_id=lesson_id,
    )
    record = await asyncio.to_thread(
        archive_program_draft_lesson,
        actor=actor,
        lesson_id=lesson_id,
    )
    page = max(0, (lesson.position - 1) // _PAGE_SIZE)
    await state.clear()
    await callback.answer("Урок удалён")
    await _send_lesson_list(
        control._callback_message(callback),
        record=record,
        page=page,
    )


@router.callback_query(F.data.startswith("cp:dlcancel:"))
async def cancel_lesson_edit(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, lesson_id = _parse_lesson_callback(callback)
    _actor, record, lesson = await _load_lesson(
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lesson_id=lesson_id,
    )
    await state.clear()
    await callback.answer("Изменение отменено")
    await _send_lesson_detail(
        control._callback_message(callback),
        record=record,
        lesson=lesson,
    )


__all__ = [
    "ClientPlatformDraftLessonEditorState",
    "ask_lesson_delete",
    "begin_lesson_content_edit",
    "begin_lesson_title_edit",
    "cancel_lesson_edit",
    "confirm_lesson_delete",
    "move_lesson_down",
    "move_lesson_up",
    "open_lesson",
    "open_lesson_list",
    "router",
    "save_lesson_content",
    "save_lesson_title",
]
