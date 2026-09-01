from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.bookings import BookingSlotStatus

simple = importlib.import_module("handlers.clientplatform_simple_experience")
control = importlib.import_module("handlers.clientplatform_control")
builder = importlib.import_module("handlers.clientplatform_program_builder")


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeBot:
    async def get_me(self) -> Any:
        return SimpleNamespace(username="clientplatform_bot")


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, data: str, user_id: int = 101) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.bot = FakeBot()
        self.answers: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))

    def model_copy(self, *, update: dict[str, Any]) -> "FakeCallback":
        copied = FakeCallback(str(update["data"]), self.from_user.id)
        copied.message = self.message
        copied.bot = self.bot
        return copied


class FakeState:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = dict(data or {})
        self.states: list[Any] = []
        self.clear_count = 0

    async def clear(self) -> None:
        self.clear_count += 1
        self.data.clear()

    async def set_state(self, value: Any) -> None:
        self.states.append(value)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)


async def direct_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def direct_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(simple.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(control, "Message", FakeMessage)


def _snapshot(
    business_id: str,
    *,
    customers: list[Any] | None = None,
    programs: list[Any] | None = None,
    slots: list[Any] | None = None,
) -> tuple[Any, ...]:
    actor = SimpleNamespace(business_id=business_id)
    access = SimpleNamespace(business=SimpleNamespace(id=business_id, name="Практика"))
    profile = SimpleNamespace(activity_description="Помогаю клиентам")
    capability = SimpleNamespace(
        id=str(uuid4()),
        connector_key="consultations",
        status=CapabilityStatus.ACTIVE,
        title="Консультации",
    )
    return (
        actor,
        access,
        profile,
        [capability],
        list(customers or []),
        list(programs or []),
        list(slots or []),
    )


def test_welcome_and_simple_keyboard_are_result_first() -> None:
    text = simple.welcome_text()
    assert "цифровой помощник" in text
    assert "сложных настроек" in text
    button = simple.welcome_keyboard().inline_keyboard[0][0]
    assert button.text == "🚀 Запустить мой бизнес"
    assert button.callback_data == "cps:start"

    business_id = str(uuid4())
    first = simple._simple_keyboard(business_id).inline_keyboard[0][0]
    assert first.text == "✨ Помочь выбрать первый шаг"
    assert str(first.callback_data).startswith("cps:firstgoal:")


def test_telegram_share_url_round_trips_link_and_text() -> None:
    link = "https://t.me/clientplatform_bot?start=cpj_invite-token"
    text = "Подключитесь ко мне"
    share = simple._telegram_share_url(link, text)
    parsed = urlsplit(share)
    assert parsed.scheme == "https"
    assert parsed.netloc == "t.me"
    assert parsed.path == "/share/url"
    params = parse_qs(parsed.query)
    assert params["url"] == [link]
    assert params["text"] == [text]


@pytest.mark.asyncio
async def test_start_simple_onboarding_sets_only_first_plain_language_step() -> None:
    callback = FakeCallback("cps:start")
    state = FakeState({"old": "value"})
    await simple.start_simple_onboarding(callback, state)
    assert state.clear_count == 1
    assert state.states[-1] == control.ClientPlatformControlState.business_name
    assert "Как называется" in callback.message.answers[-1][0]


@pytest.mark.asyncio
async def test_next_action_creates_program_first(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    monkeypatch.setattr(simple, "_business_snapshot", AsyncMock(return_value=_snapshot(business_id)))
    callback = FakeCallback(f"cps:next:{control._uuid_token(business_id)}")
    state = FakeState()
    await simple.next_best_action(callback, state)
    assert state.states[-1] == builder.ClientPlatformProgramBuilderState.program_title
    assert state.data["business_id"] == business_id
    assert "создадим первый материал" in callback.message.answers[-1][0]


@pytest.mark.asyncio
async def test_next_action_invites_first_customer_with_one_tap_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    monkeypatch.setattr(
        simple,
        "_business_snapshot",
        AsyncMock(return_value=_snapshot(business_id, programs=[object()])),
    )
    monkeypatch.setattr(
        control,
        "issue_customer_invite",
        lambda **_kwargs: SimpleNamespace(token="invite-token"),
    )
    callback = FakeCallback(f"cps:next:{control._uuid_token(business_id)}")
    await simple.next_best_action(callback, FakeState())

    text, kwargs = callback.message.answers[-1]
    direct_link = "https://t.me/clientplatform_bot?start=cpj_invite-token"
    assert direct_link in text
    assert "Отправить клиенту" in text
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "📨 Отправить клиенту"
    params = parse_qs(urlsplit(button.url).query)
    assert params["url"] == [direct_link]
    assert "ClientPlatform" in params["text"][0]


@pytest.mark.asyncio
async def test_next_action_builds_offer_then_slot_then_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    program = object()
    customer = object()
    capability = SimpleNamespace(
        id=str(uuid4()),
        connector_key="consultations",
        status=CapabilityStatus.ACTIVE,
        title="Консультации",
    )
    base = list(_snapshot(business_id, programs=[program], customers=[customer]))
    base[3] = [capability]
    monkeypatch.setattr(simple, "_business_snapshot", AsyncMock(return_value=tuple(base)))
    monkeypatch.setattr(control, "list_business_offerings", lambda **_kwargs: [])
    first = FakeCallback(f"cps:next:{control._uuid_token(business_id)}")
    first_state = FakeState()
    await simple.next_best_action(first, first_state)
    assert first_state.states[-1] == control.ClientPlatformControlState.offering_title

    offering = SimpleNamespace(id=str(uuid4()), title="Консультация")
    monkeypatch.setattr(control, "list_business_offerings", lambda **_kwargs: [offering])
    second = FakeCallback(f"cps:next:{control._uuid_token(business_id)}")
    second_state = FakeState()
    await simple.next_best_action(second, second_state)
    assert second_state.states[-1] == control.ClientPlatformControlState.booking_start
    assert second_state.data["offering_id"] == offering.id

    slot = SimpleNamespace(slot=SimpleNamespace(status=BookingSlotStatus.OPEN))
    ready = list(base)
    ready[6] = [slot]
    monkeypatch.setattr(simple, "_business_snapshot", AsyncMock(return_value=tuple(ready)))
    third = FakeCallback(f"cps:next:{control._uuid_token(business_id)}")
    await simple.next_best_action(third, FakeState())
    assert "Основной путь уже настроен" in third.message.answers[-1][0]


@pytest.mark.asyncio
async def test_simple_booking_fallback_returns_to_result_first_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    callback = FakeCallback(f"cps:booking:{token}")
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=object()))
    monkeypatch.setattr(control, "list_business_capabilities", lambda **_kwargs: [])

    await simple.open_simple_booking(callback, FakeState())

    text, kwargs = callback.message.answers[-1]
    assert "Помочь выбрать первый шаг" in text
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "✨ Помочь выбрать первый шаг"
    assert button.callback_data == f"cps:firstgoal:{token}"


@pytest.mark.asyncio
async def test_simple_routes_preserve_program_booking_and_advanced_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    open_programs = AsyncMock()
    monkeypatch.setattr(builder, "open_programs", open_programs)
    await simple.open_simple_programs(FakeCallback(f"cps:programs:{token}"), FakeState())
    assert open_programs.await_args.args[0].data == f"cp:cap:{token}:programs"

    actor = object()
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        control,
        "list_business_capabilities",
        lambda **_kwargs: [SimpleNamespace(
            connector_key="consultations",
            status=CapabilityStatus.ACTIVE,
        )],
    )
    opened = AsyncMock()
    monkeypatch.setattr(control, "open_capability", opened)
    await simple.open_simple_booking(FakeCallback(f"cps:booking:{token}"), FakeState())
    assert opened.await_args.args[0].data.endswith(":consultations")

    advanced = AsyncMock()
    monkeypatch.setattr(simple, "send_advanced_dashboard", advanced)
    await simple.open_advanced_dashboard(FakeCallback(f"cps:advanced:{token}"), FakeState())
    advanced.assert_awaited_once()
