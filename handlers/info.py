from __future__ import annotations

import asyncio
import logging
import sqlite3
import tempfile
from pathlib import Path

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, Message

from core.callback_utils import safe_answer_callback
from keyboards.inline import kb_back_main
from services.privacy_controls import erase_user_behavioral_data, write_user_data_export_gzip

router = Router()
log = logging.getLogger(__name__)

SUPPORT_TEXT = (
    "Если у Вас возникли вопросы — напишите в поддержку:\n"
    "@metrotherapysupportbot\n\n"
    "Если Telegram не открывает упоминание, можно перейти по ссылке:\n"
    "https://t.me/metrotherapysupportbot"
)

POLICY_URL = "https://t.me/metrotherapyprivacy"


def _callback_message(cb: CallbackQuery) -> Message | None:
    message = cb.message
    return message if isinstance(message, Message) else None


def _message_user_id(message: Message) -> int | None:
    user = message.from_user
    return int(user.id) if user is not None else None


def _delete_confirmed(text: str | None) -> bool:
    parts = str(text or "").strip().split(maxsplit=1)
    return len(parts) == 2 and parts[1].strip().upper() == "CONFIRM"


def _export_confirmed(text: str | None) -> bool:
    parts = str(text or "").strip().split(maxsplit=1)
    if len(parts) != 2 or parts[1].strip().upper() != "CONFIRM":
        return False
    command = parts[0].strip().casefold().split("@", maxsplit=1)[0]
    return command == "/mydata"


def _is_private_chat(message: Message) -> bool:
    chat = getattr(message, "chat", None)
    raw_type = getattr(chat, "type", None)
    value = getattr(raw_type, "value", raw_type)
    return str(value or "").strip().casefold() == ChatType.PRIVATE.value


def _new_export_path() -> Path:
    handle = tempfile.NamedTemporaryFile(
        prefix="metrotherapy-user-data-",
        suffix=".json.gz",
        delete=False,
    )
    path = Path(handle.name)
    handle.close()
    return path


def _remove_export_path(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.exception("Temporary privacy export cleanup failed: path=%s", path)


@router.callback_query(lambda c: (c.data or "") == "info:support")
async def cb_support(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    await message.answer(SUPPORT_TEXT, reply_markup=kb_back_main())


@router.callback_query(lambda c: (c.data or "") == "info:policy")
async def cb_policy(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    await message.answer(
        f"🔐 Политика конфиденциальности:\n{POLICY_URL}\n\n"
        "Получить копию своих данных: /mydata — затем /mydata CONFIRM\n"
        "Удалить поведенческие данные: /deletemydata",
        reply_markup=kb_back_main(),
    )


async def _answer_export_failure(message: Message, user_id: int) -> None:
    log.exception("User data export failed: user_id=%s", user_id)
    await message.answer(
        "Не удалось подготовить экспорт данных. Повторите позже или напишите в поддержку: "
        "@metrotherapysupportbot"
    )


@router.message(Command("mydata"))
async def cmd_my_data(message: Message) -> None:
    user_id = _message_user_id(message)
    if user_id is None:
        return
    if not _is_private_chat(message):
        await message.answer(
            "🔐 Экспорт данных доступен только в личном чате с ботом. "
            "Откройте личный диалог и отправьте /mydata заново."
        )
        return
    if not _export_confirmed(message.text):
        await message.answer(
            "⚠️ Экспорт может содержать историю использования, оценки состояния и платёжные записи. "
            "Архив сжат, но не зашифрован, и после отправки останется в истории этого чата.\n\n"
            "Для подтверждения отправьте точно:\n"
            "/mydata CONFIRM"
        )
        return

    export_path: Path | None = None
    try:
        export_path = _new_export_path()
        result = await asyncio.to_thread(
            write_user_data_export_gzip,
            user_id,
            export_path,
        )
        document = FSInputFile(
            result.path,
            filename="metrotherapy-user-data.json.gz",
        )
        await message.answer_document(
            document,
            caption=(
                "🔐 Это сжатый JSON-экспорт данных, связанных с Вашим аккаунтом. "
                f"Записей: {result.total_rows}. "
                "Архив не зашифрован. Сохраните его только в защищённом месте и удалите это сообщение, "
                "когда файл больше не нужен в истории чата."
            ),
        )
    except sqlite3.Error:
        await _answer_export_failure(message, user_id)
    except RuntimeError:
        await _answer_export_failure(message, user_id)
    except OSError:
        await _answer_export_failure(message, user_id)
    except ValueError:
        await _answer_export_failure(message, user_id)
    except TypeError:
        await _answer_export_failure(message, user_id)
    finally:
        if export_path is not None:
            await asyncio.to_thread(_remove_export_path, export_path)


async def _answer_erasure_failure(message: Message, user_id: int) -> None:
    log.exception("User data erasure failed: user_id=%s", user_id)
    await message.answer(
        "Не удалось выполнить удаление данных. Повторите позже или напишите в поддержку: "
        "@metrotherapysupportbot"
    )


@router.message(Command("deletemydata"))
async def cmd_delete_my_data(message: Message) -> None:
    user_id = _message_user_id(message)
    if user_id is None:
        return
    if not _delete_confirmed(message.text):
        await message.answer(
            "⚠️ Команда удалит поведенческую историю и очистит отображаемые данные профиля. "
            "Технический идентификатор канала, платёжные, возвратные и иные обязательные учётные записи "
            "сохраняются для исполнения оплаченного доступа, предотвращения повторных операций и требований учёта.\n\n"
            "Для подтверждения отправьте точно:\n"
            "/deletemydata CONFIRM"
        )
        return

    try:
        result = await asyncio.to_thread(
            erase_user_behavioral_data,
            user_id,
            reason="telegram_user_request",
        )
    except sqlite3.Error:
        await _answer_erasure_failure(message, user_id)
        return
    except RuntimeError:
        await _answer_erasure_failure(message, user_id)
        return
    except ValueError:
        await _answer_erasure_failure(message, user_id)
        return
    except TypeError:
        await _answer_erasure_failure(message, user_id)
        return

    deleted_rows = sum(int(value) for value in result.deleted_tables.values())
    await message.answer(
        "✅ Поведенческие данные удалены, отображаемые данные профиля очищены.\n"
        f"Удалено записей: {deleted_rows}.\n"
        "Технический идентификатор канала, платёжные и иные обязательные учётные записи сохранены "
        "для работы оплаченного доступа и требований учёта."
    )
