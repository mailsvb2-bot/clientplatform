from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from clientplatform.domain.bookings import BookingSlot, BookingSlotStatus, BookingSlotView

control = __import__("handlers.clientplatform_control", fromlist=["clientplatform_control"])
owner = __import__("handlers.clientplatform_owner_journey", fromlist=["clientplatform_owner_journey"])
promotion = __import__("handlers.clientplatform_promotion", fromlist=["clientplatform_promotion"])
safety = __import__(
    "handlers.clientplatform_interaction_safety",
    fromlist=["clientplatform_interaction_safety"],
)


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, data: str, user_id: int = 101) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage()
        self.answers: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))


async def direct_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = func(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


@pytest.fixture(autouse=True)
def direct_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(promotion.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(control.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(control, "Message", FakeMessage)


def make_slot(
    business_id: str,
    index: int,
    status: BookingSlotStatus = BookingSlotStatus.OPEN,
) -> BookingSlotView:
    starts = datetime.now(timezone.utc) + timedelta(days=index + 1)
    slot = BookingSlot(
        id=str(uuid4()),
        business_id=business_id,
        offering_id=str(uuid4()),
        starts_at=starts.isoformat(timespec="seconds"),
        ends_at=(starts + timedelta(hours=1)).isoformat(timespec="seconds"),
        duration_minutes=60,
        status=status,
        booked_customer_id=str(uuid4()) if status == BookingSlotStatus.BOOKED else None,
        created_by_member_id=str(uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return BookingSlotView(
        slot=slot,
        offering_title=f"Услуга {index + 1:02d}",
        business_name="Мастер",
        timezone="Europe/Amsterdam",
    )


def buttons(message: FakeMessage) -> list[Any]:
    return [
        button
        for row in message.answers[-1][1]["reply_markup"].inline_keyboard
        for button in row
    ]


def test_client_booking_pages_keep_navigation_and_stale_keyboard_safety() -> None:
    page_callback = "cp:client:business:1"

    assert safety._is_repeatable_navigation(page_callback) is True
    assert safety._is_state_escape_callback(page_callback) is True
    assert page_callback.startswith(safety._ONE_SHOT_PREFIXES) is True


@pytest.mark.asyncio
async def test_owner_calendar_reaches_thirteenth_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    slots = [make_slot(business_id, index) for index in range(14)]
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=object()))
    monkeypatch.setattr(control, "list_booking_slots", lambda **_kwargs: slots)

    first = FakeCallback(f"cpj:calendar:{token}:3650")
    await owner.open_owner_calendar(first)
    next_data = next(
        button.callback_data
        for button in buttons(first.message)
        if button.text == "Вперёд ➡️"
    )
    assert "Услуга 13" not in first.message.answers[-1][0]

    second = FakeCallback(next_data)
    await owner.open_owner_calendar(second)
    assert "Услуга 13" in second.message.answers[-1][0]
    assert "Услуга 14" in second.message.answers[-1][0]


@pytest.mark.asyncio
async def test_owner_bookings_page_text_matches_clickable_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    slots = [
        make_slot(business_id, index, BookingSlotStatus.BOOKED)
        for index in range(14)
    ]
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=object()))
    monkeypatch.setattr(control, "list_booking_slots", lambda **_kwargs: slots)
    monkeypatch.setattr(control, "list_customers", lambda **_kwargs: [])

    callback = FakeCallback(f"cpj:bookings:{token}:1")
    await owner.open_customer_bookings(callback)
    text = callback.message.answers[-1][0]
    slot_buttons = [
        button
        for button in buttons(callback.message)
        if (button.callback_data or "").startswith("cpj:slot:")
    ]
    assert "Услуга 13" in text and "Услуга 14" in text
    assert len(slot_buttons) == 2


@pytest.mark.asyncio
async def test_promotion_reaches_thirteenth_promotable_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    slots = [make_slot(business_id, index) for index in range(14)]
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=object()))
    monkeypatch.setattr(promotion, "list_promotable_slots", lambda **_kwargs: slots)
    monkeypatch.setattr(
        promotion,
        "promotion_stats",
        lambda **_kwargs: SimpleNamespace(
            campaigns=0,
            people_opened=0,
            bookings=0,
            conversion_percent=0.0,
        ),
    )
    monkeypatch.setattr(promotion, "list_promotion_campaigns", lambda **_kwargs: [])

    callback = FakeCallback(f"cpj:promote:{token}:1")
    await promotion.open_promotion_workspace(callback)
    labels = [
        button.text
        for button in buttons(callback.message)
        if (button.callback_data or "").startswith("cpp:slot:")
    ]
    assert any("Услуга 13" in label for label in labels)
    assert any("Услуга 14" in label for label in labels)


@pytest.mark.asyncio
async def test_customer_booking_reaches_thirteenth_slot_without_new_booking_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    slots = [make_slot(business_id, index) for index in range(14)]
    monkeypatch.setattr(control, "list_customer_booking_slots", lambda **_kwargs: slots)

    callback = FakeCallback(f"cp:client:{token}:1")
    await control.open_client_booking(callback)
    booking_buttons = [
        button
        for button in buttons(callback.message)
        if (button.callback_data or "").startswith("cp:book:")
    ]
    assert len(booking_buttons) == 2
    assert "Услуга 13" in callback.message.answers[-1][0]
    assert all(button.callback_data.startswith("cp:book:") for button in booking_buttons)


@pytest.mark.asyncio
async def test_focused_public_link_keeps_canonical_booking_callback() -> None:
    business_id = str(uuid4())
    selected = make_slot(business_id, 0)
    message = FakeMessage()
    await owner._send_public_storefront(
        message,
        business_id=business_id,
        business_name="Мастер",
        slots=[selected],
        focused_slot_id=selected.slot.id,
    )
    generated = buttons(message)
    assert generated[0].callback_data.startswith("cp:book:")
    assert generated[1].callback_data == f"cp:client:{control._uuid_token(business_id)}"
