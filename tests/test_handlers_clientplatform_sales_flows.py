from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

sales = importlib.import_module("handlers.clientplatform_sales")
control = importlib.import_module("handlers.clientplatform_control")


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, text: str | None = None, user_id: int = 101) -> None:
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, data: str, user_id: int = 101) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id=user_id)
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

    async def set_state(self, value: Any) -> None:
        self.states.append(value)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)


async def direct_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def direct_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sales.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(control, "Message", FakeMessage)
    monkeypatch.setattr(
        control,
        "_actor",
        AsyncMock(side_effect=lambda user_id, business_id: SimpleNamespace(
            user_id=user_id,
            business_id=business_id,
        )),
    )


@pytest.mark.asyncio
async def test_open_sales_home_clears_stale_wizard_state(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    callback = FakeCallback(f"cps:s:{control._uuid_token(business_id)}")
    state = FakeState({"stale": True})
    sender = AsyncMock()
    monkeypatch.setattr(sales, "send_sales_home", sender)

    await sales.open_sales_home(callback, state)

    assert state.clear_count == 1
    sender.assert_awaited_once()
    assert sender.await_args.kwargs["business_id"] == business_id


@pytest.mark.asyncio
async def test_empty_handoff_queue_has_clear_return_path(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    monkeypatch.setattr(sales, "list_sales_handoff_work", lambda **_kwargs: [])
    message = FakeMessage()

    await sales.send_sales_handoff_view(message, user_id=101, business_id=business_id)

    text, kwargs = message.answers[-1]
    assert "нет обращений" in text
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "← Обращения и продажи"


@pytest.mark.asyncio
async def test_claim_handoff_reuses_core_use_case_then_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id, handoff_id = str(uuid4()), str(uuid4())
    callback = FakeCallback(
        f"cps:shc:{control._uuid_token(business_id)}:{control._uuid_token(handoff_id)}"
    )
    captured: dict[str, Any] = {}

    def claim(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(sales, "claim_sales_handoff", claim)
    refresh = AsyncMock()
    monkeypatch.setattr(sales, "send_sales_handoff_view", refresh)
    state = FakeState({"old": True})

    await sales.claim_handoff(callback, state)

    assert captured["handoff_id"] == handoff_id
    assert captured["actor"].business_id == business_id
    assert state.clear_count == 1
    refresh.assert_awaited_once()
    assert callback.answers[-1][0] == ("Взято в работу",)


@pytest.mark.asyncio
async def test_resolve_handoff_reuses_core_use_case_then_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id, handoff_id = str(uuid4()), str(uuid4())
    callback = FakeCallback(
        f"cps:shr:{control._uuid_token(business_id)}:{control._uuid_token(handoff_id)}"
    )
    captured: dict[str, Any] = {}

    def resolve(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(sales, "resolve_sales_handoff", resolve)
    refresh = AsyncMock()
    monkeypatch.setattr(sales, "send_sales_handoff_view", refresh)

    await sales.resolve_handoff(callback, FakeState())

    assert captured["handoff_id"] == handoff_id
    refresh.assert_awaited_once()
    assert callback.answers[-1][0] == ("Готово",)


@pytest.mark.asyncio
async def test_handoff_mutation_failure_is_user_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id, handoff_id = str(uuid4()), str(uuid4())
    callback = FakeCallback(
        f"cps:shc:{control._uuid_token(business_id)}:{control._uuid_token(handoff_id)}"
    )

    def fail(**_kwargs: Any) -> object:
        raise RuntimeError("internal database detail")

    monkeypatch.setattr(sales, "claim_sales_handoff", fail)

    await sales.claim_handoff(callback, FakeState())

    args, kwargs = callback.answers[-1]
    assert "internal database detail" not in args[0]
    assert kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_offer_set_list_renders_existing_and_create_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id, ladder_id = str(uuid4()), str(uuid4())
    monkeypatch.setattr(
        sales,
        "list_commercial_ladders",
        lambda **_kwargs: [{"id": ladder_id, "name": "Основной путь", "step_count": 3}],
    )
    message = FakeMessage()

    await sales._send_ladders(message, user_id=101, business_id=business_id)

    text, kwargs = message.answers[-1]
    assert "Основной путь · этапов: 3" in text
    buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert {button.text for button in buttons} >= {
        "🧩 Основной путь",
        "➕ Создать набор предложений",
        "← Обращения и продажи",
    }
    assert all(len(str(button.callback_data).encode("utf-8")) <= 64 for button in buttons)


@pytest.mark.asyncio
async def test_ladder_detail_uses_plain_labels_and_approval_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id, ladder_id = str(uuid4()), str(uuid4())
    monkeypatch.setattr(
        sales,
        "list_commercial_ladders",
        lambda **_kwargs: [{"id": ladder_id, "name": "Путь", "step_count": 1}],
    )
    monkeypatch.setattr(
        sales,
        "list_commercial_ladder_steps",
        lambda **_kwargs: [
            {
                "position": 0,
                "kind": "implementation",
                "title": "Основная консультация",
                "requires_human_approval": 1,
            }
        ],
    )
    message = FakeMessage()

    await sales._send_ladder_detail(
        message,
        user_id=101,
        business_id=business_id,
        ladder_id=ladder_id,
    )

    text = message.answers[-1][0]
    assert "Основная услуга" in text
    assert "с Вашим подтверждением" in text


@pytest.mark.asyncio
async def test_begin_and_finish_offer_set_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id, ladder_id = str(uuid4()), str(uuid4())
    token = control._uuid_token(business_id)
    callback = FakeCallback(f"cps:sln:{token}")
    state = FakeState()

    await sales.begin_ladder_creation(callback, state)
    assert state.states[-1] == sales.ClientPlatformSalesUiState.ladder_name
    assert state.data["business_id"] == business_id
    assert "Как назвать набор предложений" in callback.message.answers[-1][0]

    monkeypatch.setattr(sales, "create_commercial_ladder", lambda **_kwargs: ladder_id)
    detail = AsyncMock()
    monkeypatch.setattr(sales, "_send_ladder_detail", detail)
    message = FakeMessage(text="  Основной   путь  ")
    await sales.receive_ladder_name(message, state)

    assert state.clear_count >= 2
    assert "Набор предложений создан" in message.answers[-1][0]
    detail.assert_awaited_once()
    assert detail.await_args.kwargs["ladder_id"] == ladder_id


@pytest.mark.asyncio
async def test_choose_step_kind_stores_canonical_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id, ladder_id = str(uuid4()), str(uuid4())
    callback = FakeCallback(
        f"cps:slk:{control._uuid_token(business_id)}:{control._uuid_token(ladder_id)}:i"
    )
    monkeypatch.setattr(sales, "list_commercial_ladder_steps", lambda **_kwargs: [])
    state = FakeState()

    await sales.choose_ladder_step_kind(callback, state)

    assert state.data == {
        "business_id": business_id,
        "ladder_id": ladder_id,
        "kind": "implementation",
    }
    assert state.states[-1] == sales.ClientPlatformSalesUiState.ladder_step_title
    assert "Основная услуга" in callback.message.answers[-1][0]
