from __future__ import annotations

import os
from dataclasses import dataclass, field

from config.settings import settings
from runtime.ingress_flags import payment_http_enabled
from services.db import db
from services.jobs import add_job, cancel_jobs
from services.personalization import get_preface
from services.delivery_preferences import (
    describe_delivery_preferences,
    set_user_timezone,
    set_quiet_hours,
    clear_quiet_hours,
    set_slot_channel,
    build_delivery_policy_decision,
)

from services.messenger.bridge import issue_bridge_token
from services.messenger.entrypoints import register_user_entry
from services.messenger.links import build_messenger_targets, build_switch_targets
from services.messenger.platforms import normalize_platform, platform_title
from services.messenger.menu_contract import normalize_menu_command
from services.messenger.package_payment_ui import gift_package_text, gift_recipient_prompt_text, package_payment_text
from services.messenger.preferences import get_channel_snapshot, set_preferred_platform
from services.messenger.audio_progress import get_progress_snapshot, SEQUENCE_FULL_SERIES, confirm_pending_audio_delivery, get_audio_item_by_anchor
from services.messenger.timeline import get_recent_audio_timeline
from services.mood_text_flow import parse_score_text, find_pending_pre_session_id, find_pending_post_session_id
from services.mood import create_session
from services.pending import set_pending, peek_pending, pop_pending
from services.bonuses import compute_bonus_stats, paid_referrals_count, gift_grants_count, gift_days_granted
from services.state_ratings import add_rating
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class MessengerReply:
    kind: str = 'text'
    text: str = ''
    meta: dict[str, str] = field(default_factory=dict)


def _score_scale_text() -> str:
    return (
        'Шкала оценки: -10 — стало сильно хуже, 0 — без изменений, +10 — стало сильно лучше.\n'
        'Можно отправить любое число от -10 до 10, например: -2, 0, 4 или 8.'
    )


def _menu_text(user_id: int) -> str:
    preface = get_preface(int(user_id), context="menu")
    return (
        f"{preface}"
        "Главное меню\n\n"
        "Выберите маршрут: можно начать с бесплатной практики, открыть полный доступ или посмотреть свой прогресс.\n\n"
        "Кнопки ВКонтакте соответствуют главному меню Telegram:\n"
        "• 🌿 Попробовать бесплатно\n"
        "• 🔐 Полный маршрут\n"
        "• 💳 Тарифы\n"
        "• 🎁 Подарить\n"
        "• 📈 Мой прогресс\n"
        "• 🧠 Настройки\n"
        "• 📣 Посоветовать\n"
        "• 🌤 Погода"
    )


def _settings_text(user_id: int) -> str:
    snapshot = get_channel_snapshot(int(user_id))
    current = platform_title(snapshot.get("preferred_platform"))
    linked: list[str] = []
    for identity in snapshot.get("identities") or []:
        title = platform_title(identity.get("platform"))
        if title not in linked:
            linked.append(title)
    linked_text = ", ".join(linked) if linked else "пока нет"
    return (
        "⚙️ Настройки канала\n\n"
        f"Предпочтительный мессенджер: {current}\n"
        f"Подключённые каналы: {linked_text}\n\n"
        "Чтобы сменить приоритет, отправьте одну из команд:\n"
        "/platform telegram\n"
        "/platform max\n"
        "/platform vk\n\n"
        "Чтобы привязать ещё один мессенджер к этому же профилю без потери прогресса, отправьте: switch\n\n"
        f"{describe_delivery_preferences(int(user_id))}"
    )


def _share_text(user_id: int) -> str:
    targets = build_messenger_targets(int(user_id))

    lines = [
        "↗️ Поделиться Метротерапией",
        "",
        "В VK нельзя надёжно открыть системный выбор друзей прямо из кнопки бота.",
        "Поэтому я подготовил готовый текст: его можно скопировать и отправить человеку в VK, Telegram или любом другом мессенджере.",
        "",
        "Текст для пересылки:",
        "",
        "🌿 Попробуй Метротерапию — короткие аудиопрактики для спокойствия, сна и восстановления.",
        "Начать можно здесь: https://vk.com/im?sel=-238191212",
        "",
        "Кнопки ниже открывают VK-бота, Telegram и сайт.",
    ]

    if targets:
        lines.append("")
        lines.append("Дополнительные ссылки:")
        for item in targets:
            lines.append(f"• {item['title']}: {item['url']}")

    return "\n".join(lines)


PAYMENT_UNAVAILABLE_TEXT = (
    "💳 Оплата сейчас недоступна в этом канале. "
    "Вернитесь в главное меню или попробуйте позже."
)


def _payment_ui_enabled() -> bool:
    """Honor an explicit payment-ingress disable without changing legacy UI defaults."""

    if "PAYMENT_HTTP_ENABLED" not in os.environ:
        return True
    return payment_http_enabled()


def _payment_text(user_id: int, *, platform: str, external_user_id: str | None) -> str:
    """Canonical public payment surface for Telegram/VK/MAX text UI."""
    if not _payment_ui_enabled():
        return PAYMENT_UNAVAILABLE_TEXT
    return package_payment_text(
        user_id=int(user_id),
        platform=normalize_platform(platform),
        external_user_id=external_user_id,
    )


def _gift_text(user_id: int, *, platform: str, external_user_id: str | None, recipient_hint: str = "") -> str:
    """Canonical public gift/payment surface for Telegram/VK/MAX text UI."""
    if not _payment_ui_enabled():
        return PAYMENT_UNAVAILABLE_TEXT
    return gift_package_text(
        user_id=int(user_id),
        platform=normalize_platform(platform),
        external_user_id=external_user_id,
        recipient_hint=recipient_hint,
    )


def _start_gift_recipient_flow(user_id: int) -> MessengerReply:
    if not _payment_ui_enabled():
        return MessengerReply(text=PAYMENT_UNAVAILABLE_TEXT)
    set_pending(int(user_id), "gift_recipient", {})
    return MessengerReply(text=gift_recipient_prompt_text())


