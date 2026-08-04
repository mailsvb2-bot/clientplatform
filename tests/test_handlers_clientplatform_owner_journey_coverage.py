from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.bookings import BookingSlot, BookingSlotStatus, BookingSlotView

control = importlib.import_module("handlers.clientplatform_control")
owner = importlib.import_module("handlers.clientplatform_owner_journey")


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id
        self.username = "visitor"
        self.full_name = "Посетитель"


class FakeBot:
    def __init__(self, username: str = "clientplatform_bot") -> None:
        self.username = username

    async def get_me(self) -> Any:
        return SimpleNamespace(username=self.username)


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.from_user = FakeUser()
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(
        self,
        data: str,
        *,
        user_id: int = 101,
        bot_username: str = "clientplatform_bot",
    ) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.bot = FakeBot(bot_username)
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
    monkeypatch.setattr(control, "Message", FakeMessage)


def _slot(
    *,
    business_id: str,
    offering_id: str | None = None,
    slot_id: str | None = None,
    status: BookingSlotStatus = BookingSlotStatus.OPEN,
    customer_id: str | None = None,
    offset_days: int = 5,
    title: str = "Замена раковины",
) -> BookingSlotView:
    starts = datetime.now(timezone.utc) + timedelta(days=offset_days)
    ends = starts + timedelta(minutes=60)
    booked_customer = customer_id
    if status == BookingSlotStatus.BOOKED and booked_customer is None:
        booked_customer = str(uuid4())
    record = BookingSlot(
        id=slot_id or str(uuid4()),
        business_id=business_id,
        offering_id=offering_id or str(uuid4()),
        starts_at=starts.isoformat(timespec="seconds"),
        ends_at=ends.isoformat(timespec="seconds"),
        duration_minutes=60,
        status=status,
        booked_customer_id=booked_customer,
        created_by_member_id=str(uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return BookingSlotView(
        slot=record,
        offering_title=title,
        business_name="Сантехник",
        timezone="Europe/Amsterdam",
    )


def _labels(message: FakeMessage) -> list[str]:
    markup = message.answers[-1][1].get("reply_markup")
    if markup is None:
        return []
    return [button.text for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_booking_start_and_replacement_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    offering_id = str(uuid4())
    old_slot_id = str(uuid4())
    replacement = _slot(business_id=business_id, offering_id=offering_id)
    message = FakeMessage("15.08.2026 18:30")
    state = FakeState({"replacing_slot_id": old_slot_id})

    await owner.receive_owner_booking_start(message, state)

    assert state.data["booking_start"] == "15.08.2026 18:30"
    assert state.states[-1] == control.ClientPlatformControlState.booking_duration
    assert "Новое время принято" in message.answers[-1][0]

    state.data.update(
        business_id=business_id,
        offering_id=offering_id,
        replacing_slot_id=old_slot_id,
    )
    message.text = "60"
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=object()))
    replace = AsyncMock(return_value=replacement)
    monkeypatch.setattr(owner, "replace_owner_booking_slot", replace)

    await owner.receive_owner_booking_duration(message, state)

    assert "Время изменено" in message.answers[-1][0]
    replace.assert_awaited_once()


@pytest.mark.asyncio
async def test_home_and_calendar_cover_visible_empty_and_truncated_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    callback = FakeCallback(f"cpj:home:{token}")
    state = FakeState({"old": "state"})
    dashboard = AsyncMock()
    monkeypatch.setattr(owner, "send_owner_dashboard", dashboard)

    await owner.open_owner_home(callback, state)

    assert state.clear_count == 1
    dashboard.assert_awaited_once()

    statuses = [
        BookingSlotStatus.OPEN,
        BookingSlotStatus.BOOKED,
        BookingSlotStatus.CANCELLED,
        BookingSlotStatus.COMPLETED,
    ]
    slots = [
        _slot(
            business_id=business_id,
            status=statuses[index % len(statuses)],
            offset_days=1 + index,
            title=f"Услуга {index + 1}",
        )
        for index in range(owner._CALENDAR_LIMIT + 2)
    ]
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=object()))
    monkeypatch.setattr(control, "list_booking_slots", lambda **_kwargs: slots)
    calendar = FakeCallback(f"cpj:calendar:{token}:30")

    await owner.open_owner_calendar(calendar)

    text = calendar.message.answers[-1][0]
    assert "Мой календарь" in text
    assert "…и ещё 2" in text
    labels = _labels(calendar.message)
    assert "7 дней" in labels
    assert "30 дней" in labels
    assert "Все" in labels
    assert "➕ Добавить время" in labels

    monkeypatch.setattr(control, "list_booking_slots", lambda **_kwargs: [])
    empty = FakeCallback(f"cpj:calendar:{token}:0")
    await owner.open_owner_calendar(empty)
    assert "пока нет" in empty.message.answers[-1][0]


