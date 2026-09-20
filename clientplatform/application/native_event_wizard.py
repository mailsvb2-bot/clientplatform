from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from clientplatform.application.event_content_plans import set_event_content_mode
from clientplatform.application.event_owner_flow import (
    MultiSessionOnlineEventCreateRequest,
    OnlineEventSessionCreateRequest,
    append_multisession_online_event_draft_session,
    create_multisession_online_event_draft,
    publish_multisession_online_event_draft,
)
from clientplatform.application.event_sessions import (
    get_event_warmup_window,
    list_event_sessions,
)
from clientplatform.application.event_warmups import (
    get_saved_event_warmup_plan,
    reset_event_warmup_text,
    save_event_warmup_plan,
    set_event_warmup_text,
)
from clientplatform.application.event_wizard import (
    normalize_event_timezone,
    normalize_session_join_url,
    parse_session_count,
    parse_session_window,
)
from clientplatform.application.owner_input import (
    begin_owner_input,
    clear_owner_input,
    get_owner_input_session,
)
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from clientplatform.domain.event_content import (
    EventContentMode,
    EventContentStage,
    event_content_mode_label,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.presentation.event_schedule_picker import (
    QUICK_DURATIONS,
    QUICK_START_TIMES,
    WEBINAR_VENUES,
    local_today,
    parse_quick_duration,
    parse_quick_time,
    session_window_text,
    webinar_venue,
)
from config.settings import settings


_DATE_PAGE_SIZE = 7
_MAX_DATE_DAYS = 550
_MONTHS_RU = (
    "",
    "янв",
    "фев",
    "мар",
    "апр",
    "май",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
)


def _button(label: str, command: str) -> CustomerInteractionButton:
    return CustomerInteractionButton(label=label[:40], command=command)


def _back_row() -> tuple[CustomerInteractionButton, ...]:
    return (_button("🎥 К вебинарам", "cpm:events"),)


def _session(
    actor: TenantContext,
    *,
    platform: ConnectionPlatform,
    surface: str,
):
    current = get_owner_input_session(
        user_id=actor.user_id,
        platform=platform.value,
        surface=surface,
    )
    if (
        current is None
        or current.business_id != actor.business_id
        or current.action != "online_event"
    ):
        raise ValueError("webinar wizard session is unavailable")
    return current


def _context(
    actor: TenantContext,
    *,
    platform: ConnectionPlatform,
    surface: str,
) -> dict[str, str]:
    return dict(_session(actor, platform=platform, surface=surface).context)


def _save(
    actor: TenantContext,
    *,
    platform: ConnectionPlatform,
    surface: str,
    context: dict[str, object],
) -> None:
    begin_owner_input(
        actor=actor,
        platform=platform.value,
        surface=surface,
        action="online_event",
        context=context,
    )


def _update(
    actor: TenantContext,
    *,
    platform: ConnectionPlatform,
    surface: str,
    **changes: object,
) -> dict[str, str]:
    context = _context(actor, platform=platform, surface=surface)
    context.update({key: str(value) for key, value in changes.items()})
    _save(actor, platform=platform, surface=surface, context=context)
    return {key: str(value) for key, value in context.items()}


def _date_label(value: date) -> str:
    return f"{value.day} {_MONTHS_RU[value.month]}"


def _date_picker_message(context: dict[str, str], *, offset: int = 0) -> CustomerInteractionMessage:
    timezone_name = context["timezone"]
    minimum = date.fromisoformat(context.get("min_date") or local_today(timezone_name).isoformat())
    start = minimum + timedelta(days=max(0, offset))
    if (start - minimum).days > _MAX_DATE_DAYS:
        raise ValueError("date page is outside the supported range")
    values = tuple(
        start + timedelta(days=index)
        for index in range(_DATE_PAGE_SIZE)
        if offset + index <= _MAX_DATE_DAYS
    )
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    for index in range(0, len(values), 2):
        rows.append(
            tuple(
                _button(
                    _date_label(item),
                    f"cpm:event-wizard:date:{item.isoformat()}",
                )
                for item in values[index : index + 2]
            )
        )
    navigation: list[CustomerInteractionButton] = []
    if offset > 0:
        navigation.append(
            _button("⬅️ Раньше", f"cpm:event-wizard:date-page:{max(0, offset - _DATE_PAGE_SIZE)}")
        )
    if offset + _DATE_PAGE_SIZE <= _MAX_DATE_DAYS:
        navigation.append(
            _button("Позже ➡️", f"cpm:event-wizard:date-page:{offset + _DATE_PAGE_SIZE}")
        )
    if navigation:
        rows.append(tuple(navigation))
    rows.append(_back_row())
    position = int(context.get("position") or "1")
    total = int(context.get("count") or "1")
    return CustomerInteractionMessage(
        text=(
            f"День {position} из {total}: выберите дату.\n"
            f"Часовой пояс: {timezone_name}.\n\n"
            "Показываю по 7 дат — листайте кнопками, вводить дату вручную не нужно."
        ),
        rows=tuple(rows),
    )


def _start_time_message(selected: date) -> CustomerInteractionMessage:
    rows = [
        tuple(
            _button(value, f"cpm:event-wizard:start:{value.replace(':', '')}")
            for value in QUICK_START_TIMES[index : index + 3]
        )
        for index in range(0, len(QUICK_START_TIMES), 3)
    ]
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=f"Дата: {selected.strftime('%d.%m.%Y')}. Во сколько начинаем?",
        rows=tuple(rows),
    )