def _gift_payment_after_recipient(user_id: int, *, platform: str, external_user_id: str | None, recipient_hint: str) -> MessengerReply:
    return MessengerReply(
        text=_gift_text(
            int(user_id),
            platform=platform,
            external_user_id=external_user_id,
            recipient_hint=recipient_hint,
        )
    )


def _switch_text(user_id: int) -> str:
    token = issue_bridge_token(int(user_id))
    targets = build_switch_targets(token)
    if not targets:
        return (
            "🔁 Переключение между мессенджерами пока не настроено ссылками.\n\n"
            "Нужно задать TELEGRAM_BOT_USERNAME, MAX_BOT_LINK_BASE/MAX_BOT_NAME и VK_GROUP_ID."
        )
    lines = [
        "🔁 Продолжить в другом мессенджере без потери прогресса можно по ссылкам ниже:",
        "",
    ]
    for item in targets:
        lines.append(f"• {item['title']}: {item['url']}")
    lines.append("")
    lines.append("После входа по одной из этих ссылок новый мессенджер привяжется к вашему текущему профилю.")
    lines.append("Дальше просто отправьте: continue — и система пришлёт текущее или следующее аудио общей очереди.")
    return "\n".join(lines)


def _bridge_linked_text(user_id: int, platform: str) -> str:
    snapshot = get_progress_snapshot(int(user_id))
    current = platform_title(platform)
    if snapshot.pending_item is not None:
        tail = f"У вас уже выдано, но ещё не открыто аудио №{snapshot.pending_item.anchor} — {snapshot.pending_item.title}. Сейчас пришлю его в этом мессенджере без дублей."
    elif snapshot.last_anchor is None and snapshot.next_item is not None:
        tail = f"Следующим будет №{snapshot.next_item.anchor} — {snapshot.next_item.title}. Сейчас пришлю его сюда."
    elif snapshot.next_item is not None:
        tail = f"Вы уже дошли до №{snapshot.last_anchor} — {snapshot.last_title}. Следующим будет №{snapshot.next_item.anchor} — {snapshot.next_item.title}. Сейчас пришлю его сюда."
    else:
        tail = "Основная серия уже дослушана до конца."
    return f"✅ {current} привязан к вашему существующему профилю.\n\n{tail}"


def _should_auto_resume_after_bridge(user_id: int) -> bool:
    snapshot = get_progress_snapshot(int(user_id))
    return snapshot.pending_item is not None or snapshot.next_item is not None


def _progress_text(user_id: int) -> str:
    snapshot = get_progress_snapshot(int(user_id))
    pending_tail = ''
    if snapshot.pending_item is not None:
        pending_tail = (
            f"\n\n⏳ Уже отправлено, но ещё не подтверждено открытием: "
            f"№{snapshot.pending_item.anchor} — {snapshot.pending_item.title} "
            f"({platform_title(snapshot.pending_platform)})."
        )
    if snapshot.last_anchor is None:
        if snapshot.next_item is None:
            audio_part = "🎧 Аудиосерия пока не найдена в каталоге."
        else:
            audio_part = (
                "🎧 Вы ещё не запускали общую очередь аудио.\n\n"
                f"Следующим будет №{snapshot.next_item.anchor} — {snapshot.next_item.title}."
                f"{pending_tail}"
            )
    else:
        tail = f"Следующим будет №{snapshot.next_item.anchor} — {snapshot.next_item.title}." if snapshot.next_item else "Серия уже дослушана до конца."
        channel = platform_title(snapshot.last_platform)
        audio_part = (
            "🎧 Общий прогресс аудио\n\n"
            f"Последнее подтверждённое аудио: №{snapshot.last_anchor} — {snapshot.last_title}\n"
            f"Подтверждено в канале: {channel}\n\n"
            f"{tail}{pending_tail}"
        )
    return (
        f"{audio_part}\n\n"
        "📈 Мой прогресс и анализ состояния\n\n"
        "Сейчас пришлю графики по тем же данным, что используются в Telegram: "
        "быстрая шкала состояния, дорога на работу, дорога домой и общая динамика — если по ним уже есть данные.\n\n"
        "Чтобы добавить новую оценку состояния, отправьте число от -10 до 10 после прослушивания аудио."
    )


def _history_text(user_id: int) -> str:
    events = get_recent_audio_timeline(int(user_id), sequence_key=SEQUENCE_FULL_SERIES, limit=8)
    if not events:
        return "🧾 История аудио и переходов пока пуста."
    labels = {
        'bridge_linked': 'перешёл в другой мессенджер',
        'issued_pending': 'выдано следующее аудио',
        'reused_pending': 'повторно показано уже выданное аудио',
        'link_sent': 'отправлена ссылка на аудио',
        'access_confirmed': 'аудио открыто и подтверждено',
        'confirmed_delivery': 'аудио подтверждено доставкой',
        'telegram_sent': 'аудио отправлено в Telegram',
        'native_audio_sent': 'аудио отправлено как вложение',
        'native_audio_fallback': 'native-вложение недоступно, использована ссылка',
        'manual_confirmed': 'аудио подтверждено вручную',
        'pre_score_received': 'оценка до прослушивания сохранена',
        'post_score_received': 'оценка после прослушивания сохранена',
    }
    lines = ["🧾 Последние шаги по общей аудио-очереди:", ""]
    for event in events:
        label = labels.get(event.event_type, event.event_type)
        piece = f"• {event.created_at}: {label}"
        if event.anchor is not None:
            piece += f" — №{event.anchor}"
        if event.title:
            piece += f" — {event.title}"
        if event.platform:
            piece += f" ({platform_title(event.platform)})"
        lines.append(piece)
    return "\n".join(lines)


