from __future__ import annotations

import asyncio

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from clientplatform.application.partner_runtime import (
    approve_and_queue_partner_email_outreach,
    authorize_partner_telegram_contact,
    get_partner_candidate_view,
    list_partner_campaigns,
    list_partner_candidates,
    list_partner_send_connections,
    partner_stats,
    queue_partner_outreach,
    rerun_connected_partner_campaign,
    set_partner_candidate_status,
    start_connected_partner_campaign,
)
from clientplatform.domain.partners import (
    ContactBasis,
    PartnerChannel,
    PartnerCandidateStatus,
    PartnerInvariantViolation,
)
from clientplatform.domain.tenancy import PlatformRole
from clientplatform.integrations.partner_discovery import PartnerDiscoveryUnavailable
from clientplatform.integrations.partner_discovery_runtime import (
    build_connected_partner_discovery,
)

from . import clientplatform_control as control
from . import clientplatform_simple_experience as simple


class ClientPlatformPartnerGrowthState(StatesGroup):
    telegram_chat_id = State()


_STATUS_LABELS = {
    PartnerCandidateStatus.DISCOVERED: "найден",
    PartnerCandidateStatus.READY: "готов",
    PartnerCandidateStatus.CONTACTED: "написали",
    PartnerCandidateStatus.REPLIED: "ответил",
    PartnerCandidateStatus.ACCEPTED: "сотрудничаем",
    PartnerCandidateStatus.DECLINED: "отказ",
    PartnerCandidateStatus.PAID_ONLY: "только платно",
    PartnerCandidateStatus.DO_NOT_CONTACT: "не писать",
    PartnerCandidateStatus.INVALID: "недоступен",
}
_TERMINAL_CONTACT_STATUSES = {
    PartnerCandidateStatus.DECLINED,
    PartnerCandidateStatus.DO_NOT_CONTACT,
    PartnerCandidateStatus.INVALID,
}


def _token(value: str) -> str:
    return control._uuid_token(value)


async def _actor(callback: CallbackQuery, business_token: str):
    return await control._actor(
        int(callback.from_user.id),
        control._token_uuid(business_token),
    )


async def _render_home(callback: CallbackQuery, business_token: str) -> None:
    actor = await _actor(callback, business_token)
    campaigns, stats, discovery = await asyncio.gather(
        asyncio.to_thread(list_partner_campaigns, actor=actor),
        asyncio.to_thread(partner_stats, actor=actor),
        asyncio.to_thread(build_connected_partner_discovery, actor=actor),
    )
    lines = [
        "🤝 Партнёрства",
        "",
        "ClientPlatform находит публичные сообщества, ранжирует их и готовит персональный материал.",
        "Автоотправка возможна только после подтверждённого согласия или существующего контакта.",
        "",
        f"Кампаний: {stats.campaigns}",
        f"Кандидатов: {stats.candidates}",
        f"Ответили: {stats.replies}",
        f"Сотрудничают: {stats.accepted}",
    ]
    if not discovery.configured:
        lines.extend(
            [
                "",
                "⚠️ Live-поиск не настроен: у бизнеса нет активного VK connection.",
                "Фиктивный результат «0 найдено» в этом состоянии не создаётся.",
            ]
        )
    rows: list[list[tuple[str, str]]] = []
    if discovery.configured:
        rows.append([("🔎 Найти партнёров", f"cpg:start:{business_token}")])
    rows.extend(
        [(f"📂 {item.name[:28]}", f"cpg:p:{business_token}:{_token(item.id)}")]
        for item in campaigns[:8]
    )
    rows.extend(
        [
            [("🔄 Обновить", f"cpg:home:{business_token}")],
            [("⬅️ Получить клиентов", f"cpj:promote:{business_token}")],
            [("🏠 В кабинет", f"cpj:home:{business_token}")],
        ]
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        "\n".join(lines),
        reply_markup=control._keyboard(rows),
    )


