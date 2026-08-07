from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from handlers import clientplatform_booking_wizard_ux as wizard


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=101)
        self.answers: list[tuple[str, object | None]] = []
        self.edits = 0

    async def answer(self, text: str, reply_markup=None):
        self.answers.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None):
        self.edits += 1


class FakeState:
    def __init__(self, data: dict[str, object] | None = None) -> None:
        self.data = dict(data or {})
        self.states: list[object] = []
        self.cleared = 0

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, value):
        self.states.append(value)

    async def clear(self):
        self.cleared += 1
        self.data.clear()


class FakeCallback:
    def __init__(self, data: str, message: FakeMessage | None = None) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=101)
        self.message = message or FakeMessage()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, *, show_alert: bool = False):
        self.answers.append((text, show_alert))


def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_booking_start_offers_common_durations_and_escape_routes() -> None:
    business_id = str(uuid4())
    message = FakeMessage("10.08.2026 15:00")
    state = FakeState({"business_id": business_id, "offering_id": str(uuid4())})

    await wizard.receive_booking_start_with_quick_duration(message, state)

    assert state.data["booking_start"] == "10.08.2026 15:00"
    assert state.states[-1] == wizard.control.ClientPlatformControlState.booking_duration
    text, markup = message.answers[-1]
    assert "одного нажатия" in text
    labels = _labels(markup)
    assert labels[:4] == ["30 мин", "45 мин", "60 мин", "90 мин"]
    assert "Другая длительность" in labels
    assert "⬅️ Изменить дату и время" in labels
    assert "✖️ Отмена" in labels


@pytest.mark.asyncio
async def test_quick_duration_reuses_canonical_booking_completion() -> None:
    business_id = str(uuid4())
    token = wizard.control._uuid_token(business_id)
    message = FakeMessage()
    callback = FakeCallback(f"cpj:wizdur:{token}:60", message)
    state = FakeState(
        {
            "business_id": business_id,
            "offering_id": str(uuid4()),
            "booking_start": "10.08.2026 15:00",
        }
    )
    completion = AsyncMock()
    with (
        patch.object(wizard.control, "_actor", new=AsyncMock(return_value=object())),
        patch.object(wizard.control, "_callback_message", return_value=message),
        patch.object(wizard.owner, "receive_owner_booking_duration", new=completion),
    ):
        await wizard.choose_quick_duration(callback, state)

    completion.assert_awaited_once()
    proxy, forwarded_state = completion.await_args.args
    assert proxy.text == "60"
    assert proxy.from_user.id == 101
    assert forwarded_state is state
    assert message.edits == 1
    assert callback.answers[-1][0] == "60 минут"


@pytest.mark.asyncio
async def test_custom_duration_keeps_manual_fallback_and_visible_exit() -> None:
    business_id = str(uuid4())
    token = wizard.control._uuid_token(business_id)
    message = FakeMessage()
    callback = FakeCallback(f"cpj:wizcustom:{token}", message)
    state = FakeState(
        {
            "business_id": business_id,
            "offering_id": str(uuid4()),
            "booking_start": "10.08.2026 15:00",
        }
    )
    with (
        patch.object(wizard.control, "_actor", new=AsyncMock(return_value=object())),
        patch.object(wizard.control, "_callback_message", return_value=message),
    ):
        await wizard.choose_custom_duration(callback, state)

    text, markup = message.answers[-1]
    assert "Напишите длительность" in text
    assert "⬅️ Изменить дату и время" in _labels(markup)
    assert "✖️ Отмена" in _labels(markup)
    assert state.cleared == 0


@pytest.mark.asyncio
async def test_back_returns_to_date_entry_without_losing_booking_context() -> None:
    business_id = str(uuid4())
    offering_id = str(uuid4())
    token = wizard.control._uuid_token(business_id)
    message = FakeMessage()
    callback = FakeCallback(f"cpj:wizback:{token}", message)
    state = FakeState(
        {
            "business_id": business_id,
            "offering_id": offering_id,
            "booking_start": "10.08.2026 15:00",
        }
    )
    with (
        patch.object(wizard.control, "_actor", new=AsyncMock(return_value=object())),
        patch.object(wizard.control, "_callback_message", return_value=message),
    ):
        await wizard.return_to_booking_start(callback, state)

    assert state.states[-1] == wizard.control.ClientPlatformControlState.booking_start
    assert state.data["offering_id"] == offering_id
    text, markup = message.answers[-1]
    assert "Напишите дату и время заново" in text
    assert "✖️ Отмена" in _labels(markup)


@pytest.mark.asyncio
async def test_visible_cancel_clears_wizard_and_returns_owner_home() -> None:
    business_id = str(uuid4())
    token = wizard.control._uuid_token(business_id)
    message = FakeMessage()
    callback = FakeCallback(f"cpj:wizcancel:{token}", message)
    state = FakeState({"business_id": business_id, "offering_id": str(uuid4())})
    dashboard = AsyncMock()
    with (
        patch.object(wizard.control, "_actor", new=AsyncMock(return_value=object())),
        patch.object(wizard.control, "_callback_message", return_value=message),
        patch.object(wizard.owner, "send_owner_dashboard", new=dashboard),
    ):
        await wizard.cancel_booking_wizard(callback, state)

    assert state.cleared == 1
    dashboard.assert_awaited_once_with(message, user_id=101, business_id=business_id)
    assert callback.answers[-1][0] == "Настройка отменена"


def test_booking_wizard_router_precedes_legacy_simple_router() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "handlers/clientplatform_entry.py").read_text(
        encoding="utf-8"
    )
    wizard_include = "router.include_router(booking_wizard_ux.router)"
    simple_include = "router.include_router(simple_experience.router)"
    assert wizard_include in source
    assert simple_include in source
    assert source.index(wizard_include) < source.index(simple_include)
