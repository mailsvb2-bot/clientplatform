from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Awaitable, Callable

from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError
from aiogram.types import CallbackQuery, Message, TelegramObject

from core.task_manager import TaskManager
from services.messenger.observability import classify_messenger_action


class SlowHandlerLogMiddleware(BaseMiddleware):
    """Log slow update handling without retaining message or callback payloads."""

    def __init__(self, threshold_ms: int = 1200):
        super().__init__()
        self.threshold_ms = max(0, int(threshold_ms))
        self._log = logging.getLogger("perf")

    @staticmethod
    def _clean(value: Any, *, limit: int = 90) -> str:
        raw = str(value or "").replace("\n", " ").replace("\r", " ").strip()
        if len(raw) > limit:
            return raw[: limit - 1] + "…"
        return raw

    @staticmethod
    def _handler_label(data: dict[str, Any]) -> str:
        for key in ("handler", "event_handler"):
            obj = data.get(key)
            if obj is None:
                continue
            callback = getattr(obj, "callback", None)
            if callback is None:
                callback = obj
            module = getattr(callback, "__module__", "") or ""
            qualname = getattr(callback, "__qualname__", "") or getattr(callback, "__name__", "")
            label = ".".join(part for part in (module, qualname) if part)
            if label:
                return SlowHandlerLogMiddleware._clean(label, limit=120)
        return "-"

    @staticmethod
    def _event_details(event: TelegramObject) -> dict[str, Any]:
        details: dict[str, Any] = {
            "event": type(event).__name__,
            "inner": "-",
            "uid": None,
            "update_id": getattr(event, "update_id", None),
            "payload": "-",
        }

        inner = event
        for attr in (
            "callback_query",
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "my_chat_member",
            "chat_member",
        ):
            candidate = getattr(event, attr, None)
            if candidate is not None:
                inner = candidate
                details["inner"] = type(candidate).__name__
                break

        if details["inner"] == "-":
            details["inner"] = type(inner).__name__

        try:
            from_user = getattr(inner, "from_user", None) or getattr(inner, "event_from_user", None)
            if from_user is not None:
                details["uid"] = int(getattr(from_user, "id"))
        except (AttributeError, TypeError, ValueError):
            details["uid"] = None

        try:
            if type(inner).__name__ == "CallbackQuery":
                details["payload"] = "callback_action=" + classify_messenger_action(
                    getattr(inner, "data", "") or ""
                )
            elif type(inner).__name__ == "Message":
                text = (getattr(inner, "text", None) or getattr(inner, "caption", None) or "").strip()
                if text.startswith("/"):
                    details["payload"] = "message_action=" + classify_messenger_action(text)
                elif text:
                    details["payload"] = f"message_text_len={len(text)}"
                elif getattr(inner, "audio", None) is not None:
                    details["payload"] = "message_audio"
                elif getattr(inner, "voice", None) is not None:
                    details["payload"] = "message_voice"
                elif getattr(inner, "document", None) is not None:
                    details["payload"] = "message_document"
                elif getattr(inner, "photo", None) is not None:
                    details["payload"] = "message_photo"
                else:
                    details["payload"] = "message_other"
            else:
                details["payload"] = SlowHandlerLogMiddleware._clean(type(inner).__name__, limit=80)
        except (AttributeError, TypeError, ValueError, RuntimeError):
            details["payload"] = "-"

        return details

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        started = time.monotonic()
        try:
            return await handler(event, data)
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            if duration_ms >= self.threshold_ms:
                details = self._event_details(event)
                message = (
                    "SLOW update "
                    f"event={details['event']} "
                    f"inner={details['inner']} "
                    f"uid={details['uid']} "
                    f"update_id={details['update_id']} "
                    f"{details['payload']} "
                    f"handler={self._handler_label(data)} "
                    f"duration_ms={duration_ms}"
                )
                if duration_ms > 3000:
                    self._log.error(message)
                else:
                    self._log.warning(message)


class QuickAckCallbackMiddleware(BaseMiddleware):
    """Answer callbacks quickly while allowing retry after a failed acknowledgement."""

    @staticmethod
    def _patch_callback_answer(event: CallbackQuery) -> None:
        original_answer = event.answer
        answered = False
        answer_lock = asyncio.Lock()

        async def _safe_answer(*args: Any, **kwargs: Any) -> Any:
            nonlocal answered
            async with answer_lock:
                if answered:
                    return None
                try:
                    result = await original_answer(*args, **kwargs)
                except (
                    TelegramBadRequest,
                    TelegramNetworkError,
                    TelegramAPIError,
                    asyncio.TimeoutError,
                ):
                    return None
                answered = True
                return result

        if hasattr(original_answer, "calls"):
            _safe_answer.calls = original_answer.calls  # type: ignore[attr-defined]
        object.__setattr__(event, "answer", _safe_answer)  # type: ignore[arg-type]


    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery):
            self._patch_callback_answer(event)
            await event.answer(cache_time=0)
        return await handler(event, data)


class SoftRateLimitMiddleware(BaseMiddleware):
    """Apply per-user message and callback limits that cannot be bypassed by payload variation."""

    @staticmethod
    def _interval(value: float, default: float = 0.05) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(parsed) or parsed < 0:
            return default
        return min(parsed, 60.0)

    def __init__(self, callback_interval_sec: float = 1.0, message_interval_sec: float = 1.0):
        super().__init__()
        self.callback_interval_sec = self._interval(callback_interval_sec)
        self.message_interval_sec = self._interval(message_interval_sec)
        self._last_ts: dict[tuple[int, str], float] = {}
        self._last_cleanup_ts: float = 0.0

    def _limit_key(self, user_id: int, event: TelegramObject) -> tuple[tuple[int, str] | None, float]:
        if isinstance(event, CallbackQuery):
            return (int(user_id), "callback"), self.callback_interval_sec
        if isinstance(event, Message):
            return (int(user_id), "message"), self.message_interval_sec
        return None, 0.0

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id

        if user_id:
            now = time.monotonic()
            if (now - self._last_cleanup_ts) > 600 and len(self._last_ts) > 2000:
                cutoff = now - 7200
                self._last_ts = {key: ts for key, ts in self._last_ts.items() if ts >= cutoff}
                self._last_cleanup_ts = now
            key, min_interval = self._limit_key(int(user_id), event)
            if key is not None and min_interval > 0:
                last = self._last_ts.get(key, 0.0)
                if now - last < min_interval:
                    if isinstance(event, CallbackQuery):
                        try:
                            await event.answer("Секунду…", show_alert=False)
                        except (TelegramBadRequest, TelegramNetworkError, TelegramAPIError):
                            logging.getLogger(__name__).debug("Callback answer failed", exc_info=True)
                    elif isinstance(event, Message):
                        try:
                            await event.answer("Секунду…")
                        except (TelegramBadRequest, TelegramNetworkError, TelegramAPIError):
                            logging.getLogger(__name__).debug("Message answer failed", exc_info=True)
                    return None
                self._last_ts[key] = now

        return await handler(event, data)