@pytest.mark.asyncio
async def test_slot_card_preview_share_and_unavailable_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    open_slot = _slot(business_id=business_id)
    business_token = control._uuid_token(business_id)
    slot_token = control._uuid_token(open_slot.slot.id)
    monkeypatch.setattr(owner, "_owner_slot", AsyncMock(return_value=open_slot))

    card = FakeCallback(f"cpj:slot:{business_token}:{slot_token}")
    await owner.open_owner_slot(card)
    assert "Опубликованное время" in card.message.answers[-1][0]
    assert "👀 Посмотреть глазами клиента" in _labels(card.message)
    assert "🗑 Снять" in _labels(card.message)

    preview = FakeCallback(f"cpj:preview:{business_token}:{slot_token}")
    await owner.preview_owner_slot(preview)
    assert "Так карточку увидит клиент" in preview.message.answers[-1][0]
    assert "✅ Записаться · предпросмотр" in _labels(preview.message)

    noop = FakeCallback("cpj:previewnoop")
    await owner.preview_noop(noop)
    assert noop.answers[-1][1]["show_alert"] is True

    share = FakeCallback(f"cpj:share:{business_token}:{slot_token}")
    await owner.share_owner_slot(share)
    share_text, share_kwargs = share.message.answers[-1]
    assert "Готово к отправке и рекламе" in share_text
    assert "https://t.me/clientplatform_bot?start=cpss_" in share_text
    assert share_kwargs["reply_markup"].inline_keyboard[0][0].url.startswith(
        "https://t.me/share/url?"
    )

    booked = _slot(business_id=business_id, status=BookingSlotStatus.BOOKED)
    monkeypatch.setattr(owner, "_owner_slot", AsyncMock(return_value=booked))
    unavailable = FakeCallback(f"cpj:share:{business_token}:{slot_token}")
    await owner.share_owner_slot(unavailable)
    assert unavailable.answers[-1][1]["show_alert"] is True
    assert unavailable.message.answers == []

    missing_username = FakeCallback("x", bot_username="")
    with pytest.raises(RuntimeError, match="public username"):
        await owner._bot_username(missing_username)


