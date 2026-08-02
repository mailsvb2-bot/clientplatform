from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, User

from clientplatform.domain.activity import BusinessProfileStatus, CapabilityStatus
from clientplatform.domain.bookings import BookingSlotStatus
from handlers import clientplatform_admin as admin
from handlers import clientplatform_interaction_safety as safety


def telegram_message(*, user_id: int = 77, text: str = "текст") -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        text=text,
    )


def telegram_callback(*, data: str, user_id: int = 77) -> CallbackQuery:
    message = telegram_message(user_id=user_id)
    assert message.from_user is not None
    return CallbackQuery(
        id=f"callback-{data}",
        from_user=message.from_user,
        chat_instance="instance",
        message=message,
        data=data,
    )


def fsm_context(*, user_id: int = 77) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def test_admin_keyboard_exposes_owner_controls() -> None:
    admin.control._uuid_token = lambda _value: "business-token"
    markup = admin._admin_keyboard("business-id")
    labels = [button.text for row in markup.inline_keyboard for button in row]
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]

    assert labels == [
        "Клиенты",
        "Результаты",
        "Форматы работы",
        "Мой Telegram-бот",
        "Изменить название",
        "Обновить",
        "Вернуться в кабинет",
    ]
    assert "cpa:home:business-token" in callbacks
    assert "cpa:formats:business-token" in callbacks
    assert "cpa:back:business-token" in callbacks


@pytest.mark.asyncio
async def test_admin_panel_renders_operational_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[tuple[str, InlineKeyboardMarkup | None]] = []
    message = telegram_message()

    async def answer_message(
        _message: Message,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        **_kwargs: Any,
    ) -> None:
        answers.append((text, reply_markup))

    async def snapshot(**_kwargs: Any):
        return (
            SimpleNamespace(business=SimpleNamespace(name="Сантехник")),
            SimpleNamespace(
                customers=4,
                programs=2,
                dispatch_pending=1,
                dispatch_sent=9,
                dispatch_attention=1,
            ),
            [
                SimpleNamespace(status=CapabilityStatus.ACTIVE),
                SimpleNamespace(status=CapabilityStatus.DISABLED),
            ],
            [
                SimpleNamespace(slot=SimpleNamespace(status=BookingSlotStatus.OPEN)),
                SimpleNamespace(slot=SimpleNamespace(status=BookingSlotStatus.BOOKED)),
            ],
        )

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(admin, "_admin_snapshot", snapshot)
    monkeypatch.setattr(admin.control, "_uuid_token", lambda _value: "token")

    await admin.send_admin_panel(
        message,
        user_id=77,
        business_id="business-id",
    )

    text, markup = answers[-1]
    assert "Админка · Сантехник" in text
    assert "Клиенты: 4" in text
    assert "Активные программы: 2" in text
    assert "Подключено форматов: 1" in text
    assert "Свободных времён: 1" in text
    assert "Требуют внимания: 1" in text
    assert markup is not None


@pytest.mark.asyncio
async def test_callback_is_acknowledged_before_slow_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: list[str] = []
    release = asyncio.Event()

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        **_kwargs: Any,
    ) -> None:
        timeline.append(f"ack:{text or ''}")

    async def edit_reply_markup(
        _message: Message,
        **_kwargs: Any,
    ) -> None:
        timeline.append("keyboard-removed")

    async def slow_handler(_event: Any, _data: dict[str, Any]) -> str:
        timeline.append("handler-start")
        await release.wait()
        timeline.append("handler-finish")
        return "done"

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    monkeypatch.setattr(Message, "edit_reply_markup", edit_reply_markup)

    middleware = safety.ClientPlatformInteractionSafetyMiddleware()
    callback = telegram_callback(data="cpa:home:business")
    task = asyncio.create_task(
        middleware(
            slow_handler,
            callback,
            {"bot": SimpleNamespace(id=1), "state": fsm_context()},
        )
    )
    await asyncio.sleep(0)

    assert timeline[:2] == ["ack:", "handler-start"]
    assert task.done() is False

    release.set()
    assert await task == "done"
    assert timeline[-2:] == ["handler-finish", "keyboard-removed"]


@pytest.mark.asyncio
async def test_production_dashboard_uses_one_parallel_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("production_like_control")
    calls = {
        "actor": 0,
        "profile": 0,
        "capabilities": 0,
        "accesses": 0,
        "legacy_dashboard": 0,
    }
    answers: list[str] = []

    async def actor(_user_id: int, _business_id: str) -> object:
        calls["actor"] += 1
        return object()

    def profile(*, actor: object) -> object:
        calls["profile"] += 1
        return SimpleNamespace(
            status=BusinessProfileStatus.READY,
            activity_description="Ремонтирую сантехнику",
        )

    def capabilities(*, actor: object) -> list[object]:
        calls["capabilities"] += 1
        return [
            SimpleNamespace(
                status=CapabilityStatus.ACTIVE,
                title="Консультации",
            )
        ]

    async def legacy_dashboard(
        _message: Message,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        calls["legacy_dashboard"] += 1

    async def legacy_resume(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("optimized production path must not call legacy resume")

    async def setup(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("ready profile must not open setup")

    async def answer_message(
        _message: Message,
        text: str,
        **_kwargs: Any,
    ) -> None:
        answers.append(text)

    def accesses(*, user_id: int) -> list[object]:
        calls["accesses"] += 1
        return [
            SimpleNamespace(
                business=SimpleNamespace(id="business-id", name="Сантехник")
            )
        ]

    module.ActivityNotFound = type("ActivityNotFound", (Exception,), {})
    module.ClientPlatformControlState = SimpleNamespace(activity_description=object())
    module._dashboard_keyboard = lambda *_args: InlineKeyboardMarkup(inline_keyboard=[])
    module._send_dashboard = legacy_dashboard
    module._resume_business = legacy_resume
    module._uuid_token = lambda value: f"token-{value}"
    module._actor = actor
    module._send_capability_setup = setup
    module.get_business_profile = profile
    module.list_business_capabilities = capabilities

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(safety, "list_accessible_businesses", accesses)

    safety.install_interaction_safety(Router(name="optimized-root"), module)
    await module._send_dashboard(
        telegram_message(),
        user_id=77,
        business_id="business-id",
    )

    assert calls == {
        "actor": 1,
        "profile": 1,
        "capabilities": 1,
        "accesses": 1,
        "legacy_dashboard": 0,
    }
    assert module._optimized_dashboard_queries_installed is True
    assert "Сантехник" in answers[-1]
    assert "Ремонтирую сантехнику" in answers[-1]


def test_admin_dashboard_install_is_idempotent() -> None:
    module = ModuleType("admin_control")
    module._uuid_token = lambda value: f"token-{value}"
    module._dashboard_keyboard = lambda *_args: InlineKeyboardMarkup(inline_keyboard=[])

    admin.install_admin_dashboard_button(module)
    first = module._dashboard_keyboard
    admin.install_admin_dashboard_button(module)

    assert module._dashboard_keyboard is first
    markup = module._dashboard_keyboard("business", [])
    assert markup.inline_keyboard[-1][0].text == "Админка"
    assert markup.inline_keyboard[-1][0].callback_data == "cpa:home:token-business"
