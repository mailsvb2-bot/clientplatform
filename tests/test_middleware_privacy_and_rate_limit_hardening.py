from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from core import middlewares


@pytest.mark.asyncio
async def test_quick_ack_allows_retry_after_failed_network_ack() -> None:
    calls = 0

    async def original_answer(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.TimeoutError
        return "ok"

    event = SimpleNamespace(answer=original_answer)
    middlewares.QuickAckCallbackMiddleware._patch_callback_answer(event)

    assert await event.answer() is None
    assert await event.answer() == "ok"
    assert await event.answer() is None
    assert calls == 2


@pytest.mark.asyncio
async def test_rate_limit_cannot_be_bypassed_by_changing_callback_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCallback:
        def __init__(self, payload: str) -> None:
            self.data = payload
            self.from_user = SimpleNamespace(id=7)
            self.answers: list[str] = []

        async def answer(self, text: str, **_kwargs) -> None:
            self.answers.append(text)

    monkeypatch.setattr(middlewares, "CallbackQuery", FakeCallback)
    ticks = iter([100.0, 100.1])
    monkeypatch.setattr(
        middlewares,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks)),
    )
    limiter = middlewares.SoftRateLimitMiddleware(
        callback_interval_sec=1.0,
        message_interval_sec=1.0,
    )
    handled: list[str] = []

    async def handler(event, _data):
        handled.append(event.data)
        return "handled"

    first = FakeCallback("menu:first")
    second = FakeCallback("payment:second-secret-payload")

    assert await limiter(handler, first, {}) == "handled"
    assert await limiter(handler, second, {}) is None
    assert handled == ["menu:first"]
    assert second.answers == ["Секунду…"]


def test_rate_limit_rejects_nan_and_infinity() -> None:
    limiter = middlewares.SoftRateLimitMiddleware(
        callback_interval_sec=float("nan"),
        message_interval_sec=float("inf"),
    )

    assert limiter.callback_interval_sec == 0.05
    assert limiter.message_interval_sec == 0.05


def test_slow_callback_diagnostics_classify_instead_of_logging_payload() -> None:
    FakeCallback = type(
        "CallbackQuery",
        (),
        {
            "data": "cpm:SUPER_SECRET_CAPABILITY",
            "from_user": SimpleNamespace(id=9),
        },
    )

    details = middlewares.SlowHandlerLogMiddleware._event_details(FakeCallback())

    assert details["payload"] == "callback_action=cpm"
    assert "SUPER_SECRET_CAPABILITY" not in str(details)


@pytest.mark.asyncio
async def test_slow_handler_logs_at_configured_threshold(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ticks = iter([10.0, 10.8])
    monkeypatch.setattr(
        middlewares,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks)),
    )
    middleware = middlewares.SlowHandlerLogMiddleware(threshold_ms=700)

    async def handler(_event, _data):
        return "ok"

    with caplog.at_level(logging.WARNING, logger="perf"):
        assert await middleware(handler, SimpleNamespace(), {}) == "ok"

    assert "duration_ms=800" in caplog.text