def _help_text() -> str:
    return (
        "Подсказка по мульти-мессенджерному режиму\n\n"
        "• /start или start — регистрация входа и меню\n"
        "• settings — посмотреть каналовые настройки\n"
        "• /platform telegram|max|vk — выбрать приоритетный мессенджер\n"
        "• share — получить ссылки для рекламы и рекомендаций\n"
        "• switch — привязать другой мессенджер к этому же профилю\n"
        "• continue — прислать текущее/следующее аудио общей очереди\n"
        "• done — подтвердить, что текущее аудио дослушано\n"
        "• число от -10 до 10 — сохранить оценку до/после прослушивания\n"
        "• progress — показать, где вы остановились\n"
        "• history — показать недавнюю историю переходов и аудио\n"
        "• time — показать время отправки, часовой пояс и тихие часы\n"
        "• timezone Europe/Amsterdam — сменить часовой пояс\n"
        "• quiet 22:00-08:00 — задать тихие часы, quiet off — выключить\n"
        "• channel morning max — выбрать канал для утренних отправок\n"
        "• channel evening auto — вернуть авто-выбор\n\n"
        "Очередь аудио общая для Telegram, MAX и ВКонтакте, если мессенджеры привязаны к одному профилю через switch-ссылки. "
        "Для VK и MAX можно явно написать done / готово / прослушал, когда трек дослушан, а затем отправить число от -10 до 10 как оценку после прослушивания."
    )


def _demo_text() -> str:
    return (
        "🌿 Бесплатная практика\n\n"
        "Выберите короткий маршрут — как в Telegram.\n\n"
        "1️⃣ Утро / дорога — мягко включиться в день.\n"
        "2️⃣ Вечер / домой — снять напряжение и завершить день спокойнее.\n\n"
        "Нажмите кнопку ниже или отправьте цифру: 1 или 2.\n\n"
        "После аудио нажмите «✅ Прослушал», затем отправьте оценку от -10 до 10.\n"
        "Telegram для этого не нужен — сценарий исполняется внутри ВКонтакте."
    )


def _full_route_text(user_id: int) -> str:
    _ = get_progress_snapshot(int(user_id))
    return (
        "🔐 Полный маршрут\n\n"
        "Здесь можно открыть полный маршрут или поставить напоминание на завтра утром.\n\n"
        "Нажмите «🔐 Открыть полный маршрут», чтобы перейти к тарифам, "
        "или «⏰ Напомнить завтра утром», чтобы я напомнил продолжить завтра."
    )


def _weather_text() -> str:
    return (
        "🌤 Погода\n\n"
        "Сейчас покажу прогноз по сохранённому городу или попрошу указать город.\n\n"
        "Во ВКонтакте доступны те же базовые действия, что и в Telegram:\n"
        "• посмотреть погоду;\n"
        "• изменить город;\n"
        "• вернуться в главное меню."
    )


def _weather_city_prompt_text() -> str:
    return (
        "🏙 Напишите название города одним сообщением.\n\n"
        "Например: Москва, Казань, Amsterdam, Berlin.\n\n"
        "После этого я сохраню город и покажу прогноз."
    )