def _duration_message(start_time: str) -> CustomerInteractionMessage:
    labels = {30: "30 мин", 60: "1 час", 90: "1,5 часа", 120: "2 часа", 180: "3 часа"}
    rows = [
        tuple(
            _button(labels[value], f"cpm:event-wizard:duration:{value}")
            for value in QUICK_DURATIONS[index : index + 3]
        )
        for index in range(0, len(QUICK_DURATIONS), 3)
    ]
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=f"Начало: {start_time}. Сколько длится эфир?",
        rows=tuple(rows),
    )


def _timezone_message() -> CustomerInteractionMessage:
    return CustomerInteractionMessage(
        text="По какому времени идут эфиры?",
        rows=(
            (_button("🕒 Москва", "cpm:event-wizard:timezone:moscow"),),
            (_button("🌍 Другое время", "cpm:event-wizard:timezone:other"),),
            _back_row(),
        ),
    )


def _topics_choice_message(count: int) -> CustomerInteractionMessage:
    noun = "дня" if count in {2, 3, 4} else "дней"
    return CustomerInteractionMessage(
        text=(
            f"У каждого из {count} {noun} есть своё название темы?\n\n"
            "Если да — ClientPlatform попросит названия и будет использовать их "
            "в сообщениях до вебинара."
        ),
        rows=(
            (
                _button("Да, есть темы", "cpm:event-wizard:topics:yes"),
                _button("Нет, тема общая", "cpm:event-wizard:topics:no"),
            ),
            _back_row(),
        ),
    )


def _event_topics(context: dict[str, str]) -> tuple[str, ...]:
    raw = str(context.get("topics_json") or "").strip()
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid event topic context") from exc
    if not isinstance(value, list):
        raise ValueError("invalid event topic context")
    topics = tuple(" ".join(str(item).split()).strip() for item in value)
    if any(not topic or len(topic) > 180 for topic in topics):
        raise ValueError("invalid event topic context")
    return topics


def _event_description(context: dict[str, str]) -> str:
    topics = _event_topics(context)
    if not topics:
        return ""
    return "Программа по дням:\n" + "\n".join(
        f"День {index}: {topic}"
        for index, topic in enumerate(topics, start=1)
    )



def _venue_message() -> CustomerInteractionMessage:
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    for index in range(0, len(WEBINAR_VENUES), 2):
        rows.append(
            tuple(
                _button(item.label, f"cpm:event-wizard:venue:{item.key}")
                for item in WEBINAR_VENUES[index : index + 2]
            )
        )
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            "Где будете проводить вебинар?\n\n"
            "Выберите площадку один раз. Для каждого дня ClientPlatform затем даст кнопку "
            "«Открыть площадку», чтобы создать комнату с минимумом действий."
        ),
        rows=tuple(rows),
    )


def _room_message(context: dict[str, str]) -> CustomerInteractionMessage:
    venue = webinar_venue(context["venue"])
    position = int(context["position"])
    total = int(context["count"])
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if venue.open_url:
        rows.append(
            (_button(f"↗️ Открыть {venue.label}", f"cpm:event-venue-open:{venue.key}"),)
        )
    rows.append(_back_row())
    later = (
        "Если ссылка появится позже — отправьте «-»."
        if total == 1
        else "Для многодневного мероприятия у каждого дня должна быть своя комната."
    )
    open_note = (
        f"Нажмите «Открыть {venue.label}», создайте комнату, вернитесь сюда и вставьте скопированную HTTPS-ссылку."
        if venue.open_url
        else "Создайте комнату в выбранном сервисе и вставьте её HTTPS-ссылку."
    )
    return CustomerInteractionMessage(
        text=f"Комната для дня {position}.\n\n{open_note}\n{later}",
        rows=tuple(rows),
    )


