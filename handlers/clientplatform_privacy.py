from __future__ import annotations

import asyncio
import importlib

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from services.privacy_controls import erase_user_behavioral_data
from services.privacy_export_links import issue_privacy_export_url, privacy_export_ttl_minutes

control = importlib.import_module('.clientplatform_control', __package__)

router = Router(name='clientplatform_privacy')
router.message.filter(control.ClientPlatformControlEnabled())


def _confirmed(text: str | None, command: str) -> bool:
    parts = str(text or '').strip().split()
    if len(parts) != 2:
        return False
    head = parts[0].split('@', 1)[0].casefold()
    return head == f'/{command}'.casefold() and parts[1].casefold() == 'confirm'


def _private_chat(message: Message) -> bool:
    return str(getattr(getattr(message, 'chat', None), 'type', '') or '').casefold() == 'private'


@router.message(Command('privacy'))
async def clientplatform_privacy_info(message: Message) -> None:
    await message.answer(
        'Конфиденциальность ClientPlatform\n\n'
        'Вы можете получить экспорт своих данных командой /mydata и удалить '
        'поведенческие данные командой /deletemydata. Обе операции требуют явного подтверждения.'
    )


@router.message(Command('mydata'))
async def clientplatform_export_data(message: Message) -> None:
    if not _private_chat(message):
        await message.answer('Экспорт данных доступен только в личном чате с ClientPlatform.')
        return
    if not _confirmed(message.text, 'mydata'):
        await message.answer(
            'Для экспорта отправьте точно: /mydata CONFIRM\n\n'
            'После подтверждения ClientPlatform создаст одноразовую HTTPS-ссылку на архив.'
        )
        return
    user_id = control._user_id(message)
    url = await asyncio.to_thread(issue_privacy_export_url, user_id, platform='telegram')
    if not url:
        await message.answer('Не удалось подготовить экспорт. Повторите позже.')
        return
    ttl = privacy_export_ttl_minutes()
    await message.answer(
        'Одноразовая ссылка на экспорт Ваших данных:\n'
        f'{url}\n\n'
        f'Ссылка действует не более {ttl} минут и позволяет скачать архив один раз. '
        'Предпросмотр мессенджера не расходует ссылку.'
    )


@router.message(Command('deletemydata'))
async def clientplatform_delete_data(message: Message) -> None:
    if not _private_chat(message):
        await message.answer('Удаление данных доступно только в личном чате с ClientPlatform.')
        return
    if not _confirmed(message.text, 'deletemydata'):
        await message.answer(
            'Удаление необратимо. Для подтверждения отправьте точно: /deletemydata CONFIRM\n\n'
            'Технический идентификатор канала может сохраниться там, где это необходимо '
            'для безопасности, аудита или предотвращения повторной обработки.'
        )
        return
    user_id = control._user_id(message)
    result = await asyncio.to_thread(
        erase_user_behavioral_data,
        user_id,
        reason='telegram_user_request',
    )
    deleted = sum(int(value or 0) for value in result.deleted_tables.values())
    await message.answer(
        f'Удаление завершено. Удалено записей: {deleted}.\n'
        'Технический идентификатор канала сохраняется только в пределах обязательного '
        'операционного/безопасностного учёта.'
    )


__all__ = [
    '_confirmed',
    'clientplatform_delete_data',
    'clientplatform_export_data',
    'clientplatform_privacy_info',
    'router',
]
