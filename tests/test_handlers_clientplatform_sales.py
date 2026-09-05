from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from clientplatform.domain.sales_metrics import SalesFunnelCounts, SalesFunnelSnapshot

sales = importlib.import_module("handlers.clientplatform_sales")
install = importlib.import_module("handlers.clientplatform_sales_install")
simple = importlib.import_module("handlers.clientplatform_simple_experience")
control = importlib.import_module("handlers.clientplatform_control")


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, user_id: int = 101, text: str | None = None) -> None:
        self.from_user = FakeUser(user_id)
        self.text = text
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, data: str, user_id: int = 101) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id)
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


def _snapshot() -> SalesFunnelSnapshot:
    return SalesFunnelSnapshot(
        total=SalesFunnelCounts(
            discovered=8,
            engaged=6,
            qualified=4,
            checkout=3,
            won=2,
            lost=1,
            open_handoffs=1,
        ),
        by_source={
            "telegram": SalesFunnelCounts(discovered=5, engaged=4, qualified=3, checkout=2, won=2),
            "website": SalesFunnelCounts(discovered=3, engaged=2, qualified=1, checkout=1, lost=1),
        },
    )


def test_sales_entry_is_added_to_simple_dashboard_once() -> None:
    install.install_sales_ui(simple)
    install.install_sales_ui(simple)
    business_id = str(uuid4())
    markup = simple._simple_keyboard(business_id)
    sales_buttons = [
        button
        for row in markup.inline_keyboard
        for button in row
        if button.text == "💬 Обращения и продажи"
    ]
    assert len(sales_buttons) == 1
    assert sales_buttons[0].callback_data == f"cps:s:{control._uuid_token(business_id)}"


@pytest.mark.asyncio
async def test_sales_home_is_plain_language_and_never_claims_to_auto_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    monkeypatch.setattr(sales, "list_sales_handoff_work", lambda **_kwargs: [{"id": str(uuid4())}])
    monkeypatch.setattr(sales, "get_sales_funnel_snapshot", lambda **_kwargs: _snapshot())
    message = FakeMessage()

    await sales.send_sales_home(message, user_id=101, business_id=business_id)

    text, kwargs = message.answers[-1]
    assert "💬 Обращения и продажи" in text
    assert "Ничего не отправляется клиенту без Вашего подтверждения" in text
    labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert labels == [
        "💬 Обращения",
        "🙋 Нужно подключиться",
        "📊 Как идут продажи",
        "🧩 Что предлагать",
        "♻️ Вернуть клиентов",
        "🛠 Управлять обращениями",
        "⬅️ Назад",
        "🏠 Главная",
    ]


@pytest.mark.asyncio
async def test_work_queue_shows_persisted_plan_candidate_and_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    plan_id = str(uuid4())
    token = control._uuid_token(business_id)
    monkeypatch.setattr(
        sales,
        "list_sales_work",
        lambda **_kwargs: [
            {
                "customer_name": "Анна",
                "stage": "qualified",
                "source_kind": "telegram",
                "next_plan_id": plan_id,
                "next_action_kind": "present_offer",
                "next_plan_status": "planned",
                "next_plan_requires_approval": 1,
                "commercial_candidate_title": "Основная консультация",
            },
            {
                "customer_name": "Борис",
                "stage": "new",
                "source_kind": "website",
                "next_plan_id": None,
                "next_action_kind": None,
                "next_plan_status": None,
                "next_plan_requires_approval": None,
            },
        ],
    )
    callback = FakeCallback(f"cps:sw:{token}")

    await sales.open_sales_work(callback, FakeState())

    text, kwargs = callback.message.answers[-1]
    assert "Анна" in text and "Готов к предложению" in text
    assert "Предложить подходящую услугу" in text
    assert "Что можно предложить: Основная консультация" in text
    assert "Борис" in text and "пока не определён" in text
    assert "model_confidence" not in text
    buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
    approval = next(button for button in buttons if button.text == "✅ Одобрить шаг для 1")
    assert approval.callback_data == (
        f"cps:swa:{control._uuid_token(business_id)}:{control._uuid_token(plan_id)}"
    )
    assert all(len(str(button.callback_data).encode("utf-8")) <= 64 for button in buttons)


