from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
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
async def test_booking_start_without_business_fails_closed() -> None:
    message = FakeMessage("10.08.2026 15:00")
    state = FakeState({"offering_id": str(uuid4())})

    await wizard.receive_booking_start_with_quick_duration(message, state)

    assert state.cleared == 1
    assert "Откройте кабинет через /start" in message.answers[-1][0]


@pytest.mark.asyncio
async def test_replacement_booking_start_uses_replacement_copy() -> None:
    business_id = str(uuid4())
    message = FakeMessage("10.08.2026 16:00")
    state = FakeState(
        {
            "business_id": business_id,
            "offering_id": str(uuid4()),
            "replacing_slot_id": str(uuid4()),
        }
    )

    await wizard.receive_booking_start_with_quick_duration(message, state)

    assert message.answers[-1][0].startswith("Новое время принято.")


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
        patch.object(
            wizard,
            "_owner_module",
            return_value=SimpleNamespace(receive_owner_booking_duration=completion),
        ),
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
async def test_quick_duration_rejects_unknown_preset() -> None:
    business_id = str(uuid4())
    token = wizard.control._uuid_token(business_id)
    callback = FakeCallback(f"cpj:wizdur:{token}:75")
    state = FakeState({"business_id": business_id})
    actor = AsyncMock()

    with patch.object(wizard.control, "_actor", new=actor):
        await wizard.choose_quick_duration(callback, state)

    actor.assert_not_awaited()
    assert callback.answers[-1] == ("Выберите длительность заново", True)


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
        patch.object(
            wizard,
            "get_business_profile",
            return_value=SimpleNamespace(timezone="Europe/Moscow"),
        ),
    ):
        await wizard.return_to_booking_start(callback, state)

    assert state.states[-1] == wizard.control.ClientPlatformControlState.booking_start
    assert state.data["offering_id"] == offering_id
    assert state.data["booking_picker_timezone"] == "Europe/Moscow"
    text, markup = message.answers[-1]
    assert "Выберите новые дату и время" in text
    assert "Выберите дату свободного времени" in text
    assert "✍️ Ввести вручную" in _labels(markup)
    assert "✖️ Отмена" in _labels(markup)


@pytest.mark.asyncio
async def test_date_then_time_buttons_build_booking_start_without_manual_typing() -> None:
    business_id = str(uuid4())
    offering_id = str(uuid4())
    token = wizard.control._uuid_token(business_id)
    message = FakeMessage()
    state = FakeState(
        {
            "business_id": business_id,
            "offering_id": offering_id,
            "booking_picker_min_date": "2026-09-20",
            "booking_picker_timezone": "Europe/Moscow",
        }
    )
    date_callback = FakeCallback(f"cpj:wizdate:{token}:2026-09-22", message)
    with (
        patch.object(wizard.control, "_actor", new=AsyncMock(return_value=object())),
        patch.object(wizard.control, "_callback_message", return_value=message),
    ):
        await wizard.choose_booking_date(date_callback, state)
    assert state.data["booking_picker_date"] == "2026-09-22"
    assert "19:00" in _labels(message.answers[-1][1])

    time_callback = FakeCallback(f"cpj:wiztime:{token}:1900", message)
    with (
        patch.object(wizard.control, "_actor", new=AsyncMock(return_value=object())),
        patch.object(wizard.control, "_callback_message", return_value=message),
    ):
        await wizard.choose_booking_time(time_callback, state)

    assert state.data["booking_start"] == "22.09.2026 19:00"
    assert state.states[-1] == wizard.control.ClientPlatformControlState.booking_duration
    text, markup = message.answers[-1]
    assert "Дата и время приняты" in text
    assert "60 мин" in _labels(markup)


@pytest.mark.asyncio
async def test_booking_date_picker_keeps_manual_fallback_visible() -> None:
    business_id = str(uuid4())
    message = FakeMessage()
    state = FakeState({"business_id": business_id, "offering_id": str(uuid4())})

    with patch.object(wizard, "local_today", return_value=__import__("datetime").date(2026, 9, 20)):
        await wizard.send_booking_date_picker(
            message,
            state,
            business_id=business_id,
            timezone_name="Europe/Moscow",
        )

    text, markup = message.answers[-1]
    labels = _labels(markup)
    assert "Выберите дату свободного времени" in text
    assert "20 сен" in labels
    assert "✍️ Ввести вручную" in labels
    assert "✖️ Отмена" in labels


