from __future__ import annotations

from config.settings import settings
from core.time_utils import today_tz
from services.db import db
from services.delivery_preferences import get_user_timezone
from services.demo_analytics import demo_sent_kinds
from services.demo_policy import can_repeat_demo_for_user
from services.messenger.audio_progress import (
    SEQUENCE_FULL_SERIES,
    confirm_pending_audio_delivery,
    get_progress_snapshot,
)
from services.messenger.entrypoints import register_user_entry
from services.messenger import text_ui as legacy_text_ui
from services.messenger.text_ui import MessengerReply
from services.mood import create_session, get_session
from services.mood_text_flow import find_pending_post_session_id
from services.practice_journey import start_or_resume_paid_practice
from services.privacy_controls import erase_user_behavioral_data


PRIVACY_POLICY_URL = "https://t.me/metrotherapyprivacy"


def _privacy_action(text: str) -> tuple[str, bool] | None:
    raw = " ".join(str(text or "").strip().split())
    normalized = raw.casefold().replace("ё", "е")
    if normalized in {"privacy", "/privacy", "конфиденциальность", "политика", "политика конфиденциальности"}:
        return "privacy", False
    export_aliases = (
        "mydata",
        "/mydata",
        "мои данные",
        "экспорт данных",
        "выгрузить данные",
    )
    for alias in export_aliases:
        if normalized == alias:
            return "export", False
        if normalized == f"{alias} confirm":
            return "export", True

    delete_aliases = (
        "deletemydata",
        "/deletemydata",
        "удалить мои данные",
        "удалить данные",
    )
    for alias in delete_aliases:
        if normalized == alias:
            return "delete", False
        if normalized == f"{alias} confirm":
            return "delete", True
    return None


def _privacy_info_reply() -> MessengerReply:
    return MessengerReply(
        text=(
            "🔐 Конфиденциальность и Ваши данные\n\n"
            f"Политика: {PRIVACY_POLICY_URL}\n\n"
            "Получить сжатую копию данных: mydata — затем mydata CONFIRM\n"
            "Удалить поведенческую историю: deletemydata\n\n"
            "Экспорт и удаление требуют отдельного подтверждения словом CONFIRM. "
            "Платежные, возвратные и обязательные технические записи сохраняются "
            "для исполнения оплаченного доступа, предотвращения повторных операций и требований учета."
        )
    )


def _privacy_export_reply(*, confirmed: bool) -> MessengerReply:
    if confirmed:
        return MessengerReply(kind="privacy_export")
    return MessengerReply(
        text=(
            "⚠️ Экспорт может содержать историю использования, оценки состояния и платёжные записи. "
            "Архив сжат, но не зашифрован, и после отправки останется в истории этого чата.\n\n"
            "Для подтверждения отправьте точно:\n"
            "mydata CONFIRM"
        )
    )


def _privacy_delete_reply(user_id: int, *, platform: str, confirmed: bool) -> MessengerReply:
    if not confirmed:
        return MessengerReply(
            text=(
                "⚠️ Команда удалит поведенческую историю и очистит отображаемые данные профиля. "
                "Платежные, возвратные и обязательные технические записи сохранятся.\n\n"
                "Для подтверждения отправьте точно:\n"
                "deletemydata CONFIRM"
            )
        )

    result = erase_user_behavioral_data(
        int(user_id),
        reason=f"{str(platform or 'messenger').strip().lower()}_user_request",
    )
    deleted_rows = sum(int(value) for value in result.deleted_tables.values())
    return MessengerReply(
        text=(
            "✅ Поведенческие данные удалены, отображаемые данные профиля очищены.\n"
            f"Удалено записей: {deleted_rows}.\n"
            "Платежные и иные обязательные учетные записи сохранены для работы "
            "оплаченного доступа и требований учета."
        )
    )


def _demo_kind(action: str) -> str | None:
    if action == "demo_work":
        return "work"
    if action == "demo_home":
        return "home"
    return None


def _user_day(user_id: int) -> str:
    timezone_name = get_user_timezone(int(user_id)) or settings.TIMEZONE or "UTC"
    return today_tz(timezone_name).isoformat()