@pytest.mark.asyncio
async def test_owner_approval_opens_dispatch_gate_then_refreshes_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id, plan_id = str(uuid4()), str(uuid4())
    callback = FakeCallback(
        f"cps:swa:{control._uuid_token(business_id)}:{control._uuid_token(plan_id)}"
    )
    captured: dict[str, Any] = {}

    def approve(**kwargs: Any) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "plan_id": plan_id,
            "platform": "telegram",
            "external_subject": "202",
            "dispatch_allowed": True,
        }

    monkeypatch.setattr(sales, "approve_and_authorize_sales_outbound", approve)
    refresh = AsyncMock()
    monkeypatch.setattr(sales, "send_sales_work_view", refresh)
    state = FakeState({"stale": True})

    await sales.approve_sales_plan(callback, state)

    assert captured["plan_id"] == plan_id
    assert captured["actor"].business_id == business_id
    assert state.clear_count == 1
    assert callback.answers[-1][0] == ("Одобрено — отправка разрешена",)
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_handoff_queue_has_one_tap_actions_and_callback_data_fit_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    monkeypatch.setattr(
        sales,
        "list_sales_handoff_work",
        lambda **_kwargs: [
            {
                "id": str(uuid4()),
                "customer_name": "Анна",
                "reason": "explicit_request",
                "severity": "high",
                "status": "open",
            }
        ],
    )
    callback = FakeCallback(f"cps:sh:{token}")

    await sales.open_sales_handoffs(callback, FakeState())

    text, kwargs = callback.message.answers[-1]
    assert "Клиент попросил человека" in text
    buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert {button.text for button in buttons} >= {"✋ Взять", "✅ Готово"}
    assert all(len(str(button.callback_data).encode("utf-8")) <= 64 for button in buttons)


@pytest.mark.asyncio
async def test_funnel_renders_only_snapshot_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    monkeypatch.setattr(sales, "get_sales_funnel_snapshot", lambda **_kwargs: _snapshot())
    callback = FakeCallback(f"cps:sf:{token}")

    await sales.open_sales_funnel(callback, FakeState())

    text = callback.message.answers[-1][0]
    assert "Обращений: 8" in text
    assert "Оплатили: 2" in text
    assert "Telegram: 5 обращ. · 2 оплат" in text
    assert "подтверждённые действия клиентов" in text
    assert "без догадок" in text


@pytest.mark.asyncio
async def test_ladder_step_kind_callbacks_are_below_telegram_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    ladder_id = str(uuid4())
    monkeypatch.setattr(sales, "list_commercial_ladder_steps", lambda **_kwargs: [])
    callback = FakeCallback(
        f"cps:sla:{control._uuid_token(business_id)}:{control._uuid_token(ladder_id)}"
    )

    await sales.begin_ladder_step(callback, FakeState())

    buttons = [
        button
        for row in callback.message.answers[-1][1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert all(len(str(button.callback_data).encode("utf-8")) <= 64 for button in buttons)
    assert any(button.text == "🎯 Основная услуга" for button in buttons)


@pytest.mark.asyncio
async def test_new_ladder_step_is_fail_safe_and_requires_owner_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    ladder_id = str(uuid4())
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        sales,
        "list_commercial_ladder_steps",
        lambda **_kwargs: [{"position": 0}],
    )

    def add_step(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(sales, "add_commercial_ladder_step", add_step)
    monkeypatch.setattr(sales, "_send_ladder_detail", AsyncMock())
    state = FakeState(
        {
            "business_id": business_id,
            "ladder_id": ladder_id,
            "kind": "implementation",
        }
    )
    message = FakeMessage(text="Основная консультация")

    await sales.receive_ladder_step_title(message, state)

    assert captured["position"] == 1
    assert captured["kind"] == "implementation"
    assert captured["min_evidence_score"] == 0.0
    assert captured["requires_human_approval"] is True
    assert state.clear_count == 1
