from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.bookings import (
    BookingSlot,
    BookingSlotStatus,
    BookingSlotView,
    CustomerBusinessLink,
)

control = importlib.import_module("handlers.clientplatform_control")
simple = importlib.import_module("handlers.clientplatform_simple_experience")
owner = importlib.import_module("handlers.clientplatform_owner_journey")
goal_dashboard = importlib.import_module("handlers.clientplatform_goal_dashboard")
public = importlib.import_module("handlers.clientplatform_public_storefront")


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id
        self.username = "visitor"
        self.full_name = "Посетитель"


class FakeBot:
    async def get_me(self) -> Any:
        return SimpleNamespace(username="clientplatform_bot")


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.from_user = FakeUser()
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


class FakeState:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = dict(data or {})
        self.states: list[Any] = []
        self.clear_count = 0

    async def clear(self) -> None:
        self.clear_count += 1
        self.data.clear()

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def set_state(self, value: Any) -> None:
        self.states.append(value)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)


async def direct_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def direct_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(public.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(control, "Message", FakeMessage)


def _slot(
    *,
    business_id: str | None = None,
    offering_id: str | None = None,
    slot_id: str | None = None,
    status: BookingSlotStatus = BookingSlotStatus.OPEN,
) -> BookingSlotView:
    starts = datetime.now(timezone.utc) + timedelta(days=5)
    ends = starts + timedelta(minutes=60)
    customer_id = str(uuid4()) if status == BookingSlotStatus.BOOKED else None
    slot = BookingSlot(
        id=slot_id or str(uuid4()),
        business_id=business_id or str(uuid4()),
        offering_id=offering_id or str(uuid4()),
        starts_at=starts.isoformat(timespec="seconds"),
        ends_at=ends.isoformat(timespec="seconds"),
        duration_minutes=60,
        status=status,
        booked_customer_id=customer_id,
        created_by_member_id=str(uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return BookingSlotView(
        slot=slot,
        offering_title="Замена раковины",
        business_name="Сантехник",
        timezone="Europe/Amsterdam",
    )


@pytest.mark.asyncio
async def test_publishing_time_ends_with_actionable_receipt_not_generic_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    offering_id = str(uuid4())
    published = _slot(business_id=business_id, offering_id=offering_id)
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=object()))
    monkeypatch.setattr(control, "create_booking_slot", lambda **_kwargs: published)
    message = FakeMessage("60")
    state = FakeState(
        {
            "business_id": business_id,
            "offering_id": offering_id,
            "booking_start": "10.08.2026 12:00",
        }
    )

    await owner.receive_owner_booking_duration(message, state)

    text, kwargs = message.answers[-1]
    assert "Готово! Время опубликовано" in text
    labels = [
        button.text
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "👀 Посмотреть глазами клиента" in labels
    assert "📅 Открыть мой календарь" in labels
    assert "📨 Просто отправить" in labels
    assert "🚀 Получить клиентов" in labels
    assert "✏️ Изменить" in labels
    assert "➕ Ещё время" in labels
    assert all(
        len(button.callback_data or "") <= 64
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    )
    assert state.clear_count == 1


@pytest.mark.asyncio
async def test_owner_dashboard_keeps_status_and_separates_acquisition_from_sales(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    actor = object()
    capability = SimpleNamespace(
        id=str(uuid4()),
        connector_key="services",
        status=CapabilityStatus.ACTIVE,
    )
    snapshot = (
        actor,
        SimpleNamespace(business=SimpleNamespace(name="Сантехник")),
        SimpleNamespace(activity_description="Ремонтирую сантехнику"),
        [capability],
        [object()],
        [],
        [_slot(business_id=business_id)],
    )
    monkeypatch.setattr(simple, "_business_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(goal_dashboard, "_owner_next_action", lambda _actor: None)
    message = FakeMessage()

    await owner.send_owner_dashboard(message, user_id=101, business_id=business_id)

    text, kwargs = message.answers[-1]
    assert "Ремонтирую сантехнику" in text
    assert "Главное сейчас" in text
    assert "свободных времён: 1" in text
    markup = kwargs["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["🚀 Найти новых клиентов", "⋯ Все возможности"]
    assert str(markup.inline_keyboard[0][0].callback_data).startswith("cpo:start:")
    assert str(markup.inline_keyboard[1][0].callback_data).startswith("cpo:more:")


@pytest.mark.asyncio
async def test_fixed_width_public_payload_handles_underscore_inside_uuid_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(UUID(bytes=b"\xff" * 16))
    slot_id = str(UUID(bytes=b"\xfb" * 16))
    published = _slot(business_id=business_id, slot_id=slot_id)
    payload = owner._public_slot_payload(published)
    assert "_" in control._uuid_token(business_id)
    message = FakeMessage(f"/start {payload}")
    state = FakeState()
    original = AsyncMock()
    monkeypatch.setattr(public, "is_public_storefront_staff", lambda **_kwargs: False)
    monkeypatch.setattr(
        public,
        "connect_public_storefront_customer",
        lambda **_kwargs: CustomerBusinessLink(
            business_id=business_id,
            business_name="Сантехник",
            customer_id=str(uuid4()),
        ),
    )
    monkeypatch.setattr(
        control,
        "list_customer_booking_slots",
        lambda **_kwargs: [published],
    )

    await public.dispatch_public_start(
        original,
        message,
        state,
        user_id=101,
        managed_bot_business_id=None,
    )

    original.assert_not_awaited()
    text, kwargs = message.answers[-1]
    assert "Замена раковины" in text
    assert "Можно записаться" in text
    assert kwargs["reply_markup"].inline_keyboard[0][0].text == "✅ Записаться"


@pytest.mark.asyncio
async def test_staff_public_slot_link_uses_preview_without_customer_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    slot_id = str(uuid4())
    payload = owner._public_slot_payload(
        _slot(business_id=business_id, slot_id=slot_id)
    )
    message = FakeMessage(f"/start {payload}")
    state = FakeState({"stale": "wizard"})
    original = AsyncMock()
    customer_connect = AsyncMock()
    monkeypatch.setattr(public, "is_public_storefront_staff", lambda **_kwargs: True)
    monkeypatch.setattr(public, "connect_public_storefront_customer", customer_connect)

    await public.dispatch_public_start(
        original,
        message,
        state,
        user_id=101,
        managed_bot_business_id=None,
    )

    original.assert_not_awaited()
    customer_connect.assert_not_awaited()
    assert state.clear_count == 1
    text, kwargs = message.answers[-1]
    assert "не создаёт для Вас клиентскую карточку" in text
    buttons = [
        button
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert buttons[0].text == "👀 Посмотреть глазами клиента"
    assert buttons[0].callback_data == (
        f"cpj:preview:{control._uuid_token(business_id)}:{control._uuid_token(slot_id)}"
    )
    assert buttons[-1].text == "🏠 В мой кабинет"


@pytest.mark.asyncio
async def test_unrelated_start_payload_stays_on_canonical_entry_path() -> None:
    original = AsyncMock()
    message = FakeMessage("/start ordinary")
    state = FakeState()

    await public.dispatch_public_start(
        original,
        message,
        state,
        user_id=101,
        managed_bot_business_id=None,
    )

    original.assert_awaited_once()
