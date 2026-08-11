from __future__ import annotations

import asyncio
import hashlib
import os

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from clientplatform.application.ad_connections import (
    ad_connections_enabled,
    confirm_ad_publication,
    create_ad_publication_draft,
    list_ad_connections,
    list_ad_publications,
    list_yandex_direct_campaigns,
    start_yandex_direct_oauth,
    yandex_direct_provider_configured,
)
from clientplatform.application.promotions import (
    create_slot_promotion,
    promotion_start_payload,
)
from clientplatform.application.visual_creatives import (
    VisualCreativeError,
    create_ad_visual,
    materialize_ad_visual,
    poll_ad_visual,
)
from clientplatform.domain.ad_connections import (
    AdConnectionError,
    AdConnectionStatus,
    AdPublicationStatus,
    normalize_region_ids,
)
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.promotions import PromotionChannel, PromotionError
from clientplatform.integrations.yandex_direct import YandexDirectError

from . import clientplatform_control as control
from . import clientplatform_simple_experience as simple


class AdConnectionState(StatesGroup):
    selecting_connection = State()
    selecting_campaign = State()
    waiting_regions = State()
    confirming_publication = State()


_STATUS_LABELS = {
    AdConnectionStatus.PENDING: "⏳ подключается",
    AdConnectionStatus.ACTIVE: "✅ подключён",
    AdConnectionStatus.ATTENTION: "⚠️ требует внимания",
    AdConnectionStatus.DISABLED: "⏸ отключён",
    AdConnectionStatus.REVOKED: "⛔ доступ отозван",
}
_JOB_LABELS = {
    AdPublicationStatus.DRAFT: "черновик ClientPlatform",
    AdPublicationStatus.QUEUED: "готовится черновик в Яндексе",
    AdPublicationStatus.PUBLISHING: "создаётся черновик в Яндексе",
    AdPublicationStatus.RETRY: "повторная попытка создания черновика",
    AdPublicationStatus.SUBMITTED: "черновик создан в Яндексе",
    AdPublicationStatus.FAILED: "ошибка",
    AdPublicationStatus.CANCELLED: "отменено",
}
_CONFIRM_DRAFT_LABEL = "✅ Создать черновик в Яндекс Директе"


def _message(callback: CallbackQuery) -> Message:
    return control._callback_message(callback)


async def _bot_username(event: CallbackQuery | Message) -> str:
    bot = await event.bot.get_me()
    username = str(getattr(bot, "username", "") or "").strip()
    if not username:
        raise RuntimeError("ClientPlatform bot requires a public username")
    return username


def _promotion_link(username: str, source_token: str) -> str:
    return f"https://t.me/{username}?start={promotion_start_payload(source_token)}"


def _active_connections(connections):
    return [item for item in connections if item.status == AdConnectionStatus.ACTIVE]


def _visual_wait_seconds() -> int:
    raw = str(os.getenv("VISUAL_TELEGRAM_WAIT_SECONDS", "20") or "20").strip()
    try:
        value = int(raw)
    except ValueError:
        return 20
    return max(0, min(value, 60))