@pytest.mark.asyncio
async def test_add_edit_and_cancel_slot_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    offering_id = str(uuid4())
    slot = _slot(business_id=business_id, offering_id=offering_id)
    business_token = control._uuid_token(business_id)
    offering_token = control._uuid_token(offering_id)
    slot_token = control._uuid_token(slot.slot.id)
    capability = SimpleNamespace(id=str(uuid4()), connector_key="services")
    programs = SimpleNamespace(id=str(uuid4()), connector_key="programs")
    offering = SimpleNamespace(id=offering_id)
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        control,
        "list_business_capabilities",
        lambda **_kwargs: [programs, capability],
    )
    monkeypatch.setattr(
        control,
        "list_business_offerings",
        lambda **_kwargs: [offering],
    )

    add_state = FakeState({"stale": True})
    add = FakeCallback(f"cpj:add:{business_token}:{offering_token}")
    await owner.add_another_slot(add, add_state)
    assert add_state.clear_count == 1
    assert add_state.states[-1] == control.ClientPlatformControlState.booking_start
    assert add_state.data == {"business_id": business_id, "offering_id": offering_id}
    assert "Напишите новое свободное время" in add.message.answers[-1][0]

    monkeypatch.setattr(control, "list_business_offerings", lambda **_kwargs: [])
    missing = FakeCallback(f"cpj:add:{business_token}:{offering_token}")
    await owner.add_another_slot(missing, FakeState())
    assert missing.answers[-1][1]["show_alert"] is True

    monkeypatch.setattr(owner, "_owner_slot", AsyncMock(return_value=slot))
    edit_state = FakeState()
    edit = FakeCallback(f"cpj:edit:{business_token}:{slot_token}")
    await owner.edit_owner_slot(edit, edit_state)
    assert edit_state.data["replacing_slot_id"] == slot.slot.id
    assert "Напишите новые дату и время" in edit.message.answers[-1][0]

    confirm = FakeCallback(f"cpj:cancel:{business_token}:{slot_token}")
    await owner.confirm_cancel_owner_slot(confirm)
    assert "Снять с публикации" in confirm.message.answers[-1][0]
    assert "Да, снять" in _labels(confirm.message)

    booked = _slot(business_id=business_id, status=BookingSlotStatus.BOOKED)
    monkeypatch.setattr(owner, "_owner_slot", AsyncMock(return_value=booked))
    blocked_edit = FakeCallback(f"cpj:edit:{business_token}:{slot_token}")
    await owner.edit_owner_slot(blocked_edit, FakeState())
    assert blocked_edit.answers[-1][1]["show_alert"] is True
    blocked_cancel = FakeCallback(f"cpj:cancel:{business_token}:{slot_token}")
    await owner.confirm_cancel_owner_slot(blocked_cancel)
    assert blocked_cancel.answers[-1][1]["show_alert"] is True

    cancelled = AsyncMock()
    render = AsyncMock()
    monkeypatch.setattr(owner, "cancel_owner_booking_slot", cancelled)
    monkeypatch.setattr(owner, "_render_calendar", render)
    accepted = FakeCallback(f"cpj:cancelok:{business_token}:{slot_token}")
    await owner.cancel_owner_slot(accepted)
    cancelled.assert_awaited_once()
    render.assert_awaited_once_with(accepted, business_id=business_id, days=30)


@pytest.mark.asyncio
async def test_services_and_customer_bookings_are_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    offering_id = str(uuid4())
    customer_id = str(uuid4())
    business_token = control._uuid_token(business_id)
    capability = SimpleNamespace(
        id=str(uuid4()),
        connector_key="services",
        status=CapabilityStatus.ACTIVE,
    )
    programs = SimpleNamespace(
        id=str(uuid4()),
        connector_key="programs",
        status=CapabilityStatus.ACTIVE,
    )
    offering = SimpleNamespace(
        id=offering_id,
        title="Замена раковины",
        description="Демонтаж и установка",
    )
    open_slot = _slot(business_id=business_id, offering_id=offering_id)
    booked_slot = _slot(
        business_id=business_id,
        offering_id=offering_id,
        status=BookingSlotStatus.BOOKED,
        customer_id=customer_id,
    )
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        control,
        "list_business_capabilities",
        lambda **_kwargs: [programs, capability],
    )
    monkeypatch.setattr(owner, "_all_offerings", AsyncMock(return_value=[offering]))
    monkeypatch.setattr(
        control,
        "list_booking_slots",
        lambda **_kwargs: [open_slot, booked_slot],
    )

    services = FakeCallback(f"cpj:services:{business_token}")
    await owner.open_owner_services(services)
    assert "Свободных времён: 1" in services.message.answers[-1][0]
    assert "➕ Добавить новую услугу" in _labels(services.message)
    assert "🕒 Добавить время · Замена раковины" in _labels(services.message)

    monkeypatch.setattr(
        control,
        "list_customers",
        lambda **_kwargs: [SimpleNamespace(id=customer_id, display_name="Анна")],
    )
    bookings = FakeCallback(f"cpj:bookings:{business_token}")
    await owner.open_customer_bookings(bookings)
    text = bookings.message.answers[-1][0]
    assert "Записи клиентов" in text
    assert "Анна" in text
    assert "Замена раковины" in text


