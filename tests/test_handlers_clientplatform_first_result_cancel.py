from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

first = importlib.import_module("handlers.clientplatform_first_result")
control = importlib.import_module("handlers.clientplatform_control")
simple = importlib.import_module("handlers.clientplatform_simple_experience")


class _Message:
    pass


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=101)
        self.message = _Message()
        self.answers: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def answer(self, *args: object, **kwargs: object) -> None:
        self.answers.append((args, kwargs))


class _State:
    def __init__(self) -> None:
        self.clear_count = 0

    async def clear(self) -> None:
        self.clear_count += 1


@pytest.mark.asyncio
async def test_visible_cancel_clears_wizard_and_returns_to_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    callback = _Callback(f"cps:cancelsetup:{token}")
    state = _State()
    actor = AsyncMock(return_value=object())
    dashboard = AsyncMock()

    monkeypatch.setattr(control, "_actor", actor)
    monkeypatch.setattr(control, "_callback_message", lambda _callback: callback.message)
    monkeypatch.setattr(simple, "send_simple_dashboard", dashboard)

    await first.cancel_first_result_setup(callback, state)

    assert state.clear_count == 1
    actor.assert_awaited_once_with(101, business_id)
    dashboard.assert_awaited_once_with(
        callback.message,
        user_id=101,
        business_id=business_id,
    )
    assert callback.answers[-1][0] == ("Настройка отменена",)


def test_first_result_wizards_expose_plain_visible_cancel() -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    button = first._cancel_setup_keyboard(token).inline_keyboard[0][0]

    assert button.text == "✖️ Отмена"
    assert button.callback_data == f"cps:cancelsetup:{token}"
