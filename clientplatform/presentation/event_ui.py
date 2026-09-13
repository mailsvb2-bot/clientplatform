from __future__ import annotations

from dataclasses import dataclass

from clientplatform.domain.event_followup import (
    EVENT_FOLLOWUP_CHANNELS,
    EVENT_FOLLOWUP_SEGMENTS,
)

EVENT_SEGMENT_LABELS = (
    ("no_show", "Зарегистрировались, но не пришли"),
    ("join_signal_unpaid", "Вошли в эфир, участие не подтверждено"),
    ("attended_unpaid", "Были на вебинаре, но не купили"),
    ("offer_clicked_unpaid", "Открыли предложение, но не купили"),
)
EVENT_CHANNEL_LABELS = (("email", "Email"), ("max", "MAX"), ("vk", "VK"))
BACK_TO_GROWTH_LABEL = "⬅️ К продвижению"
BACK_TO_EVENTS_LABEL = "🎥 К вебинарам"
CREATE_EVENT_LABEL = "🎥 Создать вебинар"


@dataclass(frozen=True, slots=True)
class EventHubAction:
    kind: str
    label: str
    key: str | None = None
    enabled: bool | None = None


def _segments(snapshot: object) -> set[str]:
    return set(getattr(snapshot, "commercial_followup_segments", EVENT_FOLLOWUP_SEGMENTS))


def _channels(snapshot: object) -> set[str]:
    return set(getattr(snapshot, "commercial_followup_channels", EVENT_FOLLOWUP_CHANNELS))


def event_hub_text(snapshot: object) -> str:
    lines = ["🎥 Вебинары", ""]
    items = tuple(getattr(snapshot, "items", ()))
    if items:
        lines.append("Последние мероприятия:")
        for item in items[:5]:
            revenue = ", ".join(row.display for row in item.revenue) or "—"
            lines.extend(
                [
                    f"• {item.title} · {item.local_start}",
                    (
                        f"  регистрации {item.registered} · входы {item.join_clicked} · "
                        f"участие {item.attendance_confirmed} · оффер {item.offer_clicked} · "
                        f"оплаты {item.paid} · выручка {revenue}"
                    ),
                ]
            )
    else:
        lines.append("Пока нет опубликованных мероприятий.")

    enabled = bool(getattr(snapshot, "commercial_followups_enabled", False))
    effective = bool(getattr(snapshot, "commercial_followups_effective", enabled))
    available = bool(getattr(snapshot, "commercial_followups_platform_available", True))
    if enabled and not effective:
        status = "Автоматические сообщения после мероприятия: 🟡 ВКЛ, временно приостановлены"
    elif enabled:
        status = "Автоматические сообщения после мероприятия: 🟢 ВКЛ"
    else:
        status = "Автоматические сообщения после мероприятия: ⚪️ ВЫКЛ"
    lines.extend(["", status])

    segments = _segments(snapshot)
    lines.append("Кому писать:" if enabled else "После включения — кому писать:")
    lines.extend(f"{'✅' if key in segments else '▫️'} {label}" for key, label in EVENT_SEGMENT_LABELS)
    channels = _channels(snapshot)
    lines.append(
        ("Каналы: " if enabled else "После включения — каналы: ")
        + " · ".join(f"{'✅' if key in channels else '▫️'} {label}" for key, label in EVENT_CHANNEL_LABELS)
    )
    if not available:
        if enabled:
            lines.append(
                "Платформа временно остановила отправку; настройка бизнеса сохранена. "
                "Её можно выключить сейчас, чтобы сообщения не возобновились автоматически."
            )
        else:
            lines.append("Автосерия временно отключена на уровне платформы.")
    limitations = tuple(getattr(snapshot, "limitations", ()))
    if limitations:
        lines.extend(["", *limitations])
    return "\n".join(lines)


def event_hub_actions(snapshot: object) -> tuple[EventHubAction, ...]:
    if not bool(getattr(snapshot, "can_manage", False)):
        return ()
    actions: list[EventHubAction] = [EventHubAction("create", CREATE_EVENT_LABEL)]
    enabled = bool(getattr(snapshot, "commercial_followups_enabled", False))
    if enabled:
        actions.append(EventHubAction("followups", "🔴 Выключить автосообщения", enabled=False))
    elif bool(getattr(snapshot, "can_enable_commercial_followups", False)):
        actions.append(EventHubAction("followups", "🟢 Включить автосообщения", enabled=True))

    segments = _segments(snapshot)
    can_expand = bool(getattr(snapshot, "can_expand_commercial_followups", False))
    for key, label in EVENT_SEGMENT_LABELS:
        active = key in segments
        if active or can_expand:
            actions.append(
                EventHubAction(
                    "segment",
                    f"{'✅' if active else '▫️'} {label}",
                    key=key,
                    enabled=not active,
                )
            )
    channels = _channels(snapshot)
    for key, label in EVENT_CHANNEL_LABELS:
        active = key in channels
        if active or can_expand:
            actions.append(
                EventHubAction(
                    "channel",
                    f"{'✅' if active else '▫️'} {label}",
                    key=key,
                    enabled=not active,
                )
            )
    return tuple(actions)


def event_creation_prompt(timezone_name: str) -> str:
    return (
        "🎥 Новый вебинар\n\n"
        f"Часовой пояс бизнеса: {timezone_name}.\n"
        "Отправьте одной строкой:\n"
        "Название | ДД.ММ.ГГГГ ЧЧ:ММ | HTTPS-ссылка на эфир | ссылка предложения или -\n\n"
        "Площадка может быть любой: Zoom, Webinar.ru, МТС Линк, Телемост, VK, "
        "YouTube, RuTube или другой HTTPS-сервис.\n\n"
        "Чтобы выйти без изменений, отправьте «Отмена» или нажмите «🎥 К вебинарам»."
    )


EVENT_CREATION_INPUT_GUIDANCE = (
    "Напишите: Название | ДД.ММ.ГГГГ ЧЧ:ММ | HTTPS-ссылка на эфир | "
    "необязательная HTTPS-ссылка предложения. Последнее поле можно заменить на -."
)


def event_creation_failure_text() -> str:
    return (
        "Не удалось создать вебинар. Проверьте дату и время, HTTPS-ссылку на эфир "
        "и, если указана, ссылку предложения. Если ошибка повторяется, проверьте "
        "настройки e-mail."
    )


def event_provider_label(provider_key: object) -> str:
    value = str(provider_key or "").strip()
    return "внешняя площадка" if not value or value == "external" else value


def event_creation_success_text(
    *,
    title: str,
    local_time: str,
    provider_key: object,
    registration_url: str,
    email_notifications_enabled: bool,
) -> str:
    mail_note = (
        "E-mail напоминания включены."
        if email_notifications_enabled
        else "E-mail не подключён — регистрация и ссылка входа всё равно работают."
    )
    return (
        "✅ Вебинар опубликован.\n\n"
        f"{title}\n"
        f"{local_time} · площадка: {event_provider_label(provider_key)}\n\n"
        f"Регистрация: {registration_url}\n\n{mail_note}"
    )


__all__ = [
    "BACK_TO_EVENTS_LABEL",
    "BACK_TO_GROWTH_LABEL",
    "CREATE_EVENT_LABEL",
    "EVENT_CHANNEL_LABELS",
    "EVENT_CREATION_INPUT_GUIDANCE",
    "EVENT_SEGMENT_LABELS",
    "EventHubAction",
    "event_creation_failure_text",
    "event_creation_prompt",
    "event_creation_success_text",
    "event_hub_actions",
    "event_hub_text",
    "event_provider_label",
]
