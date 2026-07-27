from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest

from handlers.flow import settings_core as sc


class FakeMessage:
    def __init__(self, *, text: str | None = None) -> None:
        self.text = text
        self.answers: list[tuple[str, Any]] = []
        self.edits: list[tuple[str, Any]] = []
        self.from_user = SimpleNamespace(id=42)

    async def answer(self, text: str, reply_markup: Any = None, **_kwargs: Any) -> None:
        self.answers.append((text, reply_markup))

    async def edit_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> None:
        self.edits.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data: str | None = None) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=42)
        self.message = object()


async def direct_to_thread(func, *args, **kwargs):
    return func(*args, **kwargs)


async def _async_none() -> None:
    return None


def install_callback(monkeypatch: pytest.MonkeyPatch, message: FakeMessage) -> None:
    monkeypatch.setattr(sc, "_callback_message", lambda _cb: message)
    monkeypatch.setattr(sc, "safe_answer_callback", lambda _cb: _async_none())
    monkeypatch.setattr(sc, "_to_thread", direct_to_thread)


def test_parse_hhmm_and_message_identity() -> None:
    assert sc._parse_hhmm("8:5") == "08:05"
    assert sc._parse_hhmm("23:59") == "23:59"
    for raw in ("", "8", "aa:10", "24:00", "10:60"):
        assert sc._parse_hhmm(raw) is None
    assert sc._message_user_id(SimpleNamespace(from_user=SimpleNamespace(id="7"))) == 7
    assert sc._message_user_id(SimpleNamespace(from_user=None)) is None


@pytest.mark.asyncio
async def test_safe_edit_ignores_only_not_modified() -> None:
    class NotModified(FakeMessage):
        async def edit_text(self, *_args: Any, **_kwargs: Any) -> None:
            raise TelegramBadRequest(method=None, message="message is not modified")

    await sc.safe_edit(NotModified(), "same")

    class OtherError(FakeMessage):
        async def edit_text(self, *_args: Any, **_kwargs: Any) -> None:
            raise TelegramBadRequest(method=None, message="chat not found")

    with pytest.raises(TelegramBadRequest):
        await sc.safe_edit(OtherError(), "x")


@pytest.mark.asyncio
async def test_menu_platform_and_delivery_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    message = FakeMessage()
    install_callback(monkeypatch, message)
    events: list[tuple[Any, ...]] = []
    monkeypatch.setattr(sc, "kb_settings_menu", lambda: "settings-kb")
    monkeypatch.setattr(sc, "kb_messenger_platforms", lambda snapshot, targets: (snapshot, targets))
    monkeypatch.setattr(sc, "kb_delivery_channel_slots", lambda payload: payload)
    monkeypatch.setattr(sc, "kb_delivery_channel_select", lambda slot, payload: (slot, payload))
    monkeypatch.setattr(sc, "platform_title", lambda value: str(value or "Telegram"))
    monkeypatch.setattr(sc, "get_channel_snapshot", lambda _uid: {"preferred_platform": "max", "identities": [{"platform": "max"}, {"platform": "max"}, {"platform": "vk"}]})
    monkeypatch.setattr(sc, "build_messenger_targets", lambda _uid: {"max": "url"})
    monkeypatch.setattr(sc, "set_preferred_platform", lambda *args: events.append(args))
    prefs = SimpleNamespace(morning_channel="max", evening_channel=None)
    monkeypatch.setattr(sc, "get_delivery_preferences", lambda _uid: prefs)
    monkeypatch.setattr(sc, "describe_delivery_preferences", lambda _uid: "prefs")
    monkeypatch.setattr(sc, "set_slot_channel", lambda *args: events.append(args))
    monkeypatch.setattr(sc, "build_delivery_policy_decision", lambda *_args: SimpleNamespace(fallback_used=True, resolved_channel="vk"))
    monkeypatch.setattr(sc, "log_event", lambda *args: events.append(args))

    await sc.settings_menu(FakeCallback("settings:menu"))
    await sc.settings_platform_menu(FakeCallback("settings:platform:menu"))
    await sc.settings_platform_set(FakeCallback("settings:platform:set:vk"))
    await sc.settings_delivery_menu(FakeCallback("settings:delivery:menu"))
    await sc.settings_delivery_channels(FakeCallback("settings:delivery:channels"))
    await sc.settings_delivery_slot_menu(FakeCallback("settings:delivery:slot:morning"))
    await sc.settings_delivery_slot_set(FakeCallback("settings:delivery:slot:set:morning:auto"))

    assert any("Мои настройки" in text for text, _ in message.edits)
    assert any("Предпочтительный мессенджер" in text for text, _ in message.edits)
    assert any("fallback" in text for text, _ in message.edits)
    assert (42, "vk") in events
    assert (42, "morning", None) in events


