from __future__ import annotations

"""Idempotency facade for the canonical cross-messenger mood/audio effect core."""

import asyncio
import sqlite3
from typing import Any

from services import mood_text_flow_core as _core
from services.auto_audio_recovery import acquire_delivery_lock
from services.db import mark_delivery_once, unmark_delivery, was_delivered
from services.idempotency_keys import for_demo_click, for_session
from services.messenger.audio_progress import get_pending_audio_item
from services.mood_text_flow_core import *  # noqa: F403

MoodTextFlowResult = _core.MoodTextFlowResult

_HOOK_NAMES = (
    "get_by_anchor",
    "pick_demo_file",
    "ensure_max_opus_file",
    "ensure_vk_opus_file",
)
_ORIGINAL_FACADE_HOOKS = {name: globals().get(name) for name in _HOOK_NAMES}
_ORIGINAL_CORE_HOOKS = {name: getattr(_core, name, None) for name in _HOOK_NAMES}


def _missing_mood_sessions_table(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).casefold()
    return "mood_sessions" in message and (
        "does not exist" in message or "no such table" in message
    )


def find_pending_pre_session_id(user_id: int) -> int | None:
    """Treat an absent imported mood schema as no pending legacy session."""

    try:
        return _core.find_pending_pre_session_id(int(user_id))
    except sqlite3.OperationalError as exc:
        if _missing_mood_sessions_table(exc):
            return None
        raise


def find_pending_post_session_id(user_id: int) -> int | None:
    """Treat an absent imported mood schema as no pending legacy session."""

    try:
        return _core.find_pending_post_session_id(int(user_id))
    except sqlite3.OperationalError as exc:
        if _missing_mood_sessions_table(exc):
            return None
        raise


def _sync_core_hooks() -> None:
    """Keep dependency injection attached to the stable public boundary.

    Public callers historically patch ``services.mood_text_flow`` while some
    hermetic probes patch the implementation core directly. Respect whichever
    side was intentionally overridden and never overwrite a direct core override
    with an untouched facade default.
    """

    for name in _HOOK_NAMES:
        facade_value = globals().get(name)
        core_value = getattr(_core, name, None)
        original_facade = _ORIGINAL_FACADE_HOOKS.get(name)
        original_core = _ORIGINAL_CORE_HOOKS.get(name)

        if facade_value is not original_facade:
            setattr(_core, name, facade_value)
        elif core_value is original_core:
            setattr(_core, name, facade_value)


def _idempotency_context(user_id: int, session: Any, session_id: int) -> tuple[str, str, str]:
    is_demo = str(session.source or "") == "demo"
    idem_kind = "demo" if is_demo else str(session.kind or "")
    idem_scheduled_at = str(
        for_demo_click(int(user_id), session_id=int(session_id))
        if is_demo
        else for_session(int(session_id))
    )
    sequence_key = "demo" if is_demo else "full_series"
    return idem_kind, idem_scheduled_at, sequence_key


def _expected_anchor(session: Any) -> int | None:
    _sync_core_hooks()
    if str(session.source or "") == "demo":
        item = _core._demo_item_for_kind(str(session.kind or "work"))
        return int(item.anchor) if item is not None else None
    return int(session.anchor_id) if session.anchor_id is not None else None


def _recover_sent_session_from_pending(
    user_id: int,
    session: Any,
    session_id: int,
    sequence_key: str,
) -> bool:
    """Close crash window: external send/pending marker succeeded before mood final marker."""

    if int(getattr(session, "audio_sent", 0) or 0) == 1:
        return True
    pending = get_pending_audio_item(int(user_id), sequence_key=sequence_key)
    expected_anchor = _expected_anchor(session)
    if pending is None or expected_anchor is None or int(pending.anchor) != int(expected_anchor):
        return False
    _core.mark_audio_sent(int(session_id))
    return True


def _cleanup_audio_lock(user_id: int, idem_kind: str, idem_scheduled_at: str) -> None:
    try:
        unmark_delivery(int(user_id), idem_kind, "audio_lock", idem_scheduled_at)
    except sqlite3.Error:
        _core.log.debug("audio delivery lock cleanup failed", exc_info=True)


async def complete_pre_score_and_send(
    user_id: int,
    *,
    platform: str,
    score: int,
    senders: Any,
    telegram_bot: Any | None = None,
    session_id: int | None = None,
) -> MoodTextFlowResult:
    _sync_core_hooks()
    resolved_session_id = (
        int(session_id)
        if session_id is not None
        else _core.find_pending_pre_session_id(int(user_id))
    )
    if resolved_session_id is None:
        return MoodTextFlowResult(False, "Сейчас нет активного ожидания оценки перед аудио.")
    session = _core.get_session(int(resolved_session_id))
    if session is None:
        return MoodTextFlowResult(False, "Не нашёл активную сессию оценки.")
    if int(session.user_id) != int(user_id):
        return MoodTextFlowResult(False, "Эта сессия принадлежит другому пользователю.")

    idem_kind, idem_scheduled_at, sequence_key = _idempotency_context(
        int(user_id),
        session,
        int(resolved_session_id),
    )
    if was_delivered(int(user_id), idem_kind, "audio", idem_scheduled_at):
        return MoodTextFlowResult(
            True,
            "🎧 Аудио по этой оценке уже было выдано. После прослушивания нажмите «Прослушал».",
            prompt_done=True,
            delivered_platform=platform,
            transport="already_sent",
        )
    audio_lock = await asyncio.to_thread(
        acquire_delivery_lock,
        int(user_id),
        idem_kind,
        "audio_lock",
        idem_scheduled_at,
        final_stage="audio",
    )
    if not audio_lock.acquired:
        return MoodTextFlowResult(
            True,
            "🎧 Эта практика уже отправляется или была выдана параллельным запросом. Подождите и нажмите «Прослушал» после получения аудио.",
            prompt_done=True,
            delivered_platform=platform,
            transport="idempotency_locked",
        )

    try:
        if _recover_sent_session_from_pending(
            int(user_id),
            session,
            int(resolved_session_id),
            sequence_key,
        ):
            mark_delivery_once(
                int(user_id),
                idem_kind,
                "audio",
                idem_scheduled_at,
            )
            return MoodTextFlowResult(
                True,
                "🎧 Аудио уже было отправлено. Повторное нажатие не списывает ещё одну практику.",
                prompt_done=True,
                delivered_platform=platform,
                transport="already_sent",
            )
        result = await _core.complete_pre_score_and_send(
            int(user_id),
            platform=platform,
            score=int(score),
            senders=senders,
            telegram_bot=telegram_bot,
            session_id=int(resolved_session_id),
        )
        session_after = _core.get_session(int(resolved_session_id))
        if session_after is not None and int(getattr(session_after, "audio_sent", 0) or 0) == 1:
            mark_delivery_once(
                int(user_id),
                idem_kind,
                "audio",
                idem_scheduled_at,
            )
        return result
    finally:
        _cleanup_audio_lock(int(user_id), idem_kind, idem_scheduled_at)