def _existing_unsent_demo_session(user_id: int, kind: str) -> int | None:
    with db() as conn:
        row = conn.execute(
            """
            SELECT id
            FROM mood_sessions
            WHERE user_id=? AND kind=? AND source='demo'
              AND COALESCE(audio_sent,0)=0
              AND post_score IS NULL
            ORDER BY id DESC
            LIMIT 1
            """.strip(),
            (int(user_id), str(kind)),
        ).fetchone()
    return int(row["id"]) if row else None


def _pending_demo_post_session(user_id: int, kind: str) -> int | None:
    with db() as conn:
        row = conn.execute(
            """
            SELECT id
            FROM mood_sessions
            WHERE user_id=? AND kind=? AND source='demo'
              AND COALESCE(audio_sent,0)=1
              AND pre_score IS NOT NULL
              AND post_score IS NULL
            ORDER BY id DESC
            LIMIT 1
            """.strip(),
            (int(user_id), str(kind)),
        ).fetchone()
    return int(row["id"]) if row else None


def _get_or_create_demo_session(user_id: int, kind: str) -> int:
    existing = _existing_unsent_demo_session(int(user_id), kind)
    if existing is not None:
        return existing
    return int(
        create_session(
            int(user_id),
            kind=kind,
            source="demo",
            day=_user_day(int(user_id)),
            slot="demo",
            scheduled_at=None,
            anchor_id=None,
        )
    )


def _register(
    user_id: int,
    *,
    platform: str,
    external_user_id: str | None,
    username: str | None,
    display_name: str | None,
    first_name: str | None,
    payload: str | None = None,
) -> int:
    entry = register_user_entry(
        int(user_id),
        platform=platform,
        external_user_id=external_user_id,
        username=username,
        display_name=display_name,
        first_name=first_name,
        start_payload=payload,
    )
    return int(entry.user_id)


def _demo_pre_score_reply(user_id: int, kind: str) -> MessengerReply:
    pending_post = _pending_demo_post_session(int(user_id), kind)
    if pending_post is not None:
        return MessengerReply(
            text=(
                "🎧 Эта бесплатная практика уже отправлена и ожидает завершения.\n\n"
                "Прослушайте аудио и отправьте done / готово / прослушал — затем я покажу шкалу ПОСЛЕ."
            )
        )

    sent = demo_sent_kinds(int(user_id))
    if not can_repeat_demo_for_user(int(user_id)) and kind in sent:
        return MessengerReply(
            text=(
                "✅ Эту бесплатную практику Вы уже получили.\n\n"
                "Повторная бесплатная выдача не создаётся. Для продолжения откройте пакеты практик или отправьте continue."
            )
        )
    if not can_repeat_demo_for_user(int(user_id)) and {"work", "home"}.issubset(sent):
        return MessengerReply(
            text=(
                "✅ Обе бесплатные практики уже использованы.\n\n"
                "Чтобы продолжить маршрут, откройте пакеты практик или отправьте continue."
            )
        )

    session_id = _get_or_create_demo_session(int(user_id), kind)
    title = (
        "утренней бесплатной практики / дороги"
        if kind == "work"
        else "вечерней бесплатной практики / дороги домой"
    )
    return MessengerReply(
        text=(
            f"🌿 Перед {title} оцените состояние сейчас.\n\n"
            "Сначала фиксируем состояние ДО, затем бот отправит именно demo-аудио. "
            "Бесплатная практика не расходует баланс.\n\n"
            "Шкала: −10 — очень тяжело, 0 — нейтрально, +10 — очень хорошо."
        ),
        meta={
            "vk_keyboard": "score_scale",
            "session_id": str(session_id),
            "stage": "pre",
        },
    )


def _paid_continue_reply(user_id: int) -> MessengerReply:
    start = start_or_resume_paid_practice(int(user_id))
    if start.ready_for_pre_score:
        return MessengerReply(
            text=start.message,
            meta={
                "vk_keyboard": "score_scale",
                "session_id": str(int(start.session_id)),
                "stage": "pre",
            },
        )
    if start.status == "pending_audio":
        return MessengerReply(kind="next_audio")
    return MessengerReply(text=start.message)