def _warmup_message(maximum: int) -> CustomerInteractionMessage:
    quick = [value for value in (0, 1, 2, 3, 5, 7) if value <= maximum]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if quick:
        for index in range(0, len(quick), 3):
            rows.append(
                tuple(
                    _button(str(value), f"cpm:event-wizard:warmup:{value}")
                    for value in quick[index : index + 3]
                )
            )
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            f"До первого дня можно отправлять сообщения до {maximum} дн.\n\n"
            "Нажмите готовое число или отправьте нужное число сообщением. 0 — без подготовительных сообщений."
        ),
        rows=tuple(rows),
    )


def _mode_message(stage: EventContentStage) -> CustomerInteractionMessage:
    labels = {
        EventContentStage.WARMUP: "сообщения до вебинара",
        EventContentStage.EVENT_DAY: "анонс в день мероприятия",
        EventContentStage.POST_EVENT: "дожим после мероприятия",
    }
    return CustomerInteractionMessage(
        text=(
            f"Как оформить {labels[stage]}?\n\n"
            "1) Только текст\n"
            "2) Текст + картинка\n"
            "3) Текст в тематической картинке\n"
            "4) Текст + видео"
        ),
        rows=(
            (_button("1 · Только текст", f"cpm:event-wizard:mode:{stage.value}:text"),),
            (_button("2 · Текст + картинка", f"cpm:event-wizard:mode:{stage.value}:text_with_image"),),
            (_button("3 · Текст в картинке", f"cpm:event-wizard:mode:{stage.value}:text_in_image"),),
            (_button("4 · Текст + видео", f"cpm:event-wizard:mode:{stage.value}:text_with_video"),),
            _back_row(),
        ),
    )