@pytest.mark.asyncio
async def test_time_prompts_access_and_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    message = FakeMessage()
    install_callback(monkeypatch, message)
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(sc, "kb_settings_locked", lambda: "locked")
    monkeypatch.setattr(sc, "kb_back_main", lambda: "back")
    monkeypatch.setattr(sc, "set_pending", lambda *args, **kwargs: calls.append((*args, kwargs)))
    monkeypatch.setattr(sc, "log_event", lambda *args: calls.append(args))

    monkeypatch.setattr(sc, "has_access", lambda *_args: False)
    await sc.settings_time_work(FakeCallback("settings:time:work"))
    assert "Полный доступ" in message.answers[-1][0]

    monkeypatch.setattr(sc, "has_access", lambda *_args: True)
    await sc.settings_time_work(FakeCallback("settings:time:work"))
    await sc.settings_time_home(FakeCallback("settings:time:home"))
    await sc.settings_delivery_tz(FakeCallback("settings:delivery:tz"))
    await sc.settings_delivery_quiet(FakeCallback("settings:delivery:quiet"))

    assert any(call[:3] == (42, "set_time", {"slot": "work"}) for call in calls if len(call) >= 3)
    assert any("Europe/Amsterdam" in text for text, _ in message.answers)
    assert any("quiet hours" in text for text, _ in message.answers)


class Pending:
    def __init__(self, kind: str, data: dict[str, Any] | None = None) -> None:
        self.kind = kind
        self.data = data or {}


@pytest.mark.asyncio
async def test_settings_input_timezone_quiet_and_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sc, "_to_thread", direct_to_thread)
    monkeypatch.setattr(sc, "kb_back_main", lambda: "back")
    monkeypatch.setattr(sc, "kb_main", lambda **kwargs: kwargs)
    monkeypatch.setattr(sc, "log_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sc, "describe_delivery_preferences", lambda _uid: "prefs")
    monkeypatch.setattr(sc, "_prompt_after_time_set", lambda *_args: _async_none())

    pending: list[Pending | None] = [Pending("set_timezone")]
    monkeypatch.setattr(sc, "peek_pending", lambda _uid: pending[0])
    monkeypatch.setattr(sc, "consume_pending", lambda _uid, _kind=None: pending.pop(0))
    monkeypatch.setattr(sc, "set_user_timezone", lambda _uid, value: value if value == "Europe/Amsterdam" else (_ for _ in ()).throw(ValueError("bad")))
    msg = FakeMessage(text="Europe/Amsterdam")
    await sc.settings_time_input(msg)
    assert "Часовой пояс сохранён" in msg.answers[-1][0]

    pending[:] = [Pending("set_quiet_hours")]
    msg = FakeMessage(text="off")
    monkeypatch.setattr(sc, "clear_quiet_hours", lambda _uid: None)
    await sc.settings_time_input(msg)
    assert "выключены" in msg.answers[-1][0]

    pending[:] = [Pending("set_quiet_hours")]
    msg = FakeMessage(text="22:00-08:00")
    monkeypatch.setattr(sc, "set_quiet_hours", lambda _uid, start, end: (start, end))
    await sc.settings_time_input(msg)
    assert "22:00-08:00" in msg.answers[-1][0]

    pending[:] = [Pending("set_time", {"slot": "work"})]
    msg = FakeMessage(text="08:30")
    monkeypatch.setattr(sc, "_persist_user_time", lambda *_args: None)
    monkeypatch.setattr(sc, "_load_user_times", lambda _uid: {"work_time": "08:30", "home_time": "19:00"})
    await sc.settings_time_input(msg)
    assert "Сохранил время" in msg.answers[-1][0]


@pytest.mark.asyncio
async def test_settings_input_skip_and_invalid_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sc, "_to_thread", direct_to_thread)
    monkeypatch.setattr(sc, "kb_back_main", lambda: "back")
    monkeypatch.setattr(sc, "_message_user_id", lambda _message: None)
    with pytest.raises(SkipHandler):
        await sc.settings_time_input(FakeMessage(text="08:00"))

    monkeypatch.setattr(sc, "_message_user_id", lambda _message: 42)
    monkeypatch.setattr(sc, "peek_pending", lambda _uid: None)
    with pytest.raises(SkipHandler):
        await sc.settings_time_input(FakeMessage(text="08:00"))

    monkeypatch.setattr(sc, "peek_pending", lambda _uid: Pending("set_time", {"slot": "bad"}))
    monkeypatch.setattr(sc, "consume_pending", lambda _uid, _kind=None: Pending("set_time", {"slot": "bad"}))
    msg = FakeMessage(text="08:00")
    await sc.settings_time_input(msg)
    assert "какое время" in msg.answers[-1][0]


def test_persist_and_load_user_times(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class Conn:
        def execute(self, sql: str, params: tuple[Any, ...]):
            calls.append((sql, params))
            return self

        def fetchone(self):
            return ("08:00", "19:00")

    @contextmanager
    def fake_db() -> Iterator[Conn]:
        yield Conn()

    monkeypatch.setattr(sc, "db", fake_db)
    sc._persist_user_time(42, "work", "08:00")
    sc._persist_user_time(42, "home", "19:00")
    assert sc._load_user_times(42) == ("08:00", "19:00")
    with pytest.raises(ValueError):
        sc._persist_user_time(42, "other", "10:00")
    assert any("work_time" in sql for sql, _ in calls)
    assert any("home_time" in sql for sql, _ in calls)
