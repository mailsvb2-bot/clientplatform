from __future__ import annotations

import asyncio

from aiogram import F
from aiogram.types import CallbackQuery, Message

from clientplatform.application.ad_connections import (
    disconnect_ad_connection,
    list_ad_connections,
)
from clientplatform.domain.ad_connections import (
    AdConnectionError,
    AdConnectionStatus,
)
from clientplatform.integrations.yandex_direct import YandexDirectError

from . import clientplatform_control as control
from . import clientplatform_simple_experience as simple


def _message(callback: CallbackQuery) -> Message:
    return control._callback_message(callback)


@simple.router.callback_query(F.data.startswith("cpa:disconnects:"))
async def list_disconnectable_ad_connections(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        connections = await asyncio.to_thread(list_ad_connections, actor=actor)
    except (AdConnectionError, RuntimeError, ValueError):
        await callback.answer("Не удалось открыть подключения", show_alert=True)
        return
    active = [
        item
        for item in connections
        if item.status not in {AdConnectionStatus.REVOKED, AdConnectionStatus.DISABLED}
    ]
    rows = [
        [
            (
                f"Отключить Яндекс · {item.external_login[:24]}",
                "cpa:disconnect:"
                f"{business_token}:{control._uuid_token(item.id)}",
            )
        ]
        for item in active
    ]
    rows.append([("⬅️ Рекламные кабинеты", f"cpa:home:{business_token}")])
    await callback.answer()
    await _message(callback).answer(
        "🔌 Отключение рекламного кабинета\n\n"
        + (
            "Выберите кабинет. ClientPlatform удалит локальный зашифрованный "
            "OAuth-доступ и отменит ещё не отправленные задания."
            if active
            else "Активных подключений нет."
        ),
        reply_markup=control._keyboard(rows),
    )


@simple.router.callback_query(F.data.startswith("cpa:disconnect:"))
async def confirm_ad_connection_disconnect(callback: CallbackQuery) -> None:
    _, _, business_token, connection_token = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    connection_id = control._token_uuid(connection_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        connections = await asyncio.to_thread(list_ad_connections, actor=actor)
    except (AdConnectionError, RuntimeError, ValueError):
        await callback.answer("Не удалось проверить кабинет", show_alert=True)
        return
    selected = next((item for item in connections if item.id == connection_id), None)
    if selected is None:
        await callback.answer("Кабинет не найден", show_alert=True)
        return
    await callback.answer()
    await _message(callback).answer(
        "Отключить Яндекс Директ?\n\n"
        f"Кабинет: {selected.external_login}\n\n"
        "ClientPlatform потеряет доступ и отменит неотправленные задания. Уже "
        "созданные объявления и расходы в Яндексе автоматически не остановятся.",
        reply_markup=control._keyboard(
            [
                [
                    (
                        "⛔ Да, отключить доступ",
                        "cpa:revoke:"
                        f"{business_token}:{connection_token}",
                    )
                ],
                [("Отмена", f"cpa:home:{business_token}")],
            ]
        ),
    )


@simple.router.callback_query(F.data.startswith("cpa:revoke:"))
async def revoke_ad_connection(callback: CallbackQuery) -> None:
    _, _, business_token, connection_token = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    connection_id = control._token_uuid(connection_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        connection = await asyncio.to_thread(
            disconnect_ad_connection,
            actor=actor,
            connection_id=connection_id,
        )
    except (AdConnectionError, YandexDirectError, RuntimeError, ValueError):
        await callback.answer("Не удалось отключить кабинет", show_alert=True)
        return
    await callback.answer("Доступ удалён")
    await _message(callback).answer(
        "✅ Рекламный кабинет отключён\n\n"
        f"Яндекс · {connection.external_login}\n"
        "Зашифрованные OAuth-данные удалены, новые объявления через "
        "ClientPlatform отправляться не будут. Проверьте действующие объявления "
        "и бюджеты непосредственно в Яндекс Директе.",
        reply_markup=control._keyboard(
            [[("📣 Рекламные кабинеты", f"cpa:home:{business_token}")]]
        ),
    )


__all__ = [
    "confirm_ad_connection_disconnect",
    "list_disconnectable_ad_connections",
    "revoke_ad_connection",
]