def test_booking_date_picker_last_page_clamps_to_supported_range() -> None:
    business_id = str(uuid4())
    minimum = date(2026, 1, 1)
    markup = wizard._date_keyboard(
        business_id,
        minimum=minimum,
        offset=wizard._MAX_DATE_DAYS - 1,
    )
    date_callbacks = [
        str(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if str(button.callback_data or "").startswith("cpj:wizdate:")
    ]

    assert len(date_callbacks) == 2
    rendered_dates = [
        date.fromisoformat(callback.rsplit(":", 1)[-1])
        for callback in date_callbacks
    ]
    assert max(rendered_dates) == minimum + timedelta(days=wizard._MAX_DATE_DAYS)
    assert all(
        rendered <= minimum + timedelta(days=wizard._MAX_DATE_DAYS)
        for rendered in rendered_dates
    )


@pytest.mark.asyncio
async def test_date_page_navigation_rerenders_and_rejects_out_of_range_offset() -> None:
    business_id = str(uuid4())
    token = wizard.control._uuid_token(business_id)
    message = FakeMessage()
    message.edit_reply_markup = AsyncMock()
    state = FakeState(
        {
            "business_id": business_id,
            "offering_id": str(uuid4()),
            "booking_picker_min_date": "2026-01-01",
        }
    )
    callback = FakeCallback(f"cpj:wizdatepage:{token}:364", message)
    with (
        patch.object(wizard.control, "_actor", new=AsyncMock(return_value=object())),
        patch.object(wizard.control, "_callback_message", return_value=message),
    ):
        await wizard.choose_booking_date_page(callback, state)

    message.edit_reply_markup.assert_awaited_once()
    markup = message.edit_reply_markup.await_args.kwargs["reply_markup"]
    date_callbacks = [
        str(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if str(button.callback_data or "").startswith("cpj:wizdate:")
    ]
    assert len(date_callbacks) == 2
    assert callback.answers[-1] == (None, False)

    stale = FakeCallback(
        f"cpj:wizdatepage:{token}:{wizard._MAX_DATE_DAYS + 1}",
        message,
    )
    with patch.object(
        wizard.control,
        "_actor",
        new=AsyncMock(return_value=object()),
    ):
        await wizard.choose_booking_date_page(stale, state)
    assert stale.answers[-1] == (
        "Выбор даты устарел. Откройте услугу заново.",
        True,
    )


@pytest.mark.asyncio
async def test_invalid_date_and_time_callbacks_fail_closed() -> None:
    business_id = str(uuid4())
    token = wizard.control._uuid_token(business_id)
    state = FakeState(
        {
            "business_id": business_id,
            "offering_id": str(uuid4()),
            "booking_picker_min_date": "2026-09-20",
            "booking_picker_date": "2026-09-22",
        }
    )

    invalid_date = FakeCallback(f"cpj:wizdate:{token}:2026-09-19")
    with patch.object(
        wizard.control,
        "_actor",
        new=AsyncMock(return_value=object()),
    ):
        await wizard.choose_booking_date(invalid_date, state)
    assert invalid_date.answers[-1] == ("Эта дата недоступна", True)

    invalid_time = FakeCallback(f"cpj:wiztime:{token}:abcd")
    with patch.object(
        wizard.control,
        "_actor",
        new=AsyncMock(return_value=object()),
    ):
        await wizard.choose_booking_time(invalid_time, state)
    assert invalid_time.answers[-1] == ("Выберите дату и время заново", True)


@pytest.mark.asyncio
async def test_manual_datetime_fallback_and_duration_proxy_answer() -> None:
    business_id = str(uuid4())
    token = wizard.control._uuid_token(business_id)
    message = FakeMessage()
    state = FakeState(
        {
            "business_id": business_id,
            "offering_id": str(uuid4()),
        }
    )
    callback = FakeCallback(f"cpj:wizmanual:{token}", message)
    with (
        patch.object(wizard.control, "_actor", new=AsyncMock(return_value=object())),
        patch.object(wizard.control, "_callback_message", return_value=message),
    ):
        await wizard.choose_manual_booking_datetime(callback, state)

    assert state.states[-1] == wizard.control.ClientPlatformControlState.booking_start
    text, markup = message.answers[-1]
    assert "Напишите дату и время" in text
    assert "✖️ Отмена" in _labels(markup)

    proxy = wizard._DurationMessageProxy(
        message,
        SimpleNamespace(id=101),
        75,
    )
    await proxy.answer("Готово")
    assert message.answers[-1][0] == "Готово"


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
        patch.object(
            wizard,
            "_owner_module",
            return_value=SimpleNamespace(send_owner_dashboard=dashboard),
        ),
    ):
        await wizard.cancel_booking_wizard(callback, state)

    assert state.cleared == 1
    dashboard.assert_awaited_once_with(message, user_id=101, business_id=business_id)
    assert callback.answers[-1][0] == "Настройка отменена"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "callback_data"),
    [
        (wizard.choose_quick_duration, "cpj:wizdur:{token}:60"),
        (wizard.choose_custom_duration, "cpj:wizcustom:{token}"),
        (wizard.return_to_booking_start, "cpj:wizback:{token}"),
        (wizard.cancel_booking_wizard, "cpj:wizcancel:{token}"),
    ],
)
async def test_stale_business_callback_fails_closed(handler, callback_data: str) -> None:
    selected_business = str(uuid4())
    other_business = str(uuid4())
    token = wizard.control._uuid_token(selected_business)
    callback = FakeCallback(callback_data.format(token=token))
    state = FakeState({"business_id": other_business, "offering_id": str(uuid4())})
    actor = AsyncMock()

    with patch.object(wizard.control, "_actor", new=actor):
        await handler(callback, state)

    actor.assert_not_awaited()
    assert state.cleared == 0
    assert callback.answers[-1] == (
        "Этот шаг уже устарел. Откройте кабинет заново.",
        True,
    )


def test_booking_wizard_router_precedes_legacy_simple_router() -> None:
    source = (Path(__file__).resolve().parents[1] / "handlers/clientplatform_entry.py").read_text(
        encoding="utf-8"
    )
    wizard_include = "router.include_router(booking_wizard_ux.router)"
    simple_include = "router.include_router(simple_experience.router)"
    assert wizard_include in source
    assert simple_include in source
    assert source.index(wizard_include) < source.index(simple_include)


def test_booking_wizard_keeps_owner_journey_lazy_to_avoid_router_cycle() -> None:
    source = Path(wizard.__file__).read_text(encoding="utf-8")
    assert 'owner = importlib.import_module(".clientplatform_owner_journey"' not in source
    assert 'def _owner_module()' in source
