from __future__ import annotations

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
from handlers import clientplatform_dashboard_dispatch as dashboard_dispatch
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


def business_access(business_id: str, name: str) -> object:
    return SimpleNamespace(
        business=SimpleNamespace(id=business_id, name=name),
    )


def test_admin_keyboard_exposes_owner_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin.control, "_uuid_token", lambda _value: "business-token")
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
async def test_admin_snapshot_loads_all_operational_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = object()
    access = business_access("business-id", "Сантехник")
    summary = SimpleNamespace(customers=3)
    capabilities = [SimpleNamespace(status=CapabilityStatus.ACTIVE)]
    slots = [SimpleNamespace(slot=SimpleNamespace(status=BookingSlotStatus.OPEN))]
    calls: list[tuple[str, object]] = []

    async def resolve_actor(user_id: int, business_id: str) -> object:
        calls.append(("actor", (user_id, business_id)))
        return actor

    def load_summary(*, actor: object) -> object:
        calls.append(("summary", actor))
        return summary

    def load_capabilities(*, actor: object) -> list[object]:
        calls.append(("capabilities", actor))
        return capabilities

    def load_slots(*, actor: object) -> list[object]:
        calls.append(("slots", actor))
        return slots

    def load_accesses(*, user_id: int) -> list[object]:
        calls.append(("accesses", user_id))
        return [access]

    monkeypatch.setattr(admin.control, "_actor", resolve_actor)
    monkeypatch.setattr(admin, "business_delivery_summary", load_summary)
    monkeypatch.setattr(admin.control, "list_business_capabilities", load_capabilities)
    monkeypatch.setattr(admin, "list_booking_slots", load_slots)
    monkeypatch.setattr(admin, "list_accessible_businesses", load_accesses)

    result = await admin._admin_snapshot(user_id=77, business_id="business-id")

    assert result == (access, summary, capabilities, slots)
    assert ("actor", (77, "business-id")) in calls
    assert ("summary", actor) in calls
    assert ("capabilities", actor) in calls
    assert ("slots", actor) in calls
    assert ("accesses", 77) in calls


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
            business_access("business-id", "Сантехник"),
            SimpleNamespace(
                customers=4,
                programs=2,
                dispatch_pending=1,
                dispatch_sent=9,
                dispatch_attention=1,
            ),
            [
                SimpleNamespace(status=CapabilityStatus.ACTIVE),
                SimpleNamespace(status=object()),
            ],
            [
                SimpleNamespace(slot=SimpleNamespace(status=BookingSlotStatus.OPEN)),
                SimpleNamespace(slot=SimpleNamespace(status=object())),
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
async def test_admin_command_without_business_starts_from_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[str] = []

    async def answer_message(
        _message: Message,
        text: str,
        **_kwargs: Any,
    ) -> None:
        answers.append(text)

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(admin.control, "_user_id", lambda _message: 77)
    monkeypatch.setattr(admin, "list_accessible_businesses", lambda **_kwargs: [])

    state = fsm_context()
    await state.set_state("unfinished")
    await admin.open_admin_command(telegram_message(), state)

    assert await state.get_state() is None
    assert answers == ["Сначала создайте бизнес через /start."]


@pytest.mark.asyncio
async def test_admin_command_opens_single_business(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[int, str]] = []

    async def send_panel(
        _message: Message,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        opened.append((user_id, business_id))

    monkeypatch.setattr(admin.control, "_user_id", lambda _message: 77)
    monkeypatch.setattr(
        admin,
        "list_accessible_businesses",
        lambda **_kwargs: [business_access("business-id", "Сантехник")],
    )
    monkeypatch.setattr(admin, "send_admin_panel", send_panel)

    await admin.open_admin_command(telegram_message(), fsm_context())

    assert opened == [(77, "business-id")]


@pytest.mark.asyncio
async def test_admin_command_asks_for_business_when_multiple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[tuple[str, InlineKeyboardMarkup | None]] = []

    async def answer_message(
        _message: Message,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        **_kwargs: Any,
    ) -> None:
        answers.append((text, reply_markup))

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(admin.control, "_user_id", lambda _message: 77)
    monkeypatch.setattr(admin.control, "_uuid_token", lambda value: f"token-{value}")
    monkeypatch.setattr(
        admin,
        "list_accessible_businesses",
        lambda **_kwargs: [
            business_access("one", "Первый"),
            business_access("two", "Второй"),
        ],
    )

    await admin.open_admin_command(telegram_message(), fsm_context())

    text, markup = answers[-1]
    assert text == "Для какого бизнеса открыть админку?"
    assert markup is not None
    assert [row[0].text for row in markup.inline_keyboard] == ["Первый", "Второй"]
    assert [row[0].callback_data for row in markup.inline_keyboard] == [
        "cpa:home:token-one",
        "cpa:home:token-two",
    ]


@pytest.mark.asyncio
async def test_admin_callbacks_route_to_panel_formats_and_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, str]] = []

    async def send_panel(
        _message: Message,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        calls.append(("panel", user_id, business_id))

    async def send_formats(
        _message: Message,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        calls.append(("formats", user_id, business_id))

    async def send_dashboard(
        _message: Message,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        calls.append(("dashboard", user_id, business_id))

    monkeypatch.setattr(admin.control, "_token_uuid", lambda _value: "business-id")
    monkeypatch.setattr(admin, "send_admin_panel", send_panel)
    monkeypatch.setattr(admin.control, "_send_capability_setup", send_formats)
    monkeypatch.setattr(admin.control, "_send_dashboard", send_dashboard)

    panel_state = fsm_context(user_id=77)
    await panel_state.set_state("busy")
    await admin.open_admin_panel(
        telegram_callback(data="cpa:home:token"),
        panel_state,
    )
    assert await panel_state.get_state() is None

    formats_state = fsm_context(user_id=78)
    await formats_state.set_state("busy")
    await admin.open_admin_formats(
        telegram_callback(data="cpa:formats:token", user_id=78),
        formats_state,
    )
    assert await formats_state.get_state() is None

    dashboard_state = fsm_context(user_id=79)
    await dashboard_state.set_state("busy")
    await admin.leave_admin_panel(
        telegram_callback(data="cpa:back:token", user_id=79),
        dashboard_state,
    )
    assert await dashboard_state.get_state() is None

    assert calls == [
        ("panel", 77, "business-id"),
        ("formats", 78, "business-id"),
        ("dashboard", 79, "business-id"),
    ]


@pytest.mark.asyncio
async def test_callback_is_acknowledged_before_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: list[str] = []

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

    async def handler(_event: Any, _data: dict[str, Any]) -> str:
        timeline.append("handler-start")
        return "done"

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    monkeypatch.setattr(Message, "edit_reply_markup", edit_reply_markup)

    middleware = safety.ClientPlatformInteractionSafetyMiddleware()
    result = await middleware(
        handler,
        telegram_callback(data="cpa:home:business"),
        {"bot": SimpleNamespace(id=1), "state": fsm_context()},
    )

    assert result == "done"
    assert timeline == ["ack:", "handler-start", "keyboard-removed"]


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
        return [business_access("business-id", "Сантехник")]

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


@pytest.mark.asyncio
async def test_dynamic_dashboard_dispatch_uses_fast_and_override_paths() -> None:
    module = ModuleType("dashboard_dispatch_control")
    calls: list[tuple[str, int, str]] = []

    async def optimized_resume(
        _message: Message,
        *,
        user_id: int,
        business_id: str,
        state: FSMContext,
    ) -> None:
        calls.append(("optimized", user_id, business_id))

    async def optimized_dashboard(
        _message: Message,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        calls.append(("optimized-dashboard", user_id, business_id))

    async def replacement_dashboard(
        _message: Message,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        calls.append(("replacement", user_id, business_id))

    module._resume_business = optimized_resume
    module._send_dashboard = optimized_dashboard

    dashboard_dispatch.install_dynamic_dashboard_dispatch(module)
    installed = module._resume_business
    dashboard_dispatch.install_dynamic_dashboard_dispatch(module)
    assert module._resume_business is installed

    state = fsm_context()
    await state.set_state("busy")
    await module._resume_business(
        telegram_message(),
        user_id=77,
        business_id="business-id",
        state=state,
    )
    assert await state.get_state() == "busy"

    module._send_dashboard = replacement_dashboard
    await module._resume_business(
        telegram_message(),
        user_id=77,
        business_id="business-id",
        state=state,
    )
    assert await state.get_state() is None
    assert calls == [
        ("optimized", 77, "business-id"),
        ("replacement", 77, "business-id"),
    ]


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
