from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from handlers.flow import settings_core as sc


class Message:
    def __init__(self, text: str | None = None, uid: int | None = 42) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=uid) if uid is not None else None
        self.answers: list[tuple[str, Any]] = []
        self.edits: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None, **_kwargs: Any) -> None:
        self.answers.append((text, reply_markup))


class Callback:
    def __init__(self, data: str | None) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=42)
        self.message = object()


async def direct(func, *args, **kwargs):
    return func(*args, **kwargs)


async def none_async(*_args: Any, **_kwargs: Any) -> None:
    return None


class Pending:
    def __init__(self, kind: str, data: dict[str, Any] | None = None) -> None:
        self.kind = kind
        self.data = data or {}


@pytest.mark.asyncio
async def test_callback_missing_message_and_data_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sc, "safe_answer_callback", none_async)
    monkeypatch.setattr(sc, "_callback_message", lambda _cb: None)
    for handler, data in (
        (sc.settings_menu, "settings:menu"),
        (sc.settings_platform_menu, "settings:platform:menu"),
        (sc.settings_platform_set, "settings:platform:set:vk"),
        (sc.settings_time_work, "settings:time:work"),
        (sc.settings_time_home, "settings:time:home"),
        (sc.settings_delivery_tz, "settings:delivery:tz"),
        (sc.settings_delivery_quiet, "settings:delivery:quiet"),
        (sc.settings_delivery_menu, "settings:delivery:menu"),
        (sc.settings_delivery_channels, "settings:delivery:channels"),
        (sc.settings_delivery_slot_menu, "settings:delivery:slot:morning"),
        (sc.settings_delivery_slot_set, "settings:delivery:slot:set:morning:auto"),
        (sc.settings_ref, "settings:ref"),
    ):
        await handler(Callback(data))

    message = Message()
    monkeypatch.setattr(sc, "_callback_message", lambda _cb: message)
    monkeypatch.setattr(sc, "_to_thread", direct)
    await sc.settings_platform_set(Callback(None))
    await sc.settings_delivery_slot_menu(Callback(None))
    await sc.settings_delivery_slot_set(Callback(None))
    await sc.settings_delivery_slot_set(Callback("settings:delivery:slot:set"))
    assert message.answers == []


@pytest.mark.asyncio
async def test_invalid_timezone_quiet_and_time_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sc, "_to_thread", direct)
    monkeypatch.setattr(sc, "kb_back_main", lambda: "back")
    monkeypatch.setattr(sc, "_message_user_id", lambda _message: 42)

    pending: list[Pending] = [Pending("set_timezone")]
    consumed: list[tuple[int, str | None]] = []
    monkeypatch.setattr(sc, "peek_pending", lambda _uid: pending[0])
    monkeypatch.setattr(sc, "consume_pending", lambda uid, kind=None: consumed.append((uid, kind)))
    monkeypatch.setattr(sc, "set_user_timezone", lambda *_args: (_ for _ in ()).throw(ValueError("bad")))
    msg = Message("Mars/Olympus")
    await sc.settings_time_input(msg)
    assert "корректный timezone" in msg.answers[-1][0]

    pending[:] = [Pending("set_quiet_hours")]
    msg = Message("bad")
    await sc.settings_time_input(msg)
    assert "формат HH:MM-HH:MM" in msg.answers[-1][0]

    pending[:] = [Pending("set_quiet_hours")]
    monkeypatch.setattr(sc, "set_quiet_hours", lambda *_args: (_ for _ in ()).throw(KeyError("bad")))
    msg = Message("25:00-08:00")
    await sc.settings_time_input(msg)
    assert "Не смог распознать" in msg.answers[-1][0]

    pending[:] = [Pending("set_time", {"slot": "home"})]
    msg = Message("not-time")
    await sc.settings_time_input(msg)
    assert "формате HH:MM" in msg.answers[-1][0]
    assert consumed == []

    monkeypatch.setattr(sc, "peek_pending", lambda _uid: Pending("other"))
    with pytest.raises(SkipHandler):
        await sc.settings_time_input(Message("x"))


