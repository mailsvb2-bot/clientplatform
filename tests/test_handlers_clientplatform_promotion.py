from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from clientplatform.application.promotions import (
    PromotionCampaignView,
    PromotionLanding,
)
from clientplatform.domain.bookings import BookingSlot, BookingSlotStatus, BookingSlotView
from clientplatform.domain.promotions import (
    PromotionCampaign,
    PromotionCampaignStatus,
    PromotionChannel,
    PromotionCreative,
    PromotionStats,
    stable_creative_id,
)

handlers_package = importlib.import_module("handlers")
entry, control = handlers_package._load_clientplatform_modules()
simple = importlib.import_module("handlers.clientplatform_simple_experience")
owner = importlib.import_module("handlers.clientplatform_owner_journey")
promotion = importlib.import_module("handlers.clientplatform_promotion")


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id
        self.username = "visitor"
        self.full_name = "Посетитель"


class FakeBot:
    id = 999

    async def get_me(self) -> Any:
        return SimpleNamespace(username="clientplatform_bot")


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.from_user = FakeUser()
        self.bot = FakeBot()
        self.answers: list[tuple[str, dict[str, Any]]] = []
        self.documents: list[tuple[Any, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))

    async def answer_document(self, document: Any, **kwargs: Any) -> None:
        self.documents.append((document, kwargs))


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
    def __init__(self) -> None:
        self.clear_count = 0

    async def clear(self) -> None:
        self.clear_count += 1


async def direct_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