async def _render_campaign(
    callback: CallbackQuery,
    *,
    business_token: str,
    campaign_token: str,
    answer_callback: bool = True,
) -> None:
    actor = await _actor(callback, business_token)
    campaign_id = control._token_uuid(campaign_token)
    candidates, stats = await asyncio.gather(
        asyncio.to_thread(
            list_partner_candidates,
            actor=actor,
            campaign_id=campaign_id,
            limit=20,
        ),
        asyncio.to_thread(partner_stats, actor=actor, campaign_id=campaign_id),
    )
    rows = [
        [
            (
                f"{_STATUS_LABELS[item.status]} · {item.name[:24]}",
                f"cpg:c:{business_token}:{_token(item.id)}",
            )
        ]
        for item in candidates[:12]
    ]
    rows.extend(
        [
            [("🔎 Найти ещё", f"cpg:r:{business_token}:{campaign_token}")],
            [("⬅️ Все партнёрства", f"cpg:home:{business_token}")],
        ]
    )
    if answer_callback:
        await callback.answer()
    await control._callback_message(callback).answer(
        "📂 Партнёрская кампания\n\n"
        f"Кандидатов: {stats.candidates}\n"
        f"Написали: {stats.contacted}\n"
        f"Ответили: {stats.replies}\n"
        f"Сотрудничают: {stats.accepted}\n\n"
        "Откройте кандидата — там готовый текст и безопасные действия.",
        reply_markup=control._keyboard(rows),
    )


async def _render_candidate(
    callback: CallbackQuery,
    *,
    business_token: str,
    candidate_token: str,
    answer_callback: bool = True,
) -> None:
    actor = await _actor(callback, business_token)
    view = await asyncio.to_thread(
        get_partner_candidate_view,
        actor=actor,
        candidate_id=control._token_uuid(candidate_token),
    )
    candidate = view.candidate
    channel = getattr(candidate, "channel", PartnerChannel.TELEGRAM)
    send_platform = "email" if channel == PartnerChannel.EMAIL else "telegram"
    connections = await asyncio.to_thread(
        list_partner_send_connections,
        actor=actor,
        platform=send_platform,
    )
    reply = (
        f"\n\n💬 Последний ответ:\n{view.latest_reply[:900]}"
        if view.latest_reply
        else ""
    )
    text = (
        f"🤝 {candidate.name}\n"
        f"Оценка соответствия: {view.fit_total:.1f}/100\n"
        f"Статус: {_STATUS_LABELS[candidate.status]}\n"
        f"Источник: {candidate.source_url or '—'}\n\n"
        f"✉️ Готовое предложение:\n{view.content.outreach_message[:2200]}{reply}"
    )
    rows: list[list[tuple[str, str]]] = []
    if channel == PartnerChannel.EMAIL and connections:
        if (
            candidate.contact_basis == ContactBasis.PUBLIC_BUSINESS_CONTACT
            and actor.role == PlatformRole.OWNER
        ):
            rows.append(
                [
                    (
                        "📧 Подтвердить и поставить Email в очередь",
                        f"cpg:es:{business_token}:{candidate_token}",
                    )
                ]
            )
        elif candidate.first_contact_permitted:
            rows.append(
                [
                    (
                        "📧 Поставить Email в очередь",
                        f"cpg:se:{business_token}:{candidate_token}",
                    )
                ]
            )
    elif channel == PartnerChannel.TELEGRAM:
        if candidate.first_contact_permitted and connections:
            rows.append(
                [
                    (
                        "📨 Поставить в очередь Telegram",
                        f"cpg:s:{business_token}:{candidate_token}",
                    )
                ]
            )
        elif candidate.status not in _TERMINAL_CONTACT_STATUSES:
            rows.extend(
                [
                    [
                        (
                            "✅ Есть согласие на Telegram",
                            f"cpg:a:{business_token}:{candidate_token}:o",
                        )
                    ],
                    [
                        (
                            "🤝 Уже есть деловой контакт",
                            f"cpg:a:{business_token}:{candidate_token}:r",
                        )
                    ],
                ]
            )
    rows.extend(
        [
            [("✅ Сотрудничаем", f"cpg:ok:{business_token}:{candidate_token}")],
            [("🚫 Больше не писать", f"cpg:no:{business_token}:{candidate_token}")],
            [
                (
                    "⬅️ К кампании",
                    f"cpg:p:{business_token}:{_token(candidate.campaign_id)}",
                )
            ],
        ]
    )
    if answer_callback:
        await callback.answer()
    await control._callback_message(callback).answer(
        text[:4000],
        reply_markup=control._keyboard(rows),
    )


