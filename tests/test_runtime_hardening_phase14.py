from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from services import charts


def test_libpq_timeouts_are_enforced_and_existing_options_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.db import runtime

    monkeypatch.setenv("PGOPTIONS", "-c application_name=metro -c statement_timeout=999999")
    monkeypatch.setenv("POSTGRES_CONNECT_TIMEOUT_SEC", "2.2")
    monkeypatch.setenv("POSTGRES_STATEMENT_TIMEOUT_SEC", "4")
    monkeypatch.setenv("POSTGRES_LOCK_TIMEOUT_SEC", "1.5")
    monkeypatch.setenv("POSTGRES_IDLE_TX_TIMEOUT_SEC", "9")

    runtime.configure_libpq_timeouts()

    assert os.environ["PGCONNECT_TIMEOUT"] == "3"
    assert "application_name=metro" in os.environ["PGOPTIONS"]
    assert os.environ["PGOPTIONS"].count("statement_timeout=") == 1
    assert "statement_timeout=4000ms" in os.environ["PGOPTIONS"]
    assert "lock_timeout=1500ms" in os.environ["PGOPTIONS"]
    assert "idle_in_transaction_session_timeout=9000ms" in os.environ["PGOPTIONS"]


def test_libpq_timeout_invalid_values_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.db import runtime

    monkeypatch.setenv("PGOPTIONS", "")
    monkeypatch.setenv("POSTGRES_CONNECT_TIMEOUT_SEC", "bad")
    monkeypatch.setenv("POSTGRES_STATEMENT_TIMEOUT_SEC", "0")
    monkeypatch.setenv("POSTGRES_LOCK_TIMEOUT_SEC", "-1")
    monkeypatch.setenv("POSTGRES_IDLE_TX_TIMEOUT_SEC", "bad")

    runtime.configure_libpq_timeouts()

    assert os.environ["PGCONNECT_TIMEOUT"] == "5"
    assert "statement_timeout=15000ms" in os.environ["PGOPTIONS"]
    assert "lock_timeout=3000ms" in os.environ["PGOPTIONS"]
    assert "idle_in_transaction_session_timeout=30000ms" in os.environ["PGOPTIONS"]


def test_chart_cache_is_ttl_and_size_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    charts._CHART_CACHE.clear()
    now = [100.0]
    monkeypatch.setattr(charts.time, "time", lambda: now[0])
    monkeypatch.setenv("CHART_CACHE_MAX_ENTRIES", "2")
    monkeypatch.setenv("CHART_CACHE_MAX_BYTES", "5")

    charts._chart_cache_put("a", b"12")
    charts._chart_cache_put("b", b"34")
    charts._chart_cache_put("c", b"5")
    assert list(charts._CHART_CACHE) == ["b", "c"]

    assert charts._chart_cache_get("b") == b"34"
    charts._chart_cache_put("d", b"6")
    assert list(charts._CHART_CACHE) == ["b", "d"]

    charts._chart_cache_put("oversized", b"123456")
    assert "oversized" not in charts._CHART_CACHE

    now[0] += charts._CHART_CACHE_TTL + 1
    assert charts._chart_cache_get("b") is None
    assert charts._CHART_CACHE == {}


@pytest.mark.asyncio
async def test_messenger_shutdown_always_cleans_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime import messenger_webhooks

    calls: list[str] = []

    async def fail_worker() -> None:
        calls.append("worker")
        raise RuntimeError("worker stop")

    class Runner:
        async def cleanup(self) -> None:
            calls.append("runner")

    monkeypatch.setattr(messenger_webhooks, "stop_delivery_worker", fail_worker)
    runtime = messenger_webhooks.MessengerWebhookRuntime(
        runner=Runner(),
        site=SimpleNamespace(),
        delivery_worker_started=True,
    )

    with pytest.raises(RuntimeError, match="worker stop"):
        await runtime.stop()
    assert calls == ["worker", "runner"]
    assert runtime.delivery_worker_started is False


@pytest.mark.asyncio
async def test_body_handler_offloads_sync_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from handlers.mood_flow import body

    calls: list[Any] = []
    message_answers: list[str] = []

    async def answer_callback(_cb: Any) -> None:
        return None

    async def direct_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(func)
        return func(*args, **kwargs)

    class Message:
        async def answer(self, text: str, **_kwargs: Any) -> None:
            message_answers.append(text)

    callback = SimpleNamespace(
        data="body:7:neck:0",
        from_user=SimpleNamespace(id=42),
    )
    session = SimpleNamespace(user_id=42, kind="work", source="demo")
    recorded: list[dict[str, Any]] = []

    monkeypatch.setattr(body, "safe_answer_callback", answer_callback)
    monkeypatch.setattr(body, "_callback_message", lambda _cb: Message())
    monkeypatch.setattr(body.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(body, "get_session", lambda sid: session if sid == 7 else None)
    monkeypatch.setattr(
        body,
        "pick_body_question",
        lambda **_kwargs: SimpleNamespace(options=["шея"]),
    )
    monkeypatch.setattr(
        body,
        "_record_body_answer_sync",
        lambda **kwargs: recorded.append(dict(kwargs)),
    )
    monkeypatch.setattr(body, "technique_for_area", lambda area: f"technique:{area}")
    monkeypatch.setattr(body, "kb_post_show_chart", lambda sid: f"chart:{sid}")

    await body.body_answer(callback)

    assert calls == [body.get_session, body._record_body_answer_sync]
    assert recorded == [
        {
            "user_id": 42,
            "session_id": 7,
            "kind": "work",
            "area": "шея",
            "source": "demo",
        }
    ]
    assert message_answers == ["technique:шея"]


def test_post_schedule_releases_idempotency_marker_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from handlers.mood_flow import body

    unmarked: list[tuple[Any, ...]] = []
    monkeypatch.setattr(body, "mark_delivery_once", lambda *_args: True)
    monkeypatch.setattr(
        body,
        "add_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("queue failed")),
    )
    monkeypatch.setattr(body, "unmark_delivery", lambda *args: unmarked.append(args))

    with pytest.raises(RuntimeError, match="queue failed"):
        body._persist_post_schedule_sync(
            session_id="7",
            user_id=42,
            kind="work",
            run_at_iso="2026-07-21T12:00:00+00:00",
            run_at_epoch=1,
        )
    assert unmarked == [(42, "work", "post_prompt_schedule", body.for_session("7"))]