def _parse_hhmm_for_reminder(value: str) -> tuple[int, int] | None:
    raw = (value or "").strip()
    if ":" not in raw:
        return None
    hh, mm = raw.split(":", 1)
    if not (hh.isdigit() and mm.isdigit()):
        return None
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def _schedule_continue_tomorrow_text(user_id: int) -> str:
    uid = int(user_id)
    tz_name = getattr(settings, "TIMEZONE", "Europe/Moscow")
    tz = ZoneInfo(tz_name)

    with db() as conn:
        row = conn.execute("SELECT work_time FROM users WHERE user_id=?", (uid,)).fetchone()

    hhmm = ""
    if row is not None:
        try:
            hhmm = row["work_time"] or ""
        except (KeyError, TypeError, IndexError):
            hhmm = ""

    hhmm = hhmm or getattr(settings, "MORNING_TIME", "08:30")
    parsed = _parse_hhmm_for_reminder(str(hhmm)) or _parse_hhmm_for_reminder(getattr(settings, "MORNING_TIME", "08:30"))
    h, m = parsed if parsed else (8, 30)

    now_local = datetime.now(tz)
    run_local = (now_local + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
    run_utc = run_local.astimezone(ZoneInfo("UTC")).replace(microsecond=0).isoformat()

    cancel_jobs(uid, prefix="remind_")
    add_job(uid, "remind_continue", run_utc, {"src": "full_access", "hhmm": f"{h:02d}:{m:02d}"})

    return f"✅ Хорошо. Я напомню Вам завтра в {run_local.strftime('%H:%M')} ({tz_name})."


def _text_time_prompt(slot: str) -> str:
    title = "Дорога на работу" if slot == "work" else "Дорога домой"
    return (
        f"⏰ Время «{title}»\n\n"
        "Напишите желаемое время в формате HH:MM, например 08:30.\n\n"
        "Я сохраню время — и транс будет приходить ровно в него."
    )


def _save_text_time(user_id: int, slot: str, raw_value: str) -> str:
    hhmm = _parse_hhmm_for_reminder(raw_value)
    if hhmm is None:
        return "Пожалуйста, время в формате HH:MM, например 08:30."
    h, m = hhmm
    if slot == "work":
        update_sql = "UPDATE users SET work_time=? WHERE user_id=?"
        title = "Дорога на работу"
    elif slot == "home":
        update_sql = "UPDATE users SET home_time=? WHERE user_id=?"
        title = "Дорога домой"
    else:
        return "Не понял, для какой дороги сохранить время."
    value = f"{h:02d}:{m:02d}"
    uid = int(user_id)
    with db() as conn:
        conn.execute(
            "INSERT INTO users(user_id, joined_at) VALUES(?, COALESCE((SELECT joined_at FROM users WHERE user_id=?), datetime('now'))) "
            "ON CONFLICT(user_id) DO NOTHING",
            (uid, uid),
        )
        conn.execute(update_sql, (value, uid))
    return f"✅ Сохранил время «{title}»: {value}"


def _ref_bonus_text(user_id: int) -> str:
    uid = int(user_id)
    n_paid = paid_referrals_count(uid)
    n_gifts = gift_grants_count(uid)
    gift_days = gift_days_granted(uid)
    stats = compute_bonus_stats(uid)
    return (
        "🎁 Мои бонусы за приглашения\n\n"
        f"По Вашему приглашению программу оплатили: {n_paid} человек(а).\n\n"
        f"За подарки Вы получили бонусов: {gift_days} дн. (подарков: {n_gifts}).\n\n"
        "Бонусы (в днях):\n"
        f"• начислено: {stats.earned_days} дн.\n"
        f"• израсходовано: {stats.used_days} дн.\n"
        f"• остаток: {stats.remaining_days} дн.\n\n"
        "Бонус начисляется только за тех, кто оплатил программу.\n"
        "Бонус за подарки начисляется сразу после оплаты подарка.\n"
        "Дни не обнуляются — всё сохраняется."
    )


def _delivery_channels_text(user_id: int) -> str:
    return (
        "📨 Каналы по времени дня\n\n"
        + describe_delivery_preferences(int(user_id))
        + "\n\nВыберите, куда сначала пытаться доставлять утренние и вечерние касания."
    )


def _delivery_slot_text(user_id: int, slot: str) -> str:
    label = "утренних" if slot == "morning" else "вечерних"
    return f"📨 Канал для {label} отправок\n\n" + describe_delivery_preferences(int(user_id))




def _state_rate_prompt_text() -> str:
    return "⭐ Оцените своё состояние прямо сейчас (1 — хуже, 10 — лучше):"


def _save_state_rate_text(user_id: int, value: str | None) -> str:
    try:
        rating = int(str(value or "").strip())
    except (TypeError, ValueError):
        return "⚠️ Не удалось распознать оценку. Выберите число от 1 до 10."
    if not 1 <= rating <= 10:
        return "⚠️ Оценка должна быть от 1 до 10."
    ok = add_rating(int(user_id), rating)
    if not ok:
        return "⚠️ Не удалось сохранить оценку. Попробуйте ещё раз."
    return f"✅ Сохранил: {rating}/10\n\nВыберите период, чтобы построить график:"


def _platform_changed_text(user_id: int, platform: str) -> str:
    set_preferred_platform(int(user_id), platform)
    return f"Сохранено: приоритетный канал — {platform_title(platform)}."


def _pending_score_stage(user_id: int) -> str | None:
    with db() as conn:
        row = conn.execute(
            """
            SELECT id, stage
            FROM (
                SELECT id, 'pre' AS stage
                FROM mood_sessions
                WHERE user_id=? AND pre_score IS NULL AND COALESCE(audio_sent,0)=0
                  AND COALESCE(source,'') IN ('auto','settings','demo')
                  AND COALESCE(kind,'') IN ('work','home')
                UNION ALL
                SELECT id, 'post' AS stage
                FROM mood_sessions
                WHERE user_id=? AND pre_score IS NOT NULL AND post_score IS NULL AND COALESCE(audio_sent,0)=1
                  AND COALESCE(source,'') IN ('auto','settings','demo')
                  AND COALESCE(kind,'') IN ('work','home')
            ) pending_scores
            ORDER BY id DESC
            LIMIT 1
            """.strip(),
            (int(user_id), int(user_id)),
        ).fetchone()
    return str(row["stage"]) if row else None


def _parse_command(text: str) -> tuple[str, str | None]:
    raw = (text or "").strip()
    if not raw:
        return "menu", None
    lowered = raw.lower()
    if lowered.startswith("mood:done:"):
        return "done", lowered.rsplit(":", 1)[-1]
    if lowered.startswith("mood:"):
        parts = lowered.split(":")
        if len(parts) >= 4:
            stage = parts[1]
            score = parts[-1]
            if stage in {"pre", "post"}:
                return ("pre_score" if stage == "pre" else "post_score"), score
    if lowered == "state:rate":
        return "state_rate_menu", None
    if lowered.startswith("state:rate:"):
        parts = lowered.split(":")
        if len(parts) == 3:
            return "state_rate_save", parts[2]
    if lowered in {"state:today", "state:yesterday", "state:all"}:
        return "state_period", lowered.split(":", 1)[1]
    contract_command = normalize_menu_command(raw)
    if contract_command:
        return ("menu" if contract_command == "start" else contract_command), None
    if lowered.startswith("/start"):
        parts = raw.split(maxsplit=1)
        return "start", parts[1].strip() if len(parts) == 2 else ""
    if lowered in {"start", "menu", "/menu", "/start"}:
        return "menu", None
    if lowered in {"help", "/help", "помощь"}:
        return "help", None
    if lowered in {"settings", "/settings", "настройки"}:
        return "settings", None
    if lowered in {"share", "/share", "пригласить", "поделиться"}:
        return "share", None
    if lowered in {"switch", "/switch", "сменить канал", "другой мессенджер"}:
        return "switch", None
    if lowered in {"continue", "/continue", "next", "/next", "audio", "/audio", "следующее аудио", "🎧 получить аудио"}:
        return "continue", None
    if lowered in {"repeat", "repeat_audio", "/repeat", "повторить", "🔁 повторить", "повторить аудио", "🔁 повторить аудио", "слушать снова"}:
        return "repeat_audio", None
    if lowered in {"done", "/done", "готово", "прослушал", "дослушал", "listen done"}:
        return "done", None
    if lowered in {"progress", "/progress", "где остановился", "прогресс"}:
        return "progress", None
    if lowered in {"history", "/history", "timeline", "/timeline", "история", "🧾 история"}:
        return "history", None
    if lowered in {"menu:main", "back", "⬅️ назад", "⬅️ меню"}:
        return "menu", None
    if lowered in {"sub:menu"}:
        return "pay", None
    if lowered in {"gift:menu"}:
        return "gift", None
    if lowered in {"settings:menu"}:
        return "settings", None
    if lowered in {"settings:state"}:
        return "progress", None
    if lowered in {"share:menu"}:
        return "share", None
    if lowered in {"weather:show"}:
        return "weather", None
    if lowered in {"remind_continue_tomorrow", "/remind_continue_tomorrow", "remind:continue_tomorrow", "⏰ напомнить завтра утром", "напомнить завтра утром"}:
        return "remind_continue_tomorrow", None
    if lowered in {"settings:time:work"}:
        return "settings_time", "work"
    if lowered in {"settings:time:home"}:
        return "settings_time", "home"
    if lowered in {"settings:ref"}:
        return "settings_ref", None
    if lowered in {"settings:platform:menu"}:
        return "settings", None
    if lowered in {"settings:delivery:channels"}:
        return "settings_delivery_channels", None
    if lowered in {"settings:delivery:slot:morning"}:
        return "settings_delivery_slot", "morning"
    if lowered in {"settings:delivery:slot:evening"}:
        return "settings_delivery_slot", "evening"
    if lowered.startswith("settings:delivery:slot:set:"):
        parts = lowered.split(":")
        if len(parts) >= 6:
            return "channel", f"{parts[4]} {parts[5]}"
    if lowered in {"time", "/time", "schedule", "/schedule", "время", "расписание"}:
        return "time", None
    if lowered.startswith("timezone ") or lowered.startswith("/timezone "):
        parts = raw.replace("/timezone", "timezone", 1).split(maxsplit=1)
        return "timezone", parts[1].strip() if len(parts) == 2 else ""
    if lowered.startswith("quiet ") or lowered.startswith("/quiet "):
        parts = raw.replace("/quiet", "quiet", 1).split(maxsplit=1)
        return "quiet", parts[1].strip() if len(parts) == 2 else ""
    if lowered.startswith("channel ") or lowered.startswith("/channel "):
        parts = raw.replace("/channel", "channel", 1).split(maxsplit=2)
        if len(parts) >= 3:
            return "channel", f"{parts[1].strip()} {parts[2].strip()}"
        return "channel", ""
    if lowered in {"demo", "/demo", "демо", "попробовать бесплатно", "🌿 попробовать бесплатно", "бесплатная практика"}:
        return "demo", None
    if lowered in {"demo_work", "1", "1.", "1️⃣", "1️⃣ утро / дорога", "утро", "утро / дорога", "дорога на работу", "практика на утро / дорогу"}:
        return "demo_work", None
    if lowered in {"demo_home", "2", "2.", "2️⃣", "2️⃣ вечер / домой", "вечер", "вечер / домой", "дорога домой", "практика на вечер / домой"}:
        return "demo_home", None
    if lowered in {"full", "/full", "полный маршрут", "🔐 полный маршрут", "полный доступ"}:
        return "full", None
    if lowered in {"weather", "/weather", "погода", "🌤 погода"}:
        return "weather", None
    if lowered in {"weather_city", "/weather_city", "город", "изменить город", "🏙 изменить город", "сменить город"}:
        return "weather_city", None
    if lowered in {"pay", "/pay", "оплата", "оплатить", "тарифы", "💳 тарифы"}:
        return "pay", None
    if lowered in {"gift", "/gift", "подарок", "подарить", "🎁 подарить"}:
        return "gift", None
    if lowered.startswith("/platform") or lowered.startswith("platform "):
        parts = raw.replace("/platform", "platform", 1).split(maxsplit=1)
        value = parts[1].strip() if len(parts) == 2 else ""
        return "platform", value
    return "menu", None



def _vk_pre_audio_score_text(kind: str, session_id: int) -> str:
    title = "утренней практики / дороги" if kind == "work" else "вечерней практики / дороги домой"
    return (
        f"🌿 Перед аудио для {title} оцените состояние сейчас.\n\n"
        "Это тот же шаг, что и в Telegram: сначала фиксируем состояние ДО практики, "
        "потом бот отправляет аудио.\n\n"
        f"{_score_scale_text()}\n\n"
        "Нажмите число ниже от -10 до +10. После выбора оценки аудио придёт прямо во ВКонтакте."
    )


def _start_vk_pre_audio_session(user_id: int, *, kind: str) -> MessengerReply:
    snapshot = get_progress_snapshot(int(user_id))
    item = snapshot.pending_item or snapshot.next_item
    if item is None:
        return MessengerReply(
            text=(
                "✅ Все доступные аудио уже выданы.\n\n"
                "Можно нажать «📊 Прогресс» или «🧾 История». "
                "Когда появятся новые аудио, кнопка «🎧 Получить аудио» снова начнёт цикл со шкалы ДО прослушивания."
            )
        )

    day = datetime.now(timezone.utc).date().isoformat()
    slot = "morning" if kind == "work" else "evening"
    session_id = create_session(
        int(user_id),
        kind=kind,
        source="settings",
        day=day,
        slot=slot,
        scheduled_at=None,
        anchor_id=int(item.anchor),
    )

    return MessengerReply(
        text=_vk_pre_audio_score_text(kind, int(session_id)),
        meta={"vk_keyboard": "score_scale", "session_id": str(int(session_id)), "stage": "pre"},
    )



def _kind_for_audio_item(item: object | None) -> str:
    """Infer route kind for continuing the shared audio queue.

    This is only a continuation fallback. First-time users should still choose
    route via “Попробовать бесплатно”.
    """
    title = str(getattr(item, "title", "") or "").casefold()
    anchor = getattr(item, "anchor", None)

    if "evening" in title or "home" in title or "вечер" in title or "дом" in title:
        return "home"

    try:
        if int(str(anchor)) % 2 == 0:
            return "home"
    except Exception:
        pass

    return "work"


def _continue_vk_audio_session(user_id: int) -> MessengerReply:
    """Canonical meaning of “Получить аудио” in VK/MAX text UI.

    “Попробовать бесплатно” is route selection.
    “Получить аудио” is continuation.

    If the user is brand new, do not silently pick morning/work. Send them to
    the free route chooser so the behavior is explicit and Telegram-like.
    """
    snapshot = get_progress_snapshot(int(user_id))

    if snapshot.pending_item is not None:
        return MessengerReply(kind="next_audio")

    if snapshot.last_anchor is None:
        return MessengerReply(text=_demo_text(), meta={"vk_keyboard": "demo_kind"})

    if snapshot.next_item is not None:
        kind = _kind_for_audio_item(snapshot.next_item)
        return _start_vk_pre_audio_session(int(user_id), kind=kind)

    return MessengerReply(
        text=(
            "✅ Все доступные аудио уже выданы.\n\n"
            "Можно нажать «📊 Прогресс» или «🧾 История».\n"
            "Если хотите переслушать последнюю практику — отправьте repeat / повторить.\n"
            "Если хотите начать маршрут заново — нажмите «🌿 Попробовать бесплатно» и выберите маршрут."
        )
    )



def _repeat_last_vk_audio(user_id: int) -> MessengerReply:
    """Repeat the pending or last confirmed audio without advancing the queue."""
    snapshot = get_progress_snapshot(int(user_id))
    item = snapshot.pending_item
    if item is None and snapshot.last_anchor is not None:
        item = get_audio_item_by_anchor(int(snapshot.last_anchor))
    if item is None:
        return MessengerReply(text=_demo_text(), meta={"vk_keyboard": "demo_kind"})
    return MessengerReply(kind="next_audio", meta={"replay": "1"})


def handle_incoming_text(
    user_id: int,
    *,
    platform: str,
    external_user_id: str | None,
    text: str,
    username: str | None = None,
    display_name: str | None = None,
    first_name: str | None = None,
) -> tuple[int, list[MessengerReply]]:
    score_value = parse_score_text(text)
    action, value = _parse_command(text)
    norm_platform = normalize_platform(platform)
    raw_lower = (text or "").strip().lower()
    payload = value if action == "start" else None
    entry = register_user_entry(
        int(user_id),
        platform=platform,
        external_user_id=external_user_id,
        username=username,
        display_name=display_name,
        first_name=first_name,
        start_payload=payload,
    )
    canonical_user_id = int(entry.user_id)

    if raw_lower in {"pay", "/pay", "оплата", "оплатить", "тарифы", "💳 тарифы"}:
        return canonical_user_id, [
            MessengerReply(
                text=_payment_text(
                    canonical_user_id,
                    platform=norm_platform,
                    external_user_id=external_user_id,
                )
            )
        ]

    if action == "continue" and norm_platform == "telegram":
        return canonical_user_id, [MessengerReply(kind="next_audio")]

    if action == "done" and norm_platform == "telegram":
        try:
            confirmed = confirm_pending_audio_delivery(canonical_user_id, platform=norm_platform)
        except TypeError:
            confirmed = confirm_pending_audio_delivery(canonical_user_id)

        if confirmed is None:
            return canonical_user_id, [
                MessengerReply(
                    text="Сейчас нет аудио, которое ожидает подтверждения. Нажмите «🎧 Получить аудио» или отправьте continue."
                )
            ]

        done_replies = [
            MessengerReply(
                text=(
                    f"✅ Подтвердил аудио №{confirmed.anchor} — {confirmed.title}.\\n\\n"
                    "Теперь оцените состояние ПОСЛЕ прослушивания.\\n"
                    f"{_score_scale_text()}"
                ),
                meta={"vk_keyboard": "score_scale"},
            )
        ]
        if norm_platform == "telegram":
            done_replies.append(MessengerReply(kind="next_audio"))
        return canonical_user_id, done_replies



    if score_value is not None and action == "menu":
        # Telegram has explicit mood:post callbacks. VK regular keyboards may
        # deliver only the visible label ("3"), so remember the post-score stage
        # after "done" and give it priority over any stale pre-score sessions.
        pending_score = peek_pending(canonical_user_id)
        if pending_score and pending_score.kind == "mood_post_score":
            pop_pending(canonical_user_id)
            action, value = "post_score", str(score_value)
        else:
            stage = _pending_score_stage(canonical_user_id)
            if stage == "post":
                action, value = "post_score", str(score_value)
            elif stage == "pre":
                action, value = "pre_score", str(score_value)

    command_text = (text or "").strip()
    command_norm = command_text.casefold().replace("ё", "е")

    pending = peek_pending(canonical_user_id)
    if pending and pending.kind == "set_time":
        if command_norm not in {"start", "menu", "/start", "/menu", "отмена", "cancel", "⬅️ меню", "⬅️ назад"}:
            pending = pop_pending(canonical_user_id)
            slot = ((pending.data or {}).get("slot") if pending else "work") or "work"
            return canonical_user_id, [MessengerReply(text=_save_text_time(canonical_user_id, slot, command_text))]
        pop_pending(canonical_user_id)

    if pending and pending.kind == "weather_city":
        # Let explicit navigation commands still work instead of treating them as city names.
        if command_norm not in {"start", "menu", "/start", "/menu", "отмена", "cancel", "⬅️ меню"}:
            pop_pending(canonical_user_id)
            return canonical_user_id, [
                MessengerReply(kind="weather_set_city", meta={"city": command_text})
            ]
        pop_pending(canonical_user_id)

    if pending and pending.kind == "gift_recipient":
        if command_norm not in {"start", "menu", "/start", "/menu", "отмена", "cancel", "⬅️ меню", "⬅️ назад"}:
            pop_pending(canonical_user_id)
            return canonical_user_id, [
                _gift_payment_after_recipient(
                    canonical_user_id,
                    platform=platform,
                    external_user_id=external_user_id,
                    recipient_hint=command_text,
                )
            ]
        pop_pending(canonical_user_id)

    payment_aliases = {
        "pay",
        "payment",
        "оплата",
        "оплатить",
        "платеж",
        "платёж",
        "💳 оплатить",
        "💳 оплата",
    }
    gift_aliases = {
        "gift",
        "подарить",
        "подарок",
        "🎁 подарить",
        "🎁 подарок",
    }

    if command_norm in payment_aliases:
        return canonical_user_id, [
            MessengerReply(
                text=_payment_text(
                    canonical_user_id,
                    platform=platform,
                    external_user_id=external_user_id,
                )
            )
        ]

    if command_norm in gift_aliases:
        return canonical_user_id, [_start_gift_recipient_flow(canonical_user_id)]

    norm_platform = normalize_platform(platform)

    if action == "pay":
        return canonical_user_id, [
            MessengerReply(
                text=_payment_text(
                    canonical_user_id,
                    platform=norm_platform,
                    external_user_id=external_user_id,
                )
            )
        ]

    if action == "gift":
        return canonical_user_id, [_start_gift_recipient_flow(canonical_user_id)]

    if action in {"start", "menu"}:
        menu_replies: list[MessengerReply] = []
        if entry.linked_via_bridge:
            menu_replies.append(MessengerReply(text=_bridge_linked_text(canonical_user_id, platform)))
            if _should_auto_resume_after_bridge(canonical_user_id):
                menu_replies.append(MessengerReply(kind='next_audio'))
                return canonical_user_id, menu_replies
        menu_replies.append(MessengerReply(text=_menu_text(canonical_user_id)))
        return canonical_user_id, menu_replies
    if action == "help":
        return canonical_user_id, [MessengerReply(text=_help_text())]
    if action == "settings":
        return canonical_user_id, [MessengerReply(text=_settings_text(canonical_user_id))]
    if action == "demo":
        return canonical_user_id, [MessengerReply(text=_demo_text(), meta={"vk_keyboard": "demo_kind"})]
    if action in {"demo_work", "demo_home"}:
        kind = "work" if action == "demo_work" else "home"
        return canonical_user_id, [_start_vk_pre_audio_session(canonical_user_id, kind=kind)]
    if action == "share":
        return canonical_user_id, [MessengerReply(text=_share_text(canonical_user_id))]
    if action == "switch":
        return canonical_user_id, [MessengerReply(text=_switch_text(canonical_user_id))]
    if action == "repeat_audio":
        return canonical_user_id, [_repeat_last_vk_audio(canonical_user_id)]
    if action == "continue":
        return canonical_user_id, [_continue_vk_audio_session(canonical_user_id)]
    if action == "pre_score":
        return canonical_user_id, [MessengerReply(kind='auto_pre_score', meta={'score': str(value or '')})]
    if action == "post_score":
        return canonical_user_id, [MessengerReply(kind='auto_post_score', meta={'score': str(value or '')})]
    if action == "done":
        pending_post_session_id = find_pending_post_session_id(canonical_user_id)
        confirmed = confirm_pending_audio_delivery(canonical_user_id, platform=platform)
        if pending_post_session_id is None:
            pending_post_session_id = find_pending_post_session_id(canonical_user_id)

        if confirmed is None and pending_post_session_id is None:
            return canonical_user_id, [
                MessengerReply(
                    text=(
                        'ℹ️ Сейчас нет аудио, ожидающего подтверждения.\n\n'
                        'Чтобы начать новый цикл, нажмите «🎧 Получить аудио»: '
                        'сначала появится шкала ДО, потом аудио, потом кнопка «✅ Прослушал».'
                    )
                )
            ]

        if confirmed is None:
            set_pending(canonical_user_id, "mood_post_score", {"session_id": str(pending_post_session_id or 0)})
            return canonical_user_id, [
                MessengerReply(
                    text=(
                        '✅ Принял: аудио уже было отмечено как доставленное во ВКонтакте.\n\n'
                        'Теперь оцените состояние ПОСЛЕ прослушивания.\n'
                        f'{_score_scale_text()}'
                    ),
                    meta={'vk_keyboard': 'score_scale', 'session_id': str(pending_post_session_id or 0), 'stage': 'post'},
                ),
            ]

        set_pending(canonical_user_id, "mood_post_score", {"session_id": str(pending_post_session_id or 0)})
        return canonical_user_id, [
            MessengerReply(
                text=(
                    f'✅ Подтвердил аудио №{confirmed.anchor} — {confirmed.title}.\n\n'
                    'Теперь оцените состояние ПОСЛЕ прослушивания.\n'
                    f'{_score_scale_text()}'
                ),
                meta={'vk_keyboard': 'score_scale', 'session_id': str(pending_post_session_id or 0), 'stage': 'post'},
            ),
        ]
    if action == "progress":
        return canonical_user_id, [
            MessengerReply(text=_progress_text(canonical_user_id)),
            MessengerReply(kind='progress_chart'),
        ]
    if action == "history":
        return canonical_user_id, [MessengerReply(text=_history_text(canonical_user_id))]
    if action == "remind_continue_tomorrow":
        return canonical_user_id, [MessengerReply(text=_schedule_continue_tomorrow_text(canonical_user_id))]

    if action == "state_rate_menu":
        return canonical_user_id, [MessengerReply(text=_state_rate_prompt_text(), meta={"vk_keyboard": "state_rate"})]

    if action == "state_rate_save":
        return canonical_user_id, [MessengerReply(text=_save_state_rate_text(canonical_user_id, value), meta={"vk_keyboard": "state_period"})]

    if action == "state_period":
        return canonical_user_id, [
            MessengerReply(text=_progress_text(canonical_user_id), meta={"vk_keyboard": "state_period", "period": str(value or "all")}),
            MessengerReply(kind="progress_chart", meta={"period": str(value or "all")}),
        ]


    if action == "settings_time":
        slot = "home" if value == "home" else "work"
        set_pending(canonical_user_id, "set_time", {"slot": slot})
        return canonical_user_id, [MessengerReply(text=_text_time_prompt(slot))]

    if action == "settings_ref":
        return canonical_user_id, [MessengerReply(text=_ref_bonus_text(canonical_user_id))]

    if action == "settings_delivery_channels":
        return canonical_user_id, [MessengerReply(text=_delivery_channels_text(canonical_user_id))]

    if action == "settings_delivery_slot":
        slot = "evening" if value == "evening" else "morning"
        return canonical_user_id, [MessengerReply(text=_delivery_slot_text(canonical_user_id, slot))]

    if action == "time":
        morning_decision = build_delivery_policy_decision(canonical_user_id, "morning")
        evening_decision = build_delivery_policy_decision(canonical_user_id, "evening")
        return canonical_user_id, [MessengerReply(text=(
            "🕒 Правила отправки\n\n" + describe_delivery_preferences(canonical_user_id)
            + f"\n\nУтреннее касание сейчас пойдёт через: {platform_title(morning_decision.resolved_channel)}"
            + (" (fallback)" if morning_decision.fallback_used else "")
            + f"\nВечернее касание сейчас пойдёт через: {platform_title(evening_decision.resolved_channel)}"
            + (" (fallback)" if evening_decision.fallback_used else "")
            + "\n\nЧтобы поменять часовой пояс, отправьте timezone Europe/Amsterdam. Чтобы задать тихие часы, отправьте quiet 22:00-08:00 или quiet off. Для выбора канала: channel morning max / channel evening auto."
        ))]
    if action == "timezone":
        try:
            tz_name = set_user_timezone(canonical_user_id, value or "")
        except (ValueError, KeyError):
            return canonical_user_id, [MessengerReply(text='Пожалуйста, укажите корректный IANA timezone, например timezone Europe/Amsterdam.')]
        return canonical_user_id, [MessengerReply(text=f'✅ Часовой пояс сохранён: {tz_name}.\n\n{describe_delivery_preferences(canonical_user_id)}')]
    if action == "quiet":
        raw = (value or '').strip().lower()
        if raw in {'off', 'none', 'disable', 'выкл', 'отключить'}:
            clear_quiet_hours(canonical_user_id)
            return canonical_user_id, [MessengerReply(text=f'✅ Тихие часы выключены.\n\n{describe_delivery_preferences(canonical_user_id)}')]
        if '-' not in raw:
            return canonical_user_id, [MessengerReply(text='Укажите quiet в формате quiet 22:00-08:00 или quiet off.')]
        start_hhmm, end_hhmm = [part.strip() for part in raw.split('-', 1)]
        try:
            start_hhmm, end_hhmm = set_quiet_hours(canonical_user_id, start_hhmm, end_hhmm)
        except (ValueError, KeyError):
            return canonical_user_id, [MessengerReply(text='Не смог распознать тихие часы. Пример: quiet 22:00-08:00.')]
        return canonical_user_id, [MessengerReply(text=f'✅ Тихие часы сохранены: {start_hhmm}-{end_hhmm}.\n\n{describe_delivery_preferences(canonical_user_id)}')]
    if action == "channel":
        parts = (value or "").lower().split()
        if len(parts) != 2 or parts[0] not in {"morning", "evening"}:
            return canonical_user_id, [MessengerReply(text="Используйте: channel morning telegram|max|vk|auto или channel evening telegram|max|vk|auto.")]
        slot, platform_value = parts[0], parts[1].lower()
        if platform_value == "auto":
            set_slot_channel(canonical_user_id, slot, None)
            selected_text = "авто"
        elif platform_value in {"telegram", "max", "vk"}:
            chosen = set_slot_channel(canonical_user_id, slot, platform_value)
            selected_text = platform_title(chosen)
        else:
            return canonical_user_id, [MessengerReply(text="Допустимые каналы: telegram, max, vk, auto.")]
        decision = build_delivery_policy_decision(canonical_user_id, slot)
        note = f"\nСейчас проект фактически отправит через {platform_title(decision.resolved_channel)}."
        if decision.fallback_used:
            note += " Выбранный канал пока недоступен, поэтому сработает fallback."
        label = 'утренних' if slot == 'morning' else 'вечерних'
        return canonical_user_id, [MessengerReply(text=f"✅ Канал для {label} отправок обновлён: {selected_text}.\n\n{describe_delivery_preferences(canonical_user_id)}{note}")]
    if action == "full":
        return canonical_user_id, [MessengerReply(text=_full_route_text(canonical_user_id))]
    if action == "weather":
        return canonical_user_id, [
            MessengerReply(text=_weather_text(), meta={"vk_keyboard": "weather"}),
            MessengerReply(kind="weather_show"),
        ]
    if action == "weather_city":
        set_pending(canonical_user_id, "weather_city", {})
        return canonical_user_id, [
            MessengerReply(text=_weather_city_prompt_text(), meta={"vk_keyboard": "weather_city"})
        ]
    if action == "platform":
        raw_platform = (value or "").strip().lower()
        if raw_platform not in {"telegram", "max", "vk"}:
            return canonical_user_id, [MessengerReply(text="Используйте: /platform telegram | /platform max | /platform vk.")]
        norm = normalize_platform(raw_platform)
        return canonical_user_id, [MessengerReply(text=_platform_changed_text(canonical_user_id, norm)), MessengerReply(text=_settings_text(canonical_user_id))]
    return canonical_user_id, [MessengerReply(text=_menu_text(canonical_user_id))]