async def _workspace(callback: CallbackQuery, *, business_token: str) -> None:
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    if not ad_connections_enabled() or not yandex_direct_provider_configured():
        await callback.answer()
        await _message(callback).answer(
            "📣 Личные рекламные кабинеты\n\n"
            "Интеграция подготовлена, но OAuth-приложение Яндекс Директа ещё не "
            "включено владельцем ClientPlatform. До включения реклама продолжает "
            "работать через готовые тексты и измеряемые ссылки.",
            reply_markup=control._keyboard(
                [[("⬅️ К клиентам", f"cpj:promote:{business_token}")]]
            ),
        )
        return

    connections, jobs = await asyncio.gather(
        asyncio.to_thread(list_ad_connections, actor=actor),
        asyncio.to_thread(list_ad_publications, actor=actor),
    )
    active = _active_connections(connections)
    connection_lines = [
        f"• Яндекс Директ · {item.external_login} · {_STATUS_LABELS[item.status]}"
        for item in connections
    ] or ["• рекламный кабинет пока не подключён"]
    job_lines = [
        f"• {item.external_campaign_name or item.external_campaign_id}: "
        f"{_JOB_LABELS[item.status]}"
        for item in jobs[:5]
    ] or ["• отправок пока нет"]

    rows: list[list[tuple[str, str]]] = []
    if active:
        rows.extend(
            [
                [("🎯 Создать рекламу", f"cpa:promote:{business_token}")],
                [("🔌 Отключить кабинет", f"cpa:disconnects:{business_token}")],
            ]
        )
    else:
        rows.append(
            [("➕ Подключить Яндекс Директ", f"cpa:connect:{business_token}")]
        )
    rows.extend(
        [
            [("🔄 Обновить", f"cpa:home:{business_token}")],
            [("⬅️ Получить клиентов", f"cpj:promote:{business_token}")],
        ]
    )

    next_step = (
        "\n\nКабинет готов. Нажмите «🎯 Создать рекламу». Экран «Выберите свободное время» "
        "откроется отдельным шагом."
        if active
        else (
            "\n\nПодключите Яндекс Директ, чтобы создавать рекламные черновики. "
            "Сначала опубликуйте свободное время в разделе «Запись», если хотите "
            "рекламировать конкретное окно."
        )
    )
    await callback.answer()
    await _message(callback).answer(
        "📣 Личные рекламные кабинеты\n\n"
        "Здесь только управление подключением к Яндекс Директу. ClientPlatform не "
        "получает доступ к кабинетам других пользователей. "
        "Показы и расходы автоматически не запускаются.\n\n"
        "Подключения:\n"
        + "\n".join(connection_lines)
        + "\n\nПоследние рекламные черновики:\n"
        + "\n".join(job_lines)
        + next_step,
        reply_markup=control._keyboard(rows),
    )


@simple.router.callback_query(F.data.startswith("cpa:home:"))
async def open_ad_connections(callback: CallbackQuery) -> None:
    await _workspace(
        callback,
        business_token=str(callback.data).split(":", 2)[2],
    )


@simple.router.callback_query(F.data.startswith("cpa:promote:"))
async def open_ad_promotion_slots(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    connections, slots = await asyncio.gather(
        asyncio.to_thread(list_ad_connections, actor=actor),
        asyncio.to_thread(control.list_booking_slots, actor=actor),
    )
    if not _active_connections(connections):
        await callback.answer(
            "Сначала подключите рекламный кабинет",
            show_alert=True,
        )
        return

    open_slots = [item for item in slots if item.slot.status == BookingSlotStatus.OPEN]
    rows: list[list[tuple[str, str]]] = [
        [
            (
                f"🎯 {slot.local_start} · {slot.offering_title[:20]}",
                f"cpa:slot:{business_token}:{control._uuid_token(slot.slot.id)}",
            )
        ]
        for slot in open_slots[:10]
    ]
    rows.append([("⬅️ К рекламному кабинету", f"cpa:home:{business_token}")])

    await callback.answer()
    if not open_slots:
        await _message(callback).answer(
            "🎯 Создать рекламу\n\n"
            "Сейчас нет свободного времени, которое можно превратить в рекламный "
            "черновик. Сначала откройте время для записи в разделе «Запись», затем "
            "вернитесь сюда.",
            reply_markup=control._keyboard(rows),
        )
        return

    await _message(callback).answer(
        "🎯 Создать рекламу\n\n"
        "Выберите, какое свободное время рекламировать. Следующим шагом ClientPlatform "
        "предложит кабинет, кампанию и регион, а перед созданием покажет полный "
        "черновик для подтверждения.\n\n"
        "Свободное время:",
        reply_markup=control._keyboard(rows),
    )


async def connect_yandex_direct(callback: CallbackQuery) -> None:
    """Legacy callback-flow helper retained unregistered for compatibility tests."""

    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        start = await asyncio.to_thread(start_yandex_direct_oauth, actor=actor)
    except (AdConnectionError, YandexDirectError, RuntimeError, ValueError):
        await callback.answer("Не удалось начать подключение", show_alert=True)
        return
    await callback.answer()
    await _message(callback).answer(
        "🔐 Подключение Яндекс Директа\n\n"
        "Откроется официальный экран Яндекса. Выберите нужный аккаунт и разрешите "
        "доступ. Пароль ClientPlatform не получает. Ссылка действует 10 минут.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть Яндекс и подключить кабинет",
                        url=start.authorization_url,
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Вернуться",
                        callback_data=f"cpa:home:{business_token}",
                    )
                ],
            ]
        ),
    )