def begin_native_event_wizard(
    actor: TenantContext,
    *,
    platform: ConnectionPlatform,
    surface: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    _save(
        actor,
        platform=platform,
        surface=surface,
        context={"step": "title"},
    )
    return CustomerInteractionMessage(
        text="🎥 Создаём вебинар\n\nКак называется мероприятие?",
        rows=(_back_row(),),
    )


def handle_native_event_wizard_text(
    actor: TenantContext,
    *,
    action: str,
    args: tuple[str, ...],
    platform: ConnectionPlatform,
    surface: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    if action == "event-we-text":
        if len(args) != 4:
            raise ValueError("invalid warmup edit text action")
        event_id, requested_days, raw_position, body = args
        position = int(raw_position)
        set_event_warmup_text(
            actor=actor,
            event_id=event_id,
            position=position,
            text=body,
        )
        return _warmup_preview(
            actor,
            event_id=event_id,
            requested_days=int(requested_days),
            page=position - 1,
        )

    if action == "event-warmup-days-text":
        if len(args) != 2:
            raise ValueError("invalid warmup days text action")
        event_id, raw_days = args
        plan = save_event_warmup_plan(
            actor=actor,
            event_id=event_id,
            requested_days=int(raw_days),
        )
        clear_owner_input(
            user_id=actor.user_id,
            platform=platform.value,
            surface=surface,
        )
        if not plan.drafts:
            return CustomerInteractionMessage(
                text="📨 Сообщения до вебинара отключены.",
                rows=(_back_row(),),
            )
        return _warmup_preview(
            actor,
            event_id=event_id,
            requested_days=plan.requested_days,
            page=0,
        )

    context = _context(actor, platform=platform, surface=surface)

    if action == "event-wizard-title-text":
        title = args[0]
        _update(
            actor,
            platform=platform,
            surface=surface,
            step="count",
            title=title,
        )
        return CustomerInteractionMessage(
            text="Сколько дней/эфиров будет в мероприятии?",
            rows=(
                (_button("1 день", "cpm:event-wizard:count:1"), _button("2 дня", "cpm:event-wizard:count:2")),
                (_button("3 дня", "cpm:event-wizard:count:3"), _button("5 дней", "cpm:event-wizard:count:5")),
                (_button("Другое число", "cpm:event-wizard:count:other"),),
                _back_row(),
            ),
        )

    if action == "event-wizard-count-text":
        return handle_native_event_wizard_action(
            actor,
            args=("count", args[0]),
            platform=platform,
            surface=surface,
        )

    if action == "event-wizard-topics-text":
        count = int(context.get("count") or "0")
        topics = [
            " ".join(line.split()).strip()
            for line in str(args[0] if args else "").splitlines()
            if line.strip()
        ]
        if count < 1 or len(topics) != count or any(len(topic) > 180 for topic in topics):
            _save(actor, platform=platform, surface=surface, context=context)
            return CustomerInteractionMessage(
                text=(
                    f"Нужно ровно {count} названий тем — каждое с новой строки "
                    "и не длиннее 180 символов."
                ),
                rows=(_back_row(),),
            )
        context.update(
            {
                "step": "timezone",
                "topics_json": json.dumps(topics, ensure_ascii=False),
            }
        )
        _save(actor, platform=platform, surface=surface, context=context)
        return _timezone_message()

    if action == "event-wizard-timezone-text":
        try:
            timezone_name = normalize_event_timezone(args[0])
        except ValueError:
            _save(actor, platform=platform, surface=surface, context=context)
            return CustomerInteractionMessage(
                text="Не удалось определить часовой пояс. Например: Europe/Amsterdam или Asia/Yekaterinburg.",
                rows=(_back_row(),),
            )
        context.update({"step": "venue", "timezone": timezone_name})
        _save(actor, platform=platform, surface=surface, context=context)
        return _venue_message()

    if action == "event-wizard-window-text":
        try:
            parsed = parse_session_window(
                args[0],
                timezone_name=context["timezone"],
                position=int(context["position"]),
            )
        except ValueError:
            _save(actor, platform=platform, surface=surface, context=context)
            return CustomerInteractionMessage(
                text="Не удалось понять интервал. Например: 25.09.2026 19:00-21:00.",
                rows=(_back_row(),),
            )
        context.update(
            {
                "step": "room",
                "pending_starts": parsed.starts_at.isoformat(),
                "pending_ends": parsed.ends_at.isoformat(),
                "pending_label": parsed.local_label,
            }
        )
        _save(actor, platform=platform, surface=surface, context=context)
        return _room_message(context)

    if action == "event-wizard-room-text":
        return _accept_room(
            actor,
            context=context,
            raw_url=args[0],
            platform=platform,
            surface=surface,
        )

    if action == "event-wizard-warmup-text":
        return _accept_warmup_days(
            actor,
            context=context,
            raw_days=args[0],
            platform=platform,
            surface=surface,
        )

    raise ValueError("unsupported native event wizard text action")


def _accept_count(
    actor: TenantContext,
    *,
    context: dict[str, str],
    raw_count: str,
    platform: ConnectionPlatform,
    surface: str,
) -> CustomerInteractionMessage:
    try:
        count = parse_session_count(raw_count)
    except ValueError:
        context["step"] = "count"
        _save(actor, platform=platform, surface=surface, context=context)
        return CustomerInteractionMessage(
            text="Введите число дней от 1 до 31.",
            rows=(_back_row(),),
        )
    context.update(
        {
            "step": "topics_choice",
            "count": str(count),
            "position": "1",
            "topics_json": "",
        }
    )
    _save(actor, platform=platform, surface=surface, context=context)
    return _topics_choice_message(count)


def _accept_room(
    actor: TenantContext,
    *,
    context: dict[str, str],
    raw_url: str,
    platform: ConnectionPlatform,
    surface: str,
) -> CustomerInteractionMessage:
    event_id = context.get("event_id", "")
    existing_urls: tuple[str, ...] = ()
    if event_id:
        existing_urls = tuple(
            item.join_url
            for item in list_event_sessions(actor=actor, event_id=event_id)
            if item.join_url
        )
    try:
        join_url = normalize_session_join_url(raw_url, existing_urls=existing_urls)
        if int(context["count"]) > 1 and join_url is None:
            raise ValueError("multi-session event requires a room for every session")
    except ValueError:
        _save(actor, platform=platform, surface=surface, context=context)
        return CustomerInteractionMessage(
            text=(
                "Нужна отдельная HTTPS-ссылка на комнату этого дня. "
                "Для однодневного вебинара можно отправить «-», если ссылка появится позже."
            ),
            rows=_room_message(context).rows,
        )

    venue = webinar_venue(context["venue"])
    provider_key = "auto" if venue.key == "other" else venue.key
    session = OnlineEventSessionCreateRequest(
        starts_at=datetime.fromisoformat(context["pending_starts"]),
        ends_at=datetime.fromisoformat(context["pending_ends"]),
        join_url=join_url,
        provider_key=provider_key,
        provider_label=None if venue.key == "other" else venue.label,
    )
    if event_id:
        configured = append_multisession_online_event_draft_session(
            actor=actor,
            event_id=event_id,
            session=session,
        )
        event_id = configured[0].event_id
    else:
        draft = create_multisession_online_event_draft(
            actor=actor,
            request=MultiSessionOnlineEventCreateRequest(
                title=context["title"],
                timezone_name=context["timezone"],
                description=_event_description(context),
                sessions=(session,),
            ),
        )
        event_id = draft.event_id

    position = int(context["position"])
    total = int(context["count"])
    if position < total:
        next_position = position + 1
        minimum = datetime.fromisoformat(context["pending_starts"]).astimezone(
            ZoneInfo(context["timezone"])
        ).date()
        context.update(
            {
                "event_id": event_id,
                "position": str(next_position),
                "step": "date",
                "min_date": minimum.isoformat(),
            }
        )
        for key in ("selected_date", "selected_start", "pending_starts", "pending_ends", "pending_label"):
            context.pop(key, None)
        _save(actor, platform=platform, surface=surface, context=context)
        return _date_picker_message(context)

    created = publish_multisession_online_event_draft(actor=actor, event_id=event_id)
    public_base = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip()
    registration_url = created.registration_url(public_base)
    window = get_event_warmup_window(actor=actor, event_id=event_id)
    context.update(
        {
            "event_id": event_id,
            "published": "1",
            "step": "warmup_days",
            "max_warmup_days": str(window.max_warmup_days),
            "registration_url": registration_url,
        }
    )
    _save(actor, platform=platform, surface=surface, context=context)
    return CustomerInteractionMessage(
        text=(
            f"✅ Вебинар создан. До первого дня — {window.days_until_event} календ. дн.\n\n"
            + _warmup_message(window.max_warmup_days).text
        ),
        rows=_warmup_message(window.max_warmup_days).rows,
    )


def _accept_warmup_days(
    actor: TenantContext,
    *,
    context: dict[str, str],
    raw_days: str,
    platform: ConnectionPlatform,
    surface: str,
) -> CustomerInteractionMessage:
    try:
        requested = int(str(raw_days).strip())
        plan = save_event_warmup_plan(
            actor=actor,
            event_id=context["event_id"],
            requested_days=requested,
        )
    except (TypeError, ValueError):
        _save(actor, platform=platform, surface=surface, context=context)
        return _warmup_message(int(context.get("max_warmup_days") or "0"))

    context["warmup_days"] = str(plan.requested_days)
    if plan.requested_days == 0:
        set_event_content_mode(
            actor=actor,
            event_id=context["event_id"],
            stage=EventContentStage.WARMUP,
            mode=EventContentMode.TEXT,
        )
        context.update({"warmup_mode": EventContentMode.TEXT.value, "step": "mode_event_day"})
        _save(actor, platform=platform, surface=surface, context=context)
        return _mode_message(EventContentStage.EVENT_DAY)

    context["step"] = "mode_warmup"
    _save(actor, platform=platform, surface=surface, context=context)
    return _mode_message(EventContentStage.WARMUP)


def _finish(
    actor: TenantContext,
    *,
    context: dict[str, str],
    platform: ConnectionPlatform,
    surface: str,
) -> CustomerInteractionMessage:
    clear_owner_input(user_id=actor.user_id, platform=platform.value, surface=surface)
    warmup = EventContentMode(context.get("warmup_mode", EventContentMode.TEXT.value))
    event_day = EventContentMode(context.get("event_day_mode", EventContentMode.TEXT.value))
    post_event = EventContentMode(context.get("post_event_mode", EventContentMode.TEXT.value))
    event_id = context["event_id"]
    requested_days = int(context.get("warmup_days") or "0")
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if requested_days > 0:
        rows.append(
            (
                _button(
                    "🔥 Тексты подготовительных сообщений",
                    f"cpm:event-wizard:wp:{event_id}:{requested_days}:0",
                ),
            )
        )
    rows.append((_button("✨ Сделать анонс", f"cpm:event-announce:{event_id}"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            f"✅ {context['title']}\n\n"
            f"Регистрация: {context['registration_url']}\n\n"
            f"Сообщения до вебинара: {requested_days} дн. — {event_content_mode_label(warmup)}\n"
            f"В день мероприятия — {event_content_mode_label(event_day)}\n"
            f"После мероприятия — {event_content_mode_label(post_event)}.\n\n"
            "Вебинар опубликован. Комнаты участников остаются скрыты за персональными ссылками."
        ),
        rows=tuple(rows),
    )


def _warmup_preview(
    actor: TenantContext,
    *,
    event_id: str,
    requested_days: int,
    page: int,
) -> CustomerInteractionMessage:
    plan = get_saved_event_warmup_plan(
        actor=actor,
        event_id=event_id,
    )
    if not plan.drafts:
        return CustomerInteractionMessage(
            text="Для этого вебинара сообщения до эфира не выбраны.",
            rows=(_back_row(),),
        )
    index = min(max(0, page), len(plan.drafts) - 1)
    draft = plan.drafts[index]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    navigation: list[CustomerInteractionButton] = []
    if index > 0:
        navigation.append(
            _button(
                "⬅️",
                f"cpm:event-wizard:wp:{event_id}:{requested_days}:{index - 1}",
            )
        )
    if index + 1 < len(plan.drafts):
        navigation.append(
            _button(
                "➡️",
                f"cpm:event-wizard:wp:{event_id}:{requested_days}:{index + 1}",
            )
        )
    if navigation:
        rows.append(tuple(navigation))
    rows.append(
        (
            _button(
                "✏️ Изменить / свой текст",
                f"cpm:event-wizard:we:{event_id}:{plan.requested_days}:{draft.position}",
            ),
        )
    )
    if draft.source == "owner":
        rows.append(
            (
                _button(
                    "♻️ Вернуть автотекст",
                    f"cpm:event-wizard:wr:{event_id}:{plan.requested_days}:{draft.position}",
                ),
            )
        )
    rows.append((_button("✨ Сделать анонс", f"cpm:event-announce:{event_id}"),))
    rows.append(_back_row())
    source_label = "Ваш текст" if draft.source == "owner" else "Автотекст"
    return CustomerInteractionMessage(
        text=(
            f"📨 Сообщение {draft.position}/{plan.requested_days} — "
            f"{draft.publish_date.strftime('%d.%m.%Y')}\n"
            f"Источник: {source_label}\n\n{draft.text}\n\n"
            "Можно использовать {name}, {title}, {join_url}. "
            "Если {join_url} не указан, персональная ссылка добавится автоматически."
        ),
        rows=tuple(rows),
    )


def handle_native_event_wizard_action(
    actor: TenantContext,
    *,
    args: tuple[str, ...],
    platform: ConnectionPlatform,
    surface: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    if not args:
        raise ValueError("webinar wizard action is missing")

    if args[0] == "wp":
        if len(args) != 4:
            raise ValueError("invalid warmup preview action")
        return _warmup_preview(
            actor,
            event_id=args[1],
            requested_days=int(args[2]),
            page=int(args[3]),
        )

    if args[0] == "we":
        if len(args) != 4:
            raise ValueError("invalid warmup edit action")
        event_id, requested_days, position = args[1], int(args[2]), int(args[3])
        begin_owner_input(
            actor=actor,
            platform=platform.value,
            surface=surface,
            action="event_warmup_text",
            context={
                "event_id": event_id,
                "requested_days": requested_days,
                "position": position,
            },
        )
        return CustomerInteractionMessage(
            text=(
                f"✏️ Пришлите новый текст сообщения {position} одним сообщением.\n\n"
                "Можно написать его полностью самостоятельно. Поддерживаются "
                "{name}, {title}, {join_url}. Для выхода отправьте «Отмена»."
            ),
            rows=(_back_row(),),
        )

    if args[0] == "wr":
        if len(args) != 4:
            raise ValueError("invalid warmup reset action")
        event_id, requested_days, position = args[1], int(args[2]), int(args[3])
        reset_event_warmup_text(
            actor=actor,
            event_id=event_id,
            position=position,
        )
        return _warmup_preview(
            actor,
            event_id=event_id,
            requested_days=requested_days,
            page=position - 1,
        )

    if args[0] == "ws":
        if len(args) != 2:
            raise ValueError("invalid warmup setup action")
        event_id = args[1]
        window = get_event_warmup_window(actor=actor, event_id=event_id)
        maximum = int(window.max_warmup_days)
        quick = [value for value in (0, 1, 2, 3, 5, 7, 10, 14) if value <= maximum]
        if maximum not in quick:
            quick.append(maximum)
        quick = sorted(set(quick))
        rows: list[tuple[CustomerInteractionButton, ...]] = []
        for index in range(0, len(quick), 3):
            rows.append(
                tuple(
                    _button(
                        str(value),
                        f"cpm:event-wizard:wset:{event_id}:{value}",
                    )
                    for value in quick[index : index + 3]
                )
            )
        if maximum > 0:
            rows.append(
                (
                    _button(
                        "✍️ Другое число",
                        f"cpm:event-wizard:wc:{event_id}:{maximum}",
                    ),
                )
            )
        rows.append(_back_row())
        return CustomerInteractionMessage(
            text=(
                f"🔥 Сколько дней отправлять сообщения до вебинара? Можно до {maximum} дн.\n\n"
                "Будет одно сообщение в день в 12:00 по часовому поясу вебинара. "
                "0 — не отправлять сообщения до вебинара."
            ),
            rows=tuple(rows),
        )

    if args[0] == "wset":
        if len(args) != 3:
            raise ValueError("invalid warmup set action")
        event_id, raw_days = args[1], args[2]
        plan = save_event_warmup_plan(
            actor=actor,
            event_id=event_id,
            requested_days=int(raw_days),
        )
        if not plan.drafts:
            return CustomerInteractionMessage(
                text="📨 Сообщения до вебинара отключены.",
                rows=(_back_row(),),
            )
        return _warmup_preview(
            actor,
            event_id=event_id,
            requested_days=plan.requested_days,
            page=0,
        )

    if args[0] == "wc":
        if len(args) != 3:
            raise ValueError("invalid warmup custom action")
        event_id, maximum = args[1], int(args[2])
        begin_owner_input(
            actor=actor,
            platform=platform.value,
            surface=surface,
            action="event_warmup_days",
            context={"event_id": event_id, "maximum": maximum},
        )
        return CustomerInteractionMessage(
            text=f"Отправьте число дней от 0 до {maximum}. 0 отключит сообщения до вебинара.",
            rows=(_back_row(),),
        )

    context = _context(actor, platform=platform, surface=surface)
    action = args[0]

    if action == "count":
        if len(args) != 2:
            raise ValueError("invalid count action")
        if args[1] == "other":
            context["step"] = "count"
            _save(actor, platform=platform, surface=surface, context=context)
            return CustomerInteractionMessage(
                text="Сколько дней/эфиров? Отправьте число от 1 до 31.",
                rows=(_back_row(),),
            )
        return _accept_count(
            actor,
            context=context,
            raw_count=args[1],
            platform=platform,
            surface=surface,
        )

    if action == "topics":
        if len(args) != 2 or args[1] not in {"yes", "no"}:
            raise ValueError("invalid topics action")
        if args[1] == "no":
            context.update({"step": "timezone", "topics_json": ""})
            _save(actor, platform=platform, surface=surface, context=context)
            return _timezone_message()
        context["step"] = "topics"
        _save(actor, platform=platform, surface=surface, context=context)
        count = int(context.get("count") or "0")
        return CustomerInteractionMessage(
            text=(
                f"Пришлите {count} названий тем — каждое с новой строки.\n\n"
                "Например:\nПервая тема\nВторая тема\nТретья тема"
            ),
            rows=(_back_row(),),
        )

    if action == "timezone":
        if len(args) != 2:
            raise ValueError("invalid timezone action")
        if args[1] == "other":
            context["step"] = "timezone"
            _save(actor, platform=platform, surface=surface, context=context)
            return CustomerInteractionMessage(
                text="Напишите часовой пояс, например Europe/Amsterdam или Asia/Yekaterinburg.",
                rows=(_back_row(),),
            )
        timezone_name = normalize_event_timezone("Москва")
        context.update({"step": "venue", "timezone": timezone_name})
        _save(actor, platform=platform, surface=surface, context=context)
        return _venue_message()

    if action == "venue":
        if len(args) != 2:
            raise ValueError("invalid venue action")
        venue = webinar_venue(args[1])
        if not venue.public_room_supported:
            return CustomerInteractionMessage(text=venue.note, rows=_venue_message().rows)
        context.update(
            {
                "venue": venue.key,
                "step": "date",
                "min_date": local_today(context["timezone"]).isoformat(),
            }
        )
        _save(actor, platform=platform, surface=surface, context=context)
        return _date_picker_message(context)

    if action == "date-page":
        if len(args) != 2:
            raise ValueError("invalid date page action")
        return _date_picker_message(context, offset=int(args[1]))

    if action == "date":
        if len(args) != 2:
            raise ValueError("invalid date action")
        selected = date.fromisoformat(args[1])
        minimum = date.fromisoformat(context["min_date"])
        if selected < minimum or (selected - minimum).days > _MAX_DATE_DAYS:
            raise ValueError("selected date is outside the wizard range")
        context.update({"step": "start", "selected_date": selected.isoformat()})
        _save(actor, platform=platform, surface=surface, context=context)
        return _start_time_message(selected)

    if action == "start":
        if len(args) != 2 or len(args[1]) != 4:
            raise ValueError("invalid start action")
        start_time = parse_quick_time(f"{args[1][:2]}:{args[1][2:]}")
        context.update({"step": "duration", "selected_start": start_time})
        _save(actor, platform=platform, surface=surface, context=context)
        return _duration_message(start_time)

    if action == "duration":
        if len(args) != 2:
            raise ValueError("invalid duration action")
        duration = parse_quick_duration(args[1])
        selected = date.fromisoformat(context["selected_date"])
        window = parse_session_window(
            session_window_text(
                selected_date=selected,
                start_time=context["selected_start"],
                duration_minutes=duration,
            ),
            timezone_name=context["timezone"],
            position=int(context["position"]),
        )
        context.update(
            {
                "step": "room",
                "pending_starts": window.starts_at.isoformat(),
                "pending_ends": window.ends_at.isoformat(),
                "pending_label": window.local_label,
            }
        )
        _save(actor, platform=platform, surface=surface, context=context)
        return _room_message(context)

    if action == "manual-window":
        context["step"] = "manual_window"
        _save(actor, platform=platform, surface=surface, context=context)
        return CustomerInteractionMessage(
            text="Напишите дату, начало и окончание одной строкой, например: 25.09.2026 19:00-21:00.",
            rows=(_back_row(),),
        )

    if action == "warmup":
        if len(args) != 2:
            raise ValueError("invalid warmup action")
        return _accept_warmup_days(
            actor,
            context=context,
            raw_days=args[1],
            platform=platform,
            surface=surface,
        )

    if action == "mode":
        if len(args) != 3:
            raise ValueError("invalid mode action")
        stage = EventContentStage(args[1])
        mode = EventContentMode(args[2])
        set_event_content_mode(
            actor=actor,
            event_id=context["event_id"],
            stage=stage,
            mode=mode,
        )
        if stage is EventContentStage.WARMUP:
            context.update({"warmup_mode": mode.value, "step": "mode_event_day"})
            _save(actor, platform=platform, surface=surface, context=context)
            return _mode_message(EventContentStage.EVENT_DAY)
        if stage is EventContentStage.EVENT_DAY:
            context.update({"event_day_mode": mode.value, "step": "mode_post_event"})
            _save(actor, platform=platform, surface=surface, context=context)
            return _mode_message(EventContentStage.POST_EVENT)
        context["post_event_mode"] = mode.value
        return _finish(actor, context=context, platform=platform, surface=surface)

    raise ValueError("unsupported webinar wizard action")


def abandon_native_event_wizard(
    actor: TenantContext,
    *,
    platform: ConnectionPlatform,
    surface: str,
) -> bool:
    try:
        context = _context(actor, platform=platform, surface=surface)
    except ValueError:
        return False
    event_id = context.get("event_id", "")
    published = context.get("published") == "1"
    if event_id and not published:
        from clientplatform.application.events import cancel_event

        try:
            cancel_event(actor=actor, event_id=event_id)
        except (LookupError, RuntimeError, ValueError):
            pass
    clear_owner_input(user_id=actor.user_id, platform=platform.value, surface=surface)
    return bool(event_id)


__all__ = [
    "abandon_native_event_wizard",
    "begin_native_event_wizard",
    "handle_native_event_wizard_action",
    "handle_native_event_wizard_text",
]