def _slot(
    *,
    business_id: str | None = None,
    offering_id: str | None = None,
    slot_id: str | None = None,
    status: BookingSlotStatus = BookingSlotStatus.OPEN,
) -> BookingSlotView:
    starts = datetime.now(timezone.utc) + timedelta(days=5)
    ends = starts + timedelta(minutes=60)
    slot = BookingSlot(
        id=slot_id or str(uuid4()),
        business_id=business_id or str(uuid4()),
        offering_id=offering_id or str(uuid4()),
        starts_at=starts.isoformat(timespec="seconds"),
        ends_at=ends.isoformat(timespec="seconds"),
        duration_minutes=60,
        status=status,
        booked_customer_id=None,
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


def _campaign(slot: BookingSlotView, *, channel: PromotionChannel = PromotionChannel.TELEGRAM):
    creative = PromotionCreative(
        creative_id=stable_creative_id(slot.slot.id, channel.value),
        headline="Замена раковины",
        primary_text="Свободное время 10 августа. Запишитесь онлайн.",
        description="Сантехник · 60 минут",
    )
    return PromotionCampaign(
        id=str(uuid4()),
        business_id=slot.slot.business_id,
        offering_id=slot.slot.offering_id,
        booking_slot_id=slot.slot.id,
        channel=channel,
        source_token="abcdefghijklmnop",
        creative=creative,
        status=PromotionCampaignStatus.ACTIVE,
        created_by_member_id=str(uuid4()),
        created_at="2026-08-05T00:00:00+00:00",
        updated_at="2026-08-05T00:00:00+00:00",
    )


@pytest.fixture(autouse=True)
def direct_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(control, "Message", FakeMessage)


def test_promotion_engine_is_composed_without_replacing_booking_core() -> None:
    assert owner._promotion_engine_installed is True
    assert owner.open_promotion is promotion.open_promotion_workspace
    assert any(
        getattr(handler, "callback", None) is promotion.open_promotion_workspace
        for handler in simple.router.callback_query.handlers
    )
    assert promotion.book_promoted_slot.__module__ == "clientplatform.application.promotions"
    assert control.book_customer_slot.__module__ == "clientplatform.application.bookings"


def test_owner_dashboard_exposes_new_client_acquisition_instead_of_abstract_promotion() -> None:
    business_id = str(uuid4())
    labels = [
        button.text
        for row in owner._owner_keyboard(business_id).inline_keyboard
        for button in row
    ]
    assert labels == ["🚀 Найти новых клиентов", "⋯ Все возможности"]
    assert "💬 Обращения и продажи" not in labels
    assert "📢 Продвижение" not in labels


@pytest.mark.asyncio
async def test_publish_receipt_routes_to_attributable_promotion() -> None:
    published = _slot()
    message = FakeMessage()

    await owner._send_publish_receipt(message, slot=published)

    text, kwargs = message.answers[-1]
    assert "рекламное предложение" in text
    buttons = [
        button
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    get_clients = next(button for button in buttons if button.text == "🚀 Получить клиентов")
    assert get_clients.callback_data.startswith("cpp:slot:")
    assert all(len(button.callback_data or "") <= 64 for button in buttons)


@pytest.mark.asyncio
async def test_workspace_shows_measurable_results_and_promotion_scoped_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    business_token = control._uuid_token(business_id)
    slot = _slot(business_id=business_id)
    campaign = _campaign(slot)
    actor = object()
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=actor))
    slot_calls: list[object] = []

    def promotion_slots(**kwargs: Any) -> list[BookingSlotView]:
        slot_calls.append(kwargs["actor"])
        return [slot]

    monkeypatch.setattr(promotion, "list_promotable_slots", promotion_slots)
    monkeypatch.setattr(
        control,
        "list_booking_slots",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("promotion workspace must not require customer-record slots")
        ),
    )
    monkeypatch.setattr(
        promotion,
        "promotion_stats",
        lambda **_kwargs: PromotionStats(campaigns=2, people_opened=10, bookings=3),
    )
    monkeypatch.setattr(
        promotion,
        "list_promotion_campaigns",
        lambda **_kwargs: [PromotionCampaignView(campaign=campaign, slot=slot)],
    )
    callback = FakeCallback(f"cpj:promote:{business_token}")

    await promotion.open_promotion_workspace(callback)

    assert slot_calls == [actor]
    text, kwargs = callback.message.answers[-1]
    assert "🚀 Получить клиентов" in text
    assert "Уникальных людей: 10" in text
    assert "Записались: 3" in text
    assert "Конверсия в запись: 30.0%" in text
    buttons = [
        button
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert any((button.callback_data or "").startswith("cpp:slot:") for button in buttons)
    assert all(len(button.callback_data or "") <= 64 for button in buttons)


@pytest.mark.asyncio
async def test_choose_channel_revalidates_promotable_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    slot = _slot(business_id=business_id)
    business_token = control._uuid_token(business_id)
    slot_token = control._uuid_token(slot.slot.id)
    actor = object()
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(promotion, "list_promotable_slots", lambda **_kwargs: [slot])
    callback = FakeCallback(f"cpp:slot:{business_token}:{slot_token}")

    await promotion.choose_promotion_channel(callback)

    assert callback.answers == [((), {})]
    text, kwargs = callback.message.answers[-1]
    assert "Куда Вы хотите вывести это предложение?" in text
    labels = [
        button.text
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "✈️ Telegram" in labels
    assert "🔵 ВКонтакте" in labels
    assert "🟣 MAX" in labels
    assert "⬅️ Назад" in labels


@pytest.mark.asyncio
async def test_channel_material_has_stable_tagged_link_and_safe_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    slot = _slot(business_id=business_id)
    campaign = _campaign(slot)
    view = PromotionCampaignView(campaign=campaign, slot=slot)
    business_token = control._uuid_token(business_id)
    slot_token = control._uuid_token(slot.slot.id)
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=object()))
    monkeypatch.setattr(promotion, "create_slot_promotion", lambda **_kwargs: view)
    monkeypatch.setattr(
        promotion.settings,
        "MESSENGER_PUBLIC_BASE_URL",
        "https://client.example.test",
    )
    callback = FakeCallback(
        f"cpp:make:{business_token}:{slot_token}:{PromotionChannel.TELEGRAM.value}"
    )

    await promotion.create_promotion_material(callback)

    text, kwargs = callback.message.answers[-1]
    assert "Готово для канала «Telegram»" in text
    assert (
        "https://client.example.test/clientplatform/acquire?source=cpa_abcdefghijklmnop"
        in text
    )
    assert "уникальных людей" in text
    buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert any(button.url and button.url.startswith("https://t.me/share/url") for button in buttons)
    assert all(len(button.callback_data or "") <= 64 for button in buttons)