@pytest.mark.asyncio
async def test_public_page_and_promotion_cover_ready_and_empty_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    business_token = control._uuid_token(business_id)
    capability = SimpleNamespace(
        id=str(uuid4()),
        connector_key="services",
        status=CapabilityStatus.ACTIVE,
    )
    offering = SimpleNamespace(id=str(uuid4()), title="Замена раковины")
    slot = _slot(business_id=business_id)
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        control,
        "list_business_capabilities",
        lambda **_kwargs: [capability],
    )
    monkeypatch.setattr(owner, "_all_offerings", AsyncMock(return_value=[offering]))
    monkeypatch.setattr(control, "list_booking_slots", lambda **_kwargs: [slot])

    page = FakeCallback(f"cpj:page:{business_token}")
    await owner.open_public_page_for_owner(page)
    page_text, page_kwargs = page.message.answers[-1]
    assert "Ваша публичная страница" in page_text
    assert "Замена раковины" in page_text
    assert "https://t.me/clientplatform_bot?start=cpsb_" in page_text
    assert page_kwargs["reply_markup"].inline_keyboard[0][0].url.startswith("https://t.me/")

    promotion = FakeCallback(f"cpj:promote:{business_token}")
    await owner.open_promotion(promotion)
    assert "готовый рекламный текст" in promotion.message.answers[-1][0]
    assert any(label.startswith("📢 ") for label in _labels(promotion.message))

    monkeypatch.setattr(owner, "_all_offerings", AsyncMock(return_value=[]))
    monkeypatch.setattr(control, "list_booking_slots", lambda **_kwargs: [])
    empty_page = FakeCallback(f"cpj:page:{business_token}")
    await owner.open_public_page_for_owner(empty_page)
    assert "услуги пока не добавлены" in empty_page.message.answers[-1][0]
    assert "свободного времени пока нет" in empty_page.message.answers[-1][0]

    empty_promotion = FakeCallback(f"cpj:promote:{business_token}")
    await owner.open_promotion(empty_promotion)
    assert "Сначала опубликуйте" in empty_promotion.message.answers[-1][0]


@pytest.mark.asyncio
async def test_public_storefront_handles_focused_empty_and_catalog_views() -> None:
    business_id = str(uuid4())
    first = _slot(business_id=business_id, title="Замена раковины")
    second = _slot(business_id=business_id, title="Монтаж смесителя")

    focused = FakeMessage()
    await owner._send_public_storefront(
        focused,
        business_id=business_id,
        business_name="Сантехник",
        slots=[first, second],
        focused_slot_id=first.slot.id,
    )
    assert "Можно записаться" in focused.answers[-1][0]
    assert "✅ Записаться" in _labels(focused)

    empty = FakeMessage()
    await owner._send_public_storefront(
        empty,
        business_id=business_id,
        business_name="Сантехник",
        slots=[],
        focused_slot_id=None,
    )
    assert "свободного времени сейчас нет" in empty.answers[-1][0]
    assert empty.answers[-1][1].get("reply_markup") is None

    catalog = FakeMessage()
    await owner._send_public_storefront(
        catalog,
        business_id=business_id,
        business_name="Сантехник",
        slots=[first, second],
        focused_slot_id=None,
    )
    assert "Доступно для записи" in catalog.answers[-1][0]
    assert "Замена раковины" in catalog.answers[-1][0]
    assert "Монтаж смесителя" in catalog.answers[-1][0]
    assert len(catalog.answers[-1][1]["reply_markup"].inline_keyboard) == 2


def test_owner_helpers_render_all_statuses_and_links() -> None:
    business_id = str(uuid4())
    labels = {
        BookingSlotStatus.OPEN: "свободно",
        BookingSlotStatus.BOOKED: "клиент записан",
        BookingSlotStatus.CANCELLED: "снято с публикации",
        BookingSlotStatus.COMPLETED: "завершено",
    }
    for status, expected in labels.items():
        slot = _slot(business_id=business_id, status=status)
        assert owner._slot_status(slot)[1] == expected
        assert expected in owner._slot_text(slot)
        assert slot.offering_title in owner._slot_button_text(slot)

    slot = _slot(business_id=business_id)
    business_link = owner._public_link("bot", business_id=business_id)
    slot_link = owner._public_link("bot", business_id=business_id, slot=slot)
    assert "start=cpsb_" in business_link
    assert "start=cpss_" in slot_link
    assert "Записаться:" in owner._promotion_text(slot, slot_link)
