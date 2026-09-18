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
EVENT_CHANNEL_LABELS = (
    ("email", "Email"),
    ("max", "MAX"),
    ("vk", "VK"),
    ("telegram", "Telegram"),
)
BACK_TO_GROWTH_LABEL = "⬅️ К продвижению"
BACK_TO_EVENTS_LABEL = "🎥 К вебинарам"
CREATE_EVENT_LABEL = "🎥 Создать вебинар"
EVENT_SETTINGS_LABEL = "⚙️ Автосообщения"


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


def _followup_status(snapshot: object) -> str:
    enabled = bool(getattr(snapshot, "commercial_followups_enabled", False))
    effective = bool(getattr(snapshot, "commercial_followups_effective", enabled))
    if enabled and not effective:
        return "🟡 включены, временно приостановлены"
    if enabled:
        return "🟢 включены"
    return "⚪️ выключены"


def event_hub_text(snapshot: object) -> str:
    lines = [
        "🎥 Вебинары",
        "",
        "Здесь можно создать вебинар, посмотреть результат и настроить весь контент-план.",
    ]
    items = tuple(getattr(snapshot, "items", ()))
    if items:
        lines.extend(["", "Последние вебинары:"])
        for item in items[:3]:
            revenue = ", ".join(row.display for row in item.revenue) or "—"
            join_note = "" if bool(getattr(item, "join_ready", True)) else " · 🔗 ссылка на эфир не добавлена"
            lines.extend(
                [
                    f"• {item.title} · {item.local_start}{join_note}",
                    (
                        f"  регистрации {item.registered} · пришли {item.attendance_confirmed} · "
                        f"оплаты {item.paid} · выручка {revenue}"
                    ),
                ]
            )
    else:
        lines.extend(["", "Вебинаров пока нет. Создайте первый — остальное можно настроить позже."])

    lines.extend(
        [
            "",
            f"Автосообщения вебинара: {_followup_status(snapshot)}",
            "Выберите действие ниже.",
        ]
    )
    if not bool(getattr(snapshot, "can_manage", False)):
        lines[-1] = "Вы можете смотреть результаты. Создание и настройки доступны владельцу или администратору."
    return "\n".join(lines)


def event_hub_actions(snapshot: object) -> tuple[EventHubAction, ...]:
    if not bool(getattr(snapshot, "can_manage", False)):
        return ()
    actions: list[EventHubAction] = [EventHubAction("create", CREATE_EVENT_LABEL)]
    items = tuple(getattr(snapshot, "items", ()))
    for item in items:
        if not bool(getattr(item, "join_ready", True)):
            actions.append(
                EventHubAction(
                    "join",
                    f"🔗 Добавить ссылку · {str(item.title)[:18]}",
                    key=str(item.id),
                )
            )
            break
    for item in items[:3]:
        if not getattr(item, "id", None):
            continue
        actions.append(
            EventHubAction(
                "content",
                f"🗓 Контент-план · {str(item.title)[:17]}",
                key=str(item.id),
            )
        )
    if items and getattr(items[0], "id", None):
        actions.append(
            EventHubAction(
                "announce",
                f"✨ Сделать анонс · {str(items[0].title)[:17]}",
                key=str(items[0].id),
            )
        )
    actions.append(EventHubAction("settings", EVENT_SETTINGS_LABEL))
    return tuple(actions)


def event_settings_text(snapshot: object) -> str:
    enabled = bool(getattr(snapshot, "commercial_followups_enabled", False))
    available = bool(getattr(snapshot, "commercial_followups_platform_available", True))
    lines = [
        "⚙️ Автосообщения вебинара",
        "",
        "ClientPlatform может автоматически отправлять согласованные прогревы до эфира "
        "и дожимы после него.",
        f"Статус: {_followup_status(snapshot)}",
        "",
        "Кому отправлять дожим:" if enabled else "После включения — кому отправлять дожим:",
    ]
    segments = _segments(snapshot)
    lines.extend(f"{'✅' if key in segments else '▫️'} {label}" for key, label in EVENT_SEGMENT_LABELS)
    channels = _channels(snapshot)
    lines.append(
        ("Каналы: " if enabled else "После включения — каналы: ")
        + " · ".join(f"{'✅' if key in channels else '▫️'} {label}" for key, label in EVENT_CHANNEL_LABELS)
    )
    if not available:
        lines.extend(
            [
                "",
                (
                    "Платформа временно остановила отправку. Настройка бизнеса сохранена."
                    if enabled
                    else "Автосообщения временно отключены на уровне платформы."
                ),
            ]
        )
    limitations = tuple(getattr(snapshot, "limitations", ()))
    if limitations:
        lines.extend(["", *limitations])
    return "\n".join(lines)


def event_settings_actions(snapshot: object) -> tuple[EventHubAction, ...]:
    if not bool(getattr(snapshot, "can_manage", False)):
        return ()
    actions: list[EventHubAction] = []
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
        "Как называется вебинар?\n"
        "Сначала отправьте только название.\n\n"
        f"Часовой пояс бизнеса: {timezone_name}.\n"
        "Дату и время я спрошу следующим сообщением. Zoom-ссылку или ссылку на другую площадку "
        "можно будет добавить позже отдельной кнопкой — печатать длинный адрес сейчас не нужно.\n\n"
        "Чтобы выйти без изменений, отправьте «Отмена» или нажмите «🎥 К вебинарам»."
    )


EVENT_CREATION_INPUT_GUIDANCE = (
    "Сначала отправьте название вебинара. Затем бот отдельно попросит дату и время. "
    "Ссылку на эфир можно добавить после создания."
)


def event_creation_failure_text() -> str:
    return (
        "Не удалось создать вебинар. Проверьте дату и время, ссылку на эфир (или -) "
        "и, если указана, ссылку предложения. Если ошибка повторяется, проверьте "
        "настройки e-mail."
    )


def event_provider_label(provider_key: object) -> str:
    value = str(provider_key or "").strip()
    if value == "pending":
        return "добавить позже"
    return "внешняя площадка" if not value or value == "external" else value


def event_creation_success_text(
    *,
    title: str,
    local_time: str,
    provider_key: object,
    registration_url: str,
    email_notifications_enabled: bool,
    join_ready: bool = True,
) -> str:
    mail_note = (
        "E-mail напоминания включены."
        if email_notifications_enabled
        else "E-mail не подключён — регистрация и ссылка входа всё равно работают."
    )
    join_note = (
        "Ссылка на эфир уже добавлена."
        if join_ready
        else "Ссылку на эфир можно добавить позже — регистрация уже открыта."
    )
    return (
        "✅ Вебинар опубликован.\n\n"
        f"{title}\n"
        f"{local_time} · площадка: {event_provider_label(provider_key)}\n\n"
        f"Регистрация: {registration_url}\n\n{join_note}\n{mail_note}"
    )


__all__ = [
    "BACK_TO_EVENTS_LABEL",
    "BACK_TO_GROWTH_LABEL",
    "CREATE_EVENT_LABEL",
    "EVENT_SETTINGS_LABEL",
    "EVENT_CHANNEL_LABELS",
    "EVENT_CREATION_INPUT_GUIDANCE",
    "EVENT_SEGMENT_LABELS",
    "EventHubAction",
    "event_creation_failure_text",
    "event_creation_prompt",
    "event_creation_success_text",
    "event_hub_actions",
    "event_hub_text",
    "event_settings_actions",
    "event_settings_text",
    "event_provider_label",
]