@pytest.mark.asyncio
async def test_settings_ref_renders_bonus_state(monkeypatch: pytest.MonkeyPatch) -> None:
    message = Message()
    monkeypatch.setattr(sc, "safe_answer_callback", none_async)
    monkeypatch.setattr(sc, "_callback_message", lambda _cb: message)
    monkeypatch.setattr(sc, "_to_thread", direct)
    monkeypatch.setattr(sc, "paid_referrals_count", lambda _uid: 3)
    monkeypatch.setattr(sc, "gift_grants_count", lambda _uid: 2)
    monkeypatch.setattr(sc, "gift_days_granted", lambda _uid: 14)
    monkeypatch.setattr(sc, "compute_bonus_stats", lambda _uid: SimpleNamespace(earned_days=20, used_days=5, remaining_days=15))
    monkeypatch.setattr(sc, "kb_ref_bonus_actions", lambda: "bonus-kb")
    monkeypatch.setattr(sc, "log_event", lambda *_args: None)

    async def edit(_message: Any, text: str, reply_markup: Any = None, **_kwargs: Any) -> None:
        message.edits.append((text, reply_markup))

    monkeypatch.setattr(sc, "safe_edit", edit)
    await sc.settings_ref(Callback("settings:ref"))
    assert "20 дн." in message.edits[-1][0]
    assert "остаток: 15" in message.edits[-1][0]


def module(name: str, **attrs: Any) -> ModuleType:
    value = ModuleType(name)
    for key, item in attrs.items():
        setattr(value, key, item)
    return value


@pytest.mark.asyncio
async def test_prompt_after_time_set_success_and_early_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sc, "_to_thread", direct)
    monkeypatch.setattr(sc, "today_tz", lambda: SimpleNamespace(isoformat=lambda: "2026-07-21"))
    message = Message()

    subscription = module("services.subscription", has_access=lambda *_args: False)
    monkeypatch.setitem(sys.modules, "services.subscription", subscription)
    await sc._prompt_after_time_set(message, "work")
    assert message.answers == []

    subscription.has_access = lambda *_args: True
    progress = module("services.progress", get_index=lambda *_args: 4)
    anchor = module("services.audio_anchor", pick_for_slot=lambda *_args: None)
    monkeypatch.setitem(sys.modules, "services.progress", progress)
    monkeypatch.setitem(sys.modules, "services.audio_anchor", anchor)
    monkeypatch.setitem(sys.modules, "services.mood", module("services.mood", create_session=lambda **_kwargs: 9))
    monkeypatch.setitem(sys.modules, "services.idempotency_keys", module("services.idempotency_keys", for_settings_prompt=lambda *_args: "key"))
    monkeypatch.setitem(sys.modules, "keyboards.inline", module("keyboards.inline", kb_mood_scale=lambda sid, stage: (sid, stage)))
    monkeypatch.setitem(sys.modules, "services.events", module("services.events", log_event=lambda *_args: None))

    import services.db as db_module
    monkeypatch.setattr(db_module, "mark_delivery_once", lambda *_args: True)
    await sc._prompt_after_time_set(message, "home")
    assert message.answers == []

    anchor.pick_for_slot = lambda *_args: SimpleNamespace(anchor="clientplatform")
    monkeypatch.setattr(db_module, "mark_delivery_once", lambda *_args: False)
    await sc._prompt_after_time_set(message, "work")
    assert message.answers == []

    monkeypatch.setattr(db_module, "mark_delivery_once", lambda *_args: True)
    await sc._prompt_after_time_set(message, "work")
    assert "Перед прослушиванием" in message.answers[-1][0]
    assert message.answers[-1][1] == (9, "pre")


@pytest.mark.asyncio
async def test_prompt_after_time_set_missing_user_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sc, "_to_thread", direct)
    await sc._prompt_after_time_set(Message(uid=None), "work")

    monkeypatch.setattr(sc, "_message_user_id", lambda _message: (_ for _ in ()).throw(ValueError("broken")))
    await sc._prompt_after_time_set(Message(), "work")