@simple.router.callback_query(F.data.startswith("cpa:slot:"))
async def choose_ad_connection(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    _, _, business_token, slot_token = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    slot_id = control._token_uuid(slot_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        connections = await asyncio.to_thread(list_ad_connections, actor=actor)
        active = _active_connections(connections)
        if not active:
            await callback.answer(
                "Сначала подключите рекламный кабинет",
                show_alert=True,
            )
            return
        view = await asyncio.to_thread(
            create_slot_promotion,
            actor=actor,
            slot_id=slot_id,
            channel=PromotionChannel.WEBSITE,
        )
        username = await _bot_username(callback)
    except (AdConnectionError, PromotionError, RuntimeError, ValueError):
        await callback.answer("Не удалось подготовить объявление", show_alert=True)
        return
    await state.set_state(AdConnectionState.selecting_connection)
    await state.set_data(
        {
            "business_id": business_id,
            "business_token": business_token,
            "promotion_campaign_id": view.campaign.id,
            "source_url": _promotion_link(username, view.campaign.source_token),
            "connection_ids": [item.id for item in active],
        }
    )
    rows = [
        [(f"Яндекс · {item.external_login}", f"cpa:conn:{index}")]
        for index, item in enumerate(active)
    ]
    rows.append([("Отмена", f"cpa:home:{business_token}")])
    await callback.answer()
    await _message(callback).answer(
        "Какой личный рекламный кабинет использовать?",
        reply_markup=control._keyboard(rows),
    )


@simple.router.callback_query(
    AdConnectionState.selecting_connection,
    F.data.startswith("cpa:conn:"),
)
async def choose_yandex_campaign(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await callback.answer("Загружаю кампании…")
    data = await state.get_data()
    try:
        index = int(str(callback.data).split(":", 2)[2])
        connection_id = list(data["connection_ids"])[index]
        actor = await control._actor(
            int(callback.from_user.id),
            str(data["business_id"]),
        )
        campaigns = await asyncio.wait_for(
            asyncio.to_thread(
                list_yandex_direct_campaigns,
                actor=actor,
                connection_id=connection_id,
            ),
            timeout=25.0,
        )
    except (
        asyncio.TimeoutError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        AdConnectionError,
        YandexDirectError,
    ):
        rows = [[("🔄 Повторить", str(callback.data or "cpa:conn:0"))]]
        business_token = str(data.get("business_token") or "").strip()
        if business_token:
            rows.append(
                [("⬅️ К рекламному кабинету", f"cpa:home:{business_token}")]
            )
        await _message(callback).answer(
            "Не удалось получить кампании Яндекса. Попробуйте ещё раз. "
            "Если ошибка повторится, обновите подключение рекламного кабинета.",
            reply_markup=control._keyboard(rows),
        )
        return
    eligible = [item for item in campaigns if item.state != "ARCHIVED"][:20]
    if not eligible:
        business_token = str(data.get("business_token") or "").strip()
        rows = []
        if business_token:
            rows.append(
                [("⬅️ К рекламному кабинету", f"cpa:home:{business_token}")]
            )
        await _message(callback).answer(
            "В кабинете нет подходящей активной текстовой кампании.",
            reply_markup=control._keyboard(rows),
        )
        return
    await state.update_data(
        connection_id=connection_id,
        yandex_campaigns=[
            {"id": item.campaign_id, "name": item.name} for item in eligible
        ],
    )
    await state.set_state(AdConnectionState.selecting_campaign)
    rows = [
        [(item.name[:45], f"cpa:campaign:{index}")]
        for index, item in enumerate(eligible)
    ]
    rows.append([("Отмена", f"cpa:home:{data['business_token']}")])
    await _message(callback).answer(
        "В какой существующей кампании создать рекламный черновик?\n\n"
        "ClientPlatform не меняет бюджет и стратегию кампании и не отправляет "
        "черновик на модерацию автоматически.",
        reply_markup=control._keyboard(rows),
    )


@simple.router.callback_query(
    AdConnectionState.selecting_campaign,
    F.data.startswith("cpa:campaign:"),
)
async def request_ad_regions(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        index = int(str(callback.data).split(":", 2)[2])
        selected = list(data["yandex_campaigns"])[index]
    except (IndexError, KeyError, TypeError, ValueError):
        await callback.answer("Кампания больше не найдена", show_alert=True)
        return
    await state.update_data(
        external_campaign_id=str(selected["id"]),
        external_campaign_name=str(selected["name"]),
    )
    await state.set_state(AdConnectionState.waiting_regions)
    await callback.answer()
    await _message(callback).answer(
        "Укажите регион показа — один или несколько ID через запятую.\n\n"
        "Частые варианты:\n"
        "• Нижний Новгород — 47\n"
        "• Москва — 213\n"
        "• Санкт-Петербург — 2\n\n"
        "Показы по всей стране автоматически не включаются: география должна быть "
        "задана явно."
    )


@simple.router.message(AdConnectionState.waiting_regions)
async def prepare_ad_publication(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        regions = normalize_region_ids(str(message.text or ""))
        actor = await control._actor(
            control._user_id(message),
            str(data["business_id"]),
        )
        draft = await asyncio.to_thread(
            create_ad_publication_draft,
            actor=actor,
            promotion_campaign_id=str(data["promotion_campaign_id"]),
            connection_id=str(data["connection_id"]),
            external_campaign_id=str(data["external_campaign_id"]),
            external_campaign_name=str(data["external_campaign_name"]),
            region_ids=regions,
            source_url=str(data["source_url"]),
        )
    except (KeyError, TypeError, ValueError, AdConnectionError):
        await message.answer(
            "Не удалось распознать регион. Введите положительный ID, например 47, "
            "или несколько ID через запятую."
        )
        return
    await state.update_data(
        job_id=draft.job.id,
        creative_title=draft.job.title,
        creative_body=draft.job.text,
        creative_job_id="",
    )
    await state.set_state(AdConnectionState.confirming_publication)
    await message.answer(
        "Проверьте рекламный черновик:\n\n"
        f"Кампания: {draft.campaign_name}\n"
        f"Регионы: {', '.join(str(item) for item in draft.job.region_ids)}\n"
        f"Заголовок: {draft.job.title}\n"
        f"Текст: {draft.job.text}\n"
        f"Ссылка: {draft.job.source_url}\n\n"
        "После подтверждения ClientPlatform создаст группу и объявление со статусом "
        "DRAFT в Вашем кабинете. Показов, модерации и расходов автоматически не будет.",
        reply_markup=control._keyboard(
            [
                [("🖼 Создать картинку", "cpa:creative:image")],
                [("🎬 Создать видео", "cpa:creative:video")],
                [(_CONFIRM_DRAFT_LABEL, "cpa:confirm")],
                [("Отмена", f"cpa:home:{data['business_token']}")],
            ]
        ),
    )


async def _render_ad_visual(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    kind: str,
) -> None:
    data = await state.get_data()
    if str(data.get("creative_job_id") or "").strip():
        await callback.answer(
            "Предыдущий визуал ещё генерируется. Сначала проверьте его или "
            "продолжите без визуала.",
            show_alert=True,
        )
        return
    try:
        business_id = str(data["business_id"])
        publication_job_id = str(data["job_id"])
        idempotency_key = "clientplatform:" + hashlib.sha256(
            f"{business_id}|{publication_job_id}|{kind}".encode("utf-8")
        ).hexdigest()
        job = await asyncio.to_thread(
            create_ad_visual,
            title=str(data.get("creative_title") or ""),
            body=str(data.get("creative_body") or ""),
            kind=kind,
            scope_id=business_id,
            idempotency_key=idempotency_key,
            country_code=os.getenv("VISUAL_DEPLOYMENT_COUNTRY", ""),
            wait_seconds=_visual_wait_seconds(),
        )
    except KeyError:
        await callback.answer("Не удалось подготовить визуал", show_alert=True)
        return
    except TypeError:
        await callback.answer("Не удалось подготовить визуал", show_alert=True)
        return
    except ValueError:
        await callback.answer("Не удалось подготовить визуал", show_alert=True)
        return
    except VisualCreativeError:
        await callback.answer("Не удалось подготовить визуал", show_alert=True)
        return

    await callback.answer()
    target = _message(callback)
    if job.status == "succeeded" and job.asset_ready:
        try:
            path = await asyncio.to_thread(materialize_ad_visual, job)
        except VisualCreativeError:
            await state.update_data(creative_job_id="")
            await target.answer(
                "Визуал создан, но не удалось безопасно получить файл. "
                "Текстовый черновик сохранён."
            )
            return
        await state.update_data(creative_job_id="")
        caption = (
            f"Готовый рекламный визуал · {job.provider} · {job.model or 'default'}"
        )
        if job.kind == "video":
            await target.answer_video(FSInputFile(path), caption=caption)
        else:
            await target.answer_photo(FSInputFile(path), caption=caption)
        await target.answer(
            "Визуал готов. Текущий Yandex Direct-контур создаёт текстовый DRAFT; "
            "файл визуала пока остаётся отдельным материалом для владельца.",
            reply_markup=control._keyboard(
                [[(_CONFIRM_DRAFT_LABEL, "cpa:confirm")]]
            ),
        )
        return
    if job.status in {"queued", "running"} and job.id:
        await state.update_data(creative_job_id=job.id)
        await target.answer(
            "Визуал ещё генерируется. Дождитесь результата или явно продолжите "
            "без визуала.",
            reply_markup=control._keyboard(
                [
                    [("🔄 Проверить визуал", "cpa:creative:refresh")],
                    [("➡️ Продолжить без визуала", "cpa:creative:skip")],
                ]
            ),
        )
        return
    await state.update_data(creative_job_id="")
    await target.answer(
        "Визуал сейчас недоступен. Текстовый рекламный черновик остаётся готовым; "
        "провайдер изображений/видео переключается конфигурацией."
    )


@simple.router.callback_query(
    AdConnectionState.confirming_publication,
    F.data.in_({"cpa:creative:image", "cpa:creative:video"}),
)
async def generate_ad_visual(callback: CallbackQuery, state: FSMContext) -> None:
    kind = "video" if str(callback.data).endswith(":video") else "image"
    await _render_ad_visual(callback, state, kind=kind)


@simple.router.callback_query(
    AdConnectionState.confirming_publication,
    F.data == "cpa:creative:refresh",
)
async def refresh_ad_visual(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        job = await asyncio.to_thread(
            poll_ad_visual,
            job_id=str(data["creative_job_id"]),
            scope_id=str(data["business_id"]),
        )
    except KeyError:
        await callback.answer("Не удалось проверить визуал", show_alert=True)
        return
    except TypeError:
        await callback.answer("Не удалось проверить визуал", show_alert=True)
        return
    except ValueError:
        await callback.answer("Не удалось проверить визуал", show_alert=True)
        return
    except VisualCreativeError:
        await callback.answer("Не удалось проверить визуал", show_alert=True)
        return
    await callback.answer()
    target = _message(callback)
    if job.status == "succeeded" and job.asset_ready:
        try:
            path = await asyncio.to_thread(materialize_ad_visual, job)
        except VisualCreativeError:
            await state.update_data(creative_job_id="")
            await target.answer(
                "Визуал создан, но файл получить не удалось. Можно продолжить с "
                "текстовым черновиком."
            )
            return
        await state.update_data(creative_job_id="")
        caption = (
            f"Готовый рекламный визуал · {job.provider} · {job.model or 'default'}"
        )
        if job.kind == "video":
            await target.answer_video(FSInputFile(path), caption=caption)
        else:
            await target.answer_photo(FSInputFile(path), caption=caption)
        await target.answer(
            "Визуал готов. Он не прикрепляется к Yandex Direct автоматически этим "
            "контуром.",
            reply_markup=control._keyboard(
                [[(_CONFIRM_DRAFT_LABEL, "cpa:confirm")]]
            ),
        )
    elif job.status in {"queued", "running"}:
        await target.answer(
            "Визуал ещё генерируется.",
            reply_markup=control._keyboard(
                [
                    [("🔄 Проверить визуал", "cpa:creative:refresh")],
                    [("➡️ Продолжить без визуала", "cpa:creative:skip")],
                ]
            ),
        )
    else:
        await state.update_data(creative_job_id="")
        await target.answer(
            "Генерация визуала завершилась ошибкой; текстовый рекламный черновик "
            "сохранён."
        )


@simple.router.callback_query(
    AdConnectionState.confirming_publication,
    F.data == "cpa:creative:skip",
)
async def skip_ad_visual(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(creative_job_id="")
    await confirm_yandex_publication(callback, state)


@simple.router.callback_query(
    AdConnectionState.confirming_publication,
    F.data == "cpa:confirm",
)
async def confirm_yandex_publication(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    if str(data.get("creative_job_id") or "").strip():
        await callback.answer(
            "Визуал ещё генерируется. Сначала проверьте его или выберите "
            "«Продолжить без визуала».",
            show_alert=True,
        )
        return
    try:
        actor = await control._actor(
            int(callback.from_user.id),
            str(data["business_id"]),
        )
        job = await asyncio.to_thread(
            confirm_ad_publication,
            actor=actor,
            job_id=str(data["job_id"]),
        )
    except (KeyError, AdConnectionError, RuntimeError, ValueError):
        await callback.answer(
            "Не удалось поставить черновик в очередь",
            show_alert=True,
        )
        return
    await state.clear()
    await callback.answer("Черновик принят")
    await _message(callback).answer(
        "✅ Рекламный черновик поставлен в защищённую очередь\n\n"
        f"Статус: {_JOB_LABELS[job.status]}\n"
        "ClientPlatform создаст его в личном кабинете идемпотентно: повторное "
        "нажатие не создаст дубликат. Чтобы начались показы и расходы, черновик "
        "нужно отдельно проверить и запустить в Яндекс Директе.",
        reply_markup=control._keyboard(
            [
                [
                    (
                        "📣 Открыть рекламные кабинеты",
                        f"cpa:home:{data['business_token']}",
                    )
                ]
            ]
        ),
    )


__all__ = [
    "AdConnectionState",
    "confirm_yandex_publication",
    "connect_yandex_direct",
    "generate_ad_visual",
    "open_ad_connections",
    "open_ad_promotion_slots",
    "prepare_ad_publication",
    "refresh_ad_visual",
    "skip_ad_visual",
]
