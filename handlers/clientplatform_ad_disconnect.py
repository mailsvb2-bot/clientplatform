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
    AdConnectionInvariantViolation,
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
    except (AdConnectionError, ValueError):
        await callback.answer("Не удалось открыть подключения", show_alert=True)
        return
    except RuntimeError:
        await callback.answer("Не удалось открыть подключения", show_alert=True)
        return
    disconnectable = [
        item for item in connections if item.status != AdConnectionStatus.REVOKED
    ]
    rows = [
        [
            (
                (
                    "Завершить отключение Яндекс · "
                    if item.status == AdConnectionStatus.DISABLED
                    else "Отключить Яндекс · "
                )
                + item.external_login[:24],
                "cpa:disconnect:"
                f"{business_token}:{control._uuid_token(item.id)}",
            )
        ]
        for item in disconnectable
    ]
    rows.append([("⬅️ Рекламные кабинеты", f"cpa:home:{business_token}")])
    await callback.answer()
    await _message(callback).answer(
        "🔌 Отключение рекламного кабинета\n\n"
        + (
            "Выберите кабинет. ClientPlatform сразу блокирует новые отправки, "
            "дожидается правдивого завершения уже начатой публикации и только "
            "после этого удаляет локальный зашифрованный OAuth-доступ."
            if disconnectable
            else "Подключений для отключения нет."
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
    except (AdConnectionError, ValueError):
        await callback.answer("Не удалось проверить кабинет", show_alert=True)
        return
    except RuntimeError:
        await callback.answer("Не удалось проверить кабинет", show_alert=True)
        return
    selected = next((item for item in connections if item.id == connection_id), None)
    if selected is None or selected.status == AdConnectionStatus.REVOKED:
        await callback.answer("Кабинет не найден", show_alert=True)
        return
    await callback.answer()
    await _message(callback).answer(
        "Отключить Яндекс Директ?\n\n"
        f"Кабинет: {selected.external_login}\n\n"
        "ClientPlatform заблокирует новые отправки и отменит ещё не начатые "
        "задания. Если публикация уже ушла worker-у, её состояние сначала будет "
        "зафиксировано честно; затем можно завершить отзыв OAuth-доступа. Уже "
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
    except AdConnectionInvariantViolation as exc:
        if str(exc) == "advertising publication is still in progress; retry disconnect":
            await callback.answer(
                "Новые отправки уже заблокированы. Завершается ранее начатая публикация; повторите отключение ещё раз.",
                show_alert=True,
            )
        else:
            await callback.answer("Не удалось отключить кабинет", show_alert=True)
        return
    except (AdConnectionError, YandexDirectError, ValueError):
        await callback.answer("Не удалось отключить кабинет", show_alert=True)
        return
    except RuntimeError:
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