async def _queue_selected_connection(
    callback: CallbackQuery,
    *,
    business_token: str,
    candidate_token: str,
    connection_id: str,
    explicit_email_approval: bool = False,
) -> None:
    actor = await _actor(callback, business_token)
    try:
        dispatch = await asyncio.to_thread(
            (
                approve_and_queue_partner_email_outreach
                if explicit_email_approval
                else queue_partner_outreach
            ),
            actor=actor,
            candidate_id=control._token_uuid(candidate_token),
            connection_id=connection_id,
        )
    except PartnerInvariantViolation as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    await callback.answer("Поставлено в очередь")
    await control._callback_message(callback).answer(
        "📨 Предложение поставлено в каноническую очередь отправки.\n\n"
        f"Статус dispatch: {dispatch.status.value}. "
        "Повторное нажатие не создаст дубль.",
        reply_markup=control._keyboard(
            [[("Открыть партнёра", f"cpg:c:{business_token}:{candidate_token}")]]
        ),
    )


@simple.router.callback_query(F.data.startswith("cpg:home:"))
async def open_partner_growth(callback: CallbackQuery) -> None:
    await _render_home(callback, str(callback.data).split(":", 2)[2])


@simple.router.callback_query(F.data.startswith("cpg:start:"))
async def start_partner_growth(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    actor = await _actor(callback, business_token)
    await callback.answer("Ищу и оцениваю…")
    try:
        run = await asyncio.to_thread(start_connected_partner_campaign, actor=actor)
    except PartnerDiscoveryUnavailable:
        await control._callback_message(callback).answer(
            "Live-поиск сейчас недоступен. Проверьте подключение VK и его права. "
            "Кампания с фиктивным нулевым результатом не создана.",
            reply_markup=control._keyboard(
                [[("⬅️ К партнёрствам", f"cpg:home:{business_token}")]]
            ),
        )
        return
    await control._callback_message(callback).answer(
        f"✅ Поиск завершён\n\nНайдено публичных источников: {run.discovered}\n"
        f"Прошли порог качества: {run.prepared}\n\n"
        "Контакты не отправлялись автоматически.",
        reply_markup=control._keyboard(
            [
                [
                    (
                        "Открыть кандидатов",
                        f"cpg:p:{business_token}:{_token(run.campaign.id)}",
                    )
                ]
            ]
        ),
    )


@simple.router.callback_query(F.data.startswith("cpg:p:"))
async def open_partner_campaign(callback: CallbackQuery) -> None:
    _, _, business_token, campaign_token = str(callback.data).split(":", 3)
    await _render_campaign(
        callback,
        business_token=business_token,
        campaign_token=campaign_token,
    )


@simple.router.callback_query(F.data.startswith("cpg:r:"))
async def rerun_partner_growth(callback: CallbackQuery) -> None:
    _, _, business_token, campaign_token = str(callback.data).split(":", 3)
    actor = await _actor(callback, business_token)
    await callback.answer("Обновляю поиск…")
    try:
        await asyncio.to_thread(
            rerun_connected_partner_campaign,
            actor=actor,
            campaign_id=control._token_uuid(campaign_token),
        )
    except PartnerDiscoveryUnavailable:
        await control._callback_message(callback).answer(
            "VK discovery сейчас недоступен; прежние кандидаты сохранены, "
            "ложный ноль не записан."
        )
        return
    await _render_campaign(
        callback,
        business_token=business_token,
        campaign_token=campaign_token,
        answer_callback=False,
    )


@simple.router.callback_query(F.data.startswith("cpg:c:"))
async def open_partner_candidate(callback: CallbackQuery) -> None:
    _, _, business_token, candidate_token = str(callback.data).split(":", 3)
    await _render_candidate(
        callback,
        business_token=business_token,
        candidate_token=candidate_token,
    )


@simple.router.callback_query(F.data.startswith("cpg:a:"))
async def begin_partner_contact_authorization(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    _, _, business_token, candidate_token, raw_basis = str(callback.data).split(
        ":", 4
    )
    basis = "opted_in" if raw_basis == "o" else "existing_relationship"
    await state.clear()
    await state.update_data(
        partner_business_token=business_token,
        partner_candidate_token=candidate_token,
        partner_contact_basis=basis,
    )
    await state.set_state(ClientPlatformPartnerGrowthState.telegram_chat_id)
    await callback.answer()
    await control._callback_message(callback).answer(
        "Введите numeric Telegram chat ID партнёра.\n\n"
        "Используйте этот шаг только если человек действительно дал согласие или "
        "у Вас уже есть деловой контакт. @username и ссылки намеренно не принимаются."
    )


@simple.router.message(ClientPlatformPartnerGrowthState.telegram_chat_id)
async def save_partner_contact(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_token = str(data.get("partner_business_token") or "")
    candidate_token = str(data.get("partner_candidate_token") or "")
    basis = str(data.get("partner_contact_basis") or "")
    if not business_token or not candidate_token:
        await state.clear()
        await message.answer(
            "Контекст партнёра устарел. Откройте «Партнёрства» ещё раз."
        )
        return
    actor = await control._actor(
        int(message.from_user.id),
        control._token_uuid(business_token),
    )
    try:
        await asyncio.to_thread(
            authorize_partner_telegram_contact,
            actor=actor,
            candidate_id=control._token_uuid(candidate_token),
            chat_id=str(message.text or ""),
            basis=ContactBasis(basis),
        )
    except (ValueError, PartnerInvariantViolation):
        await message.answer(
            "Не удалось подтвердить контакт. Нужен numeric Telegram chat ID и "
            "реальное основание: согласие или существующий деловой контакт."
        )
        return
    await state.clear()
    await message.answer(
        "✅ Контакт подтверждён. Автоматическая отправка теперь разрешена "
        "доменным правилом.",
        reply_markup=control._keyboard(
            [[("Открыть партнёра", f"cpg:c:{business_token}:{candidate_token}")]]
        ),
    )


@simple.router.callback_query(F.data.startswith("cpg:es:"))
async def approve_public_email_partner_outreach(callback: CallbackQuery) -> None:
    _, _, business_token, candidate_token = str(callback.data).split(":", 3)
    actor = await _actor(callback, business_token)
    connections = await asyncio.to_thread(
        list_partner_send_connections, actor=actor, platform="email"
    )
    if not connections:
        await callback.answer("Нет активного Email SMTP connection", show_alert=True)
        return
    if len(connections) == 1:
        await _queue_selected_connection(
            callback,
            business_token=business_token,
            candidate_token=candidate_token,
            connection_id=connections[0].id,
            explicit_email_approval=True,
        )
        return
    await callback.answer()
    await control._callback_message(callback).answer(
        "Подтвердите отправителя для этого конкретного публичного B2B email. "
        "Это подтверждение относится только к текущему адресу и текущему тексту.",
        reply_markup=control._keyboard(
            [
                [
                    (
                        connection.label[:36],
                        "cpg:esc:"
                        f"{business_token}:{candidate_token}:{_token(connection.id)}",
                    )
                ]
                for connection in connections[:10]
            ]
            + [[("⬅️ К партнёру", f"cpg:c:{business_token}:{candidate_token}")]],
        ),
    )


@simple.router.callback_query(F.data.startswith("cpg:esc:"))
async def approve_public_email_partner_outreach_via_connection(
    callback: CallbackQuery,
) -> None:
    _, _, business_token, candidate_token, connection_token = str(
        callback.data
    ).split(":", 4)
    await _queue_selected_connection(
        callback,
        business_token=business_token,
        candidate_token=candidate_token,
        connection_id=control._token_uuid(connection_token),
        explicit_email_approval=True,
    )


@simple.router.callback_query(F.data.startswith("cpg:s:"))
async def send_partner_outreach(callback: CallbackQuery) -> None:
    _, _, business_token, candidate_token = str(callback.data).split(":", 3)
    actor = await _actor(callback, business_token)
    connections = await asyncio.to_thread(list_partner_send_connections, actor=actor)
    if not connections:
        await callback.answer("Нет активного Telegram bot connection", show_alert=True)
        return
    if len(connections) == 1:
        await _queue_selected_connection(
            callback,
            business_token=business_token,
            candidate_token=candidate_token,
            connection_id=connections[0].id,
        )
        return

    await callback.answer()
    await control._callback_message(callback).answer(
        "Выберите подключение, от имени которого отправить предложение. "
        "ClientPlatform не выбирает его автоматически, когда подключено несколько.",
        reply_markup=control._keyboard(
            [
                [
                    (
                        connection.label[:36],
                        "cpg:sc:"
                        f"{business_token}:{candidate_token}:{_token(connection.id)}",
                    )
                ]
                for connection in connections[:10]
            ]
            + [[("⬅️ К партнёру", f"cpg:c:{business_token}:{candidate_token}")]],
        ),
    )


@simple.router.callback_query(F.data.startswith("cpg:se:"))
async def send_partner_email_outreach(callback: CallbackQuery) -> None:
    _, _, business_token, candidate_token = str(callback.data).split(":", 3)
    actor = await _actor(callback, business_token)
    connections = await asyncio.to_thread(
        list_partner_send_connections, actor=actor, platform="email"
    )
    if not connections:
        await callback.answer("Нет активного Email SMTP connection", show_alert=True)
        return
    if len(connections) == 1:
        await _queue_selected_connection(
            callback,
            business_token=business_token,
            candidate_token=candidate_token,
            connection_id=connections[0].id,
        )
        return

    await callback.answer()
    await control._callback_message(callback).answer(
        "Выберите Email SMTP подключение, от имени которого отправить предложение. "
        "ClientPlatform не выбирает его автоматически, когда подключено несколько.",
        reply_markup=control._keyboard(
            [
                [
                    (
                        connection.label[:36],
                        "cpg:sc:"
                        f"{business_token}:{candidate_token}:{_token(connection.id)}",
                    )
                ]
                for connection in connections[:10]
            ]
            + [[("⬅️ К партнёру", f"cpg:c:{business_token}:{candidate_token}")]],
        ),
    )


@simple.router.callback_query(F.data.startswith("cpg:sc:"))
async def send_partner_outreach_via_connection(callback: CallbackQuery) -> None:
    _, _, business_token, candidate_token, connection_token = str(
        callback.data
    ).split(":", 4)
    await _queue_selected_connection(
        callback,
        business_token=business_token,
        candidate_token=candidate_token,
        connection_id=control._token_uuid(connection_token),
    )


@simple.router.callback_query(F.data.startswith("cpg:ok:"))
async def accept_partner(callback: CallbackQuery) -> None:
    _, _, business_token, candidate_token = str(callback.data).split(":", 3)
    actor = await _actor(callback, business_token)
    await asyncio.to_thread(
        set_partner_candidate_status,
        actor=actor,
        candidate_id=control._token_uuid(candidate_token),
        status=PartnerCandidateStatus.ACCEPTED,
    )
    await _render_candidate(
        callback,
        business_token=business_token,
        candidate_token=candidate_token,
    )


@simple.router.callback_query(F.data.startswith("cpg:no:"))
async def do_not_contact_partner(callback: CallbackQuery) -> None:
    _, _, business_token, candidate_token = str(callback.data).split(":", 3)
    actor = await _actor(callback, business_token)
    await asyncio.to_thread(
        set_partner_candidate_status,
        actor=actor,
        candidate_id=control._token_uuid(candidate_token),
        status=PartnerCandidateStatus.DO_NOT_CONTACT,
    )
    await _render_candidate(
        callback,
        business_token=business_token,
        candidate_token=candidate_token,
    )


__all__ = [
    "ClientPlatformPartnerGrowthState",
    "open_partner_growth",
]
