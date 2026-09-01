from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, User

from core import middlewares as mw


def user(uid: int = 7) -> User:
    return User.model_construct(id=uid, is_bot=False, first_name="User")


def message(text: str | None = "hello", uid: int | None = 7) -> Message:
    return Message.model_construct(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat.model_construct(id=uid or 1, type="private"),
        from_user=user(uid) if uid is not None else None,
        text=text,
    )


def callback(data: str | None = "menu", uid: int | None = 7) -> CallbackQuery:
    return CallbackQuery.model_construct(
        id="cb",
        from_user=user(uid) if uid is not None else None,
        chat_instance="chat",
        data=data,
    )


async def handler_result(_event: TelegramObject, _data: dict[str, Any]) -> str:
    return "ok"


class Clock:
    def __init__(self, *values: float) -> None:
        self.values = list(values)
        self.last = self.values[-1] if self.values else 0.0

    def monotonic(self) -> float:
        if self.values:
            self.last = self.values.pop(0)
        return self.last


def test_slow_helpers_and_event_details(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mw.SlowHandlerLogMiddleware._clean("  a\nb  ") == "a b"
    assert mw.SlowHandlerLogMiddleware._clean("x" * 100, limit=10) == "x" * 9 + "…"

    def sample() -> None:
        return None

    assert "sample" in mw.SlowHandlerLogMiddleware._handler_label({"handler": SimpleNamespace(callback=sample)})
    assert mw.SlowHandlerLogMiddleware._handler_label({}) == "-"
    monkeypatch.setattr(mw, "classify_messenger_action", lambda value: f"action:{value}")

    details = mw.SlowHandlerLogMiddleware._event_details(callback("pay"))
    assert details["uid"] == 7
    assert details["payload"] == "callback_action=action:pay"
    assert "message_action=action:/start secret" in mw.SlowHandlerLogMiddleware._event_details(message("/start secret"))["payload"]
    assert mw.SlowHandlerLogMiddleware._event_details(message("plain"))["payload"] == "message_text_len=5"

    for field_name, expected in (
        ("audio", "message_audio"),
        ("voice", "message_voice"),
        ("document", "message_document"),
        ("photo", "message_photo"),
    ):
        msg = message(None)
        object.__setattr__(msg, field_name, object())
        assert mw.SlowHandlerLogMiddleware._event_details(msg)["payload"] == expected

    assert mw.SlowHandlerLogMiddleware._event_details(message(None))["payload"] == "message_other"
    assert mw.SlowHandlerLogMiddleware._event_details(SimpleNamespace(from_user=SimpleNamespace(id="bad")))["uid"] is None


@pytest.mark.asyncio
async def test_slow_middleware_warning_error_and_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = mw.SlowHandlerLogMiddleware(threshold_ms=1000)
    warnings: list[str] = []
    errors: list[str] = []
    middleware._log = SimpleNamespace(warning=warnings.append, error=errors.append)

    monkeypatch.setattr(mw, "time", Clock(10.0, 11.5))
    assert await middleware(handler_result, message("x"), {}) == "ok"
    assert "duration_ms=1500" in warnings[-1]

    monkeypatch.setattr(mw, "time", Clock(20.0, 24.1))

    async def failing(_event: TelegramObject, _data: dict[str, Any]) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await middleware(failing, message("x"), {})
    assert "duration_ms=4100" in errors[-1]

    monkeypatch.setattr(mw, "time", Clock(30.0, 30.1))
    await mw.SlowHandlerLogMiddleware(threshold_ms=1000)(handler_result, message("x"), {})
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_quick_ack_retries_and_deduplicates() -> None:
    cb = callback("menu")
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def original(*args: Any, **kwargs: Any) -> str:
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise TelegramBadRequest(method=None, message="temporary")
        return "answered"

    original.calls = calls  # type: ignore[attr-defined]
    object.__setattr__(cb, "answer", original)
    mw.QuickAckCallbackMiddleware._patch_callback_answer(cb)
    assert getattr(cb.answer, "calls") is calls
    assert await cb.answer() is None
    assert await cb.answer(text="ok") == "answered"
    assert await cb.answer(text="again") is None
    assert len(calls) == 2


def test_rate_limit_interval_and_keys() -> None:
    assert mw.SoftRateLimitMiddleware._interval("0.5") == 0.5
    assert mw.SoftRateLimitMiddleware._interval("bad") == 0.05
    assert mw.SoftRateLimitMiddleware._interval(float("nan")) == 0.05
    assert mw.SoftRateLimitMiddleware._interval(-1) == 0.05
    assert mw.SoftRateLimitMiddleware._interval(100) == 60.0
    limiter = mw.SoftRateLimitMiddleware(1, 2)
    assert limiter._limit_key(7, callback()) == ((7, "callback"), 1.0)
    assert limiter._limit_key(7, message()) == ((7, "message"), 2.0)
    assert limiter._limit_key(7, TelegramObject.model_construct()) == (None, 0.0)


@pytest.mark.asyncio
async def test_rate_limit_message_callback_cleanup_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = mw.SoftRateLimitMiddleware(callback_interval_sec=1, message_interval_sec=1)
    monkeypatch.setattr(mw, "time", Clock(100.0, 100.2, 101.5, 102.0, 102.1))

    msg = message("hello")
    msg_answers: list[str] = []

    async def msg_answer(text: str) -> None:
        msg_answers.append(text)

    object.__setattr__(msg, "answer", msg_answer)
    assert await limiter(handler_result, msg, {}) == "ok"
    assert await limiter(handler_result, msg, {}) is None
    assert msg_answers == ["Секунду…"]
    assert await limiter(handler_result, msg, {}) == "ok"

    cb = callback("x")
    cb_answers: list[str] = []

    async def cb_answer(text: str, show_alert: bool = False) -> None:
        cb_answers.append(text)

    object.__setattr__(cb, "answer", cb_answer)
    assert await limiter(handler_result, cb, {}) == "ok"
    assert await limiter(handler_result, cb, {}) is None
    assert cb_answers == ["Секунду…"]

    limiter._last_cleanup_ts = 0
    limiter._last_ts = {(i, "message"): 1.0 for i in range(2101)}
    monkeypatch.setattr(mw, "time", Clock(10000.0))
    assert await limiter(handler_result, message("x", uid=5000), {}) == "ok"
    assert limiter._last_cleanup_ts == 10000.0

    async def error_answer(*_args: Any, **_kwargs: Any) -> None:
        raise TelegramBadRequest(method=None, message="failed")

    error_msg = message("x")
    object.__setattr__(error_msg, "answer", error_answer)
    limiter._last_ts[(7, "message")] = 10000.0
    monkeypatch.setattr(mw, "time", Clock(10000.1))
    assert await limiter(handler_result, error_msg, {}) is None

    error_cb = callback("x")
    object.__setattr__(error_cb, "answer", error_answer)
    limiter._last_ts[(7, "callback")] = 10000.1
    monkeypatch.setattr(mw, "time", Clock(10000.2))
    assert await limiter(handler_result, error_cb, {}) is None