def _done_reply(user_id: int, *, platform: str) -> MessengerReply:
    session_id = find_pending_post_session_id(int(user_id))
    session = get_session(session_id) if session_id is not None else None
    if session is not None and int(session.user_id) == int(user_id):
        sequence_key = "demo" if str(session.source or "") == "demo" else SEQUENCE_FULL_SERIES
        confirm_pending_audio_delivery(
            int(user_id),
            platform=platform,
            sequence_key=sequence_key,
        )
        return MessengerReply(
            text=(
                "✅ Прослушивание подтверждено.\n\n"
                "Теперь оцените состояние ПОСЛЕ прослушивания от −10 до +10."
            ),
            meta={
                "vk_keyboard": "score_scale",
                "session_id": str(int(session_id)),
                "stage": "post",
            },
        )

    snapshot = get_progress_snapshot(int(user_id))
    if snapshot.pending_item is not None:
        confirmed = confirm_pending_audio_delivery(
            int(user_id),
            platform=platform,
            sequence_key=SEQUENCE_FULL_SERIES,
        )
        if confirmed is not None:
            return MessengerReply(
                text=(
                    f"✅ Подтвердил аудио №{confirmed.anchor} — {confirmed.title}.\n\n"
                    "У старой выдачи нет активной сессии оценки. Отправьте continue — следующее аудио начнётся правильно со шкалы ДО."
                )
            )

    return MessengerReply(
        text=(
            "ℹ️ Сейчас нет аудио, ожидающего подтверждения. "
            "Отправьте continue, чтобы продолжить маршрут."
        )
    )


def _harden_next_audio_replies(user_id: int, replies: list[MessengerReply]) -> list[MessengerReply]:
    """Prevent legacy/direct next_audio replies from skipping the pre-score step."""

    out: list[MessengerReply] = []
    for reply in replies:
        if reply.kind != "next_audio":
            out.append(reply)
            continue
        meta = reply.meta or {}
        if str(meta.get("replay") or "").strip().lower() in {"1", "true", "yes", "on"}:
            out.append(reply)
            continue

        start = start_or_resume_paid_practice(int(user_id))
        if start.status == "pending_audio":
            out.append(reply)
        elif start.ready_for_pre_score:
            out.append(
                MessengerReply(
                    text=start.message,
                    meta={
                        "vk_keyboard": "score_scale",
                        "session_id": str(int(start.session_id)),
                        "stage": "pre",
                    },
                )
            )
        else:
            out.append(MessengerReply(text=start.message))
    return out


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
    """Canonical cross-messenger user route for demo/paid practice boundaries."""

    privacy_action = _privacy_action(text)
    if privacy_action is not None:
        action_name, confirmed = privacy_action
        canonical_user_id = _register(
            user_id,
            platform=platform,
            external_user_id=external_user_id,
            username=username,
            display_name=display_name,
            first_name=first_name,
        )
        if action_name == "privacy":
            return canonical_user_id, [_privacy_info_reply()]
        if action_name == "export":
            return canonical_user_id, [_privacy_export_reply(confirmed=confirmed)]
        return canonical_user_id, [
            _privacy_delete_reply(
                canonical_user_id,
                platform=platform,
                confirmed=confirmed,
            )
        ]

    action, value = legacy_text_ui._parse_command(text)  # noqa: SLF001
    payload = value if action == "start" else None

    if action in {"demo_work", "demo_home", "continue", "done"}:
        canonical_user_id = _register(
            user_id,
            platform=platform,
            external_user_id=external_user_id,
            username=username,
            display_name=display_name,
            first_name=first_name,
            payload=payload,
        )
        kind = _demo_kind(action)
        if kind is not None:
            return canonical_user_id, [_demo_pre_score_reply(canonical_user_id, kind)]
        if action == "continue":
            return canonical_user_id, [_paid_continue_reply(canonical_user_id)]
        return canonical_user_id, [_done_reply(canonical_user_id, platform=platform)]

    canonical_user_id, replies = legacy_text_ui.handle_incoming_text(
        user_id,
        platform=platform,
        external_user_id=external_user_id,
        text=text,
        username=username,
        display_name=display_name,
        first_name=first_name,
    )
    return int(canonical_user_id), _harden_next_audio_replies(
        int(canonical_user_id),
        list(replies),
    )


__all__ = ["MessengerReply", "handle_incoming_text"]