@pytest.mark.asyncio
async def test_max_material_uses_max_attribution_with_neutral_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    slot = _slot(business_id=business_id)
    campaign = _campaign(slot, channel=PromotionChannel.MAX)
    view = PromotionCampaignView(campaign=campaign, slot=slot)
    business_token = control._uuid_token(business_id)
    slot_token = control._uuid_token(slot.slot.id)
    calls: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> PromotionCampaignView:
        calls.append(dict(kwargs))
        return view

    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=object()))
    monkeypatch.setattr(promotion, "create_slot_promotion", create)
    monkeypatch.setattr(
        promotion.settings,
        "MESSENGER_PUBLIC_BASE_URL",
        "https://client.example.test",
    )
    callback = FakeCallback(
        f"cpp:make:{business_token}:{slot_token}:{PromotionChannel.MAX.value}"
    )

    await promotion.create_promotion_material(callback)

    assert calls[0]["channel"] == PromotionChannel.MAX
    text, kwargs = callback.message.answers[-1]
    assert "Готово для канала «MAX»" in text
    assert "client.example.test/clientplatform/acquire?source=cpa_abcdefghijklmnop" in text
    buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert not any(
        button.text == "📨 Опубликовать/отправить в Telegram" for button in buttons
    )


@pytest.mark.asyncio
async def test_promotion_start_opens_exact_offer_and_records_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot = _slot()
    campaign = _campaign(slot)
    landing = PromotionLanding(
        campaign=campaign,
        slot=slot,
        customer_id=str(uuid4()),
    )
    opened: list[dict[str, Any]] = []

    def open_link(**kwargs: Any) -> PromotionLanding:
        opened.append(dict(kwargs))
        return landing

    monkeypatch.setattr(promotion, "open_promotion_link", open_link)
    message = FakeMessage("/start cpa_abcdefghijklmnop")
    state = FakeState()

    handled = await promotion.dispatch_promotion_start(
        message,
        state,
        user_id=101,
        managed_bot_business_id=None,
    )

    assert handled is True
    assert opened[0]["source_token"] == "abcdefghijklmnop"
    assert state.clear_count == 1
    text, kwargs = message.answers[-1]
    assert "Замена раковины" in text
    assert "Время свободно" in text
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "✅ Записаться"
    assert button.callback_data == "cpp:book:abcdefghijklmnop"


@pytest.mark.asyncio
async def test_unrelated_and_malformed_start_payloads_remain_on_canonical_path() -> None:
    message = FakeMessage("/start ordinary")
    state = FakeState()
    assert await promotion.dispatch_promotion_start(
        message,
        state,
        user_id=101,
        managed_bot_business_id=None,
    ) is False

    message.text = "/start cpa_bad!"
    assert await promotion.dispatch_promotion_start(
        message,
        state,
        user_id=101,
        managed_bot_business_id=None,
    ) is False


@pytest.mark.asyncio
async def test_managed_business_bot_opens_canonical_promotion_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot = _slot()
    landing = PromotionLanding(
        campaign=_campaign(slot),
        slot=slot,
        customer_id=str(uuid4()),
    )
    business_id = slot.slot.business_id
    opened: list[dict[str, Any]] = []

    def open_identity(**kwargs: Any) -> PromotionLanding:
        opened.append(dict(kwargs))
        return landing

    monkeypatch.setattr(promotion, "open_channel_promotion_for_identity", open_identity)
    message = FakeMessage("/start cpa_abcdefghijklmnop")
    state = FakeState()

    handled = await promotion.dispatch_promotion_start(
        message,
        state,
        user_id=101,
        managed_bot_business_id=business_id,
    )

    assert handled is True
    assert opened == [
        {
            "source_token": "abcdefghijklmnop",
            "business_id": business_id,
            "platform": "telegram",
            "external_subject": "101",
        }
    ]
    assert state.clear_count == 1
    assert "Время свободно" in message.answers[-1][0]
    button = message.answers[-1][1]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "cpp:book:abcdefghijklmnop"
