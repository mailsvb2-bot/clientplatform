from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from handlers import clientplatform_sales_operations as operations


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(
        self,
        *,
        user_id: int = 101,
        text: str | None = None,
        chat_id: int = 500,
        message_id: int = 700,
    ) -> None:
        self.from_user = FakeUser(user_id)
        self.text = text
        self.chat = SimpleNamespace(id=chat_id)
        self.message_id = message_id
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, data: str, *, user_id: int = 101) -> None:
        self.data = data
        self.id = "callback-1"
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

    async def set_state(self, state: Any) -> None:
        self.states.append(state)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)


async def direct_to_thread(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    return func(*args, **kwargs)


def _actor(user_id: int, business_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        business_id=business_id,
        membership_id=str(uuid4()),
    )


@pytest.fixture(autouse=True)
def operation_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def actor(user_id: int, business_id: str) -> SimpleNamespace:
        return _actor(user_id, business_id)

    monkeypatch.setattr(operations.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(operations.control, "_actor", actor)
    monkeypatch.setattr(
        operations.control,
        "_callback_message",
        lambda callback: callback.message,
    )


def _lead(
    *,
    stage: str = "qualified",
    assigned_user_id: int | None = 101,
    next_action: str | None = "Позвонить клиенту",
) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "customer_name": "Анна",
        "stage": stage,
        "assigned_member_id": (
            None if assigned_user_id is None else str(uuid4())
        ),
        "assigned_user_id": assigned_user_id,
        "next_action": next_action,
        "due_at": "2026-08-20T07:00:00+00:00" if next_action else None,
        "closure_reason": "нет бюджета" if stage == "lost" else None,
        "source_kind": "website",
        "source_ref": "landing-main",
        "attribution_source": "yandex_direct",
        "attribution_source_ref_type": "creative_variant",
        "attribution_source_ref_id": "creative-42",
        "attribution_promotion_campaign_id": "campaign-7",
    }


def _labels(markup: Any) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_helpers_render_assignee_attribution_and_stage_variants() -> None:
    business_id = str(uuid4())
    item = _lead()

    assert operations._short_time(None) == "без срока"
    assert operations._source_label(None) == "Другой источник"
    assert operations._source_label("telegram") == "Telegram"
    assert operations._assignee_label(item, user_id=101) == "Вы"
    assert operations._assignee_label(item, user_id=202) == "другой участник команды"
    assert operations._assignee_label(_lead(assigned_user_id=None), user_id=101) == "не назначен"

    attribution = operations._attribution_line(item)
    assert "Яндекс Директ" in attribution
    assert "creative_variant: creative-42" in attribution
    assert "кампания: campaign-7" in attribution
    fallback = dict(item)
    fallback.update(
        attribution_source=None,
        attribution_source_ref_type=None,
        attribution_source_ref_id="landing-main",
        attribution_promotion_campaign_id=None,
    )
    assert operations._attribution_line(fallback) == "Сайт · landing-main"

    text = operations._item_text(item, user_id=101)
    assert "Ответственный: Вы" in text
    assert "Следующее действие: Позвонить клиенту" in text
    assert "Метка источника: landing-main" in text

    open_labels = _labels(
        operations._detail_keyboard(business_id, item, user_id=101)
    )
    assert "Снять ответственного" in open_labels
    assert "⏱ +1 час" in open_labels
    assert "✅ Выиграно" in open_labels

    unassigned_labels = _labels(
        operations._detail_keyboard(
            business_id,
            _lead(assigned_user_id=None, next_action=None),
            user_id=101,
        )
    )
    assert "🙋 Назначить меня" in unassigned_labels
    assert "⏱ +1 час" not in unassigned_labels

    lost_labels = _labels(
        operations._detail_keyboard(
            business_id,
            _lead(stage="lost"),
            user_id=101,
        )
    )
    assert "↩️ Вернуть в работу" in lost_labels
    assert "Причина закрытия: нет бюджета" in operations._item_text(
        _lead(stage="lost"),
        user_id=101,
    )

    won_labels = _labels(
        operations._detail_keyboard(
            business_id,
            _lead(stage="won"),
            user_id=101,
        )
    )
    assert "📝 Заметка" in won_labels
    assert "↩️ Вернуть в работу" not in won_labels


@pytest.mark.asyncio
async def test_load_detail_manage_and_closed_projections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    open_item = _lead()
    closed_item = _lead(stage="lost")
    monkeypatch.setattr(
        operations,
        "list_sales_work",
        lambda **_kwargs: [open_item],
    )
    monkeypatch.setattr(
        operations,
        "list_recent_closed_sales_work",
        lambda **_kwargs: [closed_item],
    )

    actor = _actor(101, business_id)
    assert await operations._load_item(actor=actor, lead_id=open_item["id"]) == open_item
    assert await operations._load_item(actor=actor, lead_id=closed_item["id"]) == closed_item
    assert await operations._load_item(actor=actor, lead_id=str(uuid4())) is None

    detail = FakeMessage()
    await operations._send_detail(
        detail,
        user_id=101,
        business_id=business_id,
        lead_id=open_item["id"],
    )
    assert "👤 Анна" in detail.answers[-1][0]

    manage = FakeMessage()
    await operations._send_manage_work(
        manage,
        user_id=101,
        business_id=business_id,
    )
    assert "Управлять: 1" in _labels(manage.answers[-1][1]["reply_markup"])
    assert "🧠 Рекомендации и ИИ" in _labels(
        manage.answers[-1][1]["reply_markup"]
    )

    closed = FakeMessage()
    await operations._send_closed_work(
        closed,
        user_id=101,
        business_id=business_id,
    )
    assert "нет бюджета" in closed.answers[-1][0]
    assert "Открыть: 1" in _labels(closed.answers[-1][1]["reply_markup"])

    monkeypatch.setattr(operations, "list_sales_work", lambda **_kwargs: [])
    monkeypatch.setattr(
        operations,
        "list_recent_closed_sales_work",
        lambda **_kwargs: [],
    )
    empty_manage = FakeMessage()
    await operations._send_manage_work(
        empty_manage,
        user_id=101,
        business_id=business_id,
    )
    assert "Активных обращений сейчас нет" in empty_manage.answers[-1][0]

    empty_closed = FakeMessage()
    await operations._send_closed_work(
        empty_closed,
        user_id=101,
        business_id=business_id,
    )
    assert "Закрытых обращений пока нет" in empty_closed.answers[-1][0]

    missing = FakeMessage()
    await operations._send_detail(
        missing,
        user_id=101,
        business_id=business_id,
        lead_id=str(uuid4()),
    )
    assert "больше недоступна" in missing.answers[-1][0]


def test_install_sales_operations_is_idempotent() -> None:
    business_id = str(uuid4())
    module = SimpleNamespace(
        _sales_operations_installed=False,
        _home_keyboard=lambda _business_id: operations.control._keyboard(
            [[("Главная", "x:home")], [("Назад", "x:back")]]
        ),
        control=operations.control,
    )

    operations.install_sales_operations(module)
    first = _labels(module._home_keyboard(business_id))
    operations.install_sales_operations(module)
    second = _labels(module._home_keyboard(business_id))

    assert first == second
    assert first.count("🛠 Управлять обращениями") == 1


@pytest.mark.asyncio
async def test_navigation_callbacks_dispatch_to_canonical_views(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id, lead_id = str(uuid4()), str(uuid4())
    business_token = operations._token(business_id)
    lead_token = operations._token(lead_id)
    state = FakeState({"stale": True})

    manage = AsyncMock()
    closed = AsyncMock()
    detail = AsyncMock()
    monkeypatch.setattr(operations, "_send_manage_work", manage)
    monkeypatch.setattr(operations, "_send_closed_work", closed)
    monkeypatch.setattr(operations, "_send_detail", detail)

    await operations.open_sales_operations(
        FakeCallback(f"cps:swm:{business_token}"),
        state,
    )
    await operations.open_closed_sales(
        FakeCallback(f"cps:swc:{business_token}"),
        state,
    )
    await operations.open_sales_lead(
        FakeCallback(f"cps:swv:{business_token}:{lead_token}"),
        state,
    )

    manage.assert_awaited_once()
    closed.assert_awaited_once()
    detail.assert_awaited_once()
    assert state.clear_count == 3


@pytest.mark.asyncio
async def test_assignment_unassignment_and_clear_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id, lead_id = str(uuid4()), str(uuid4())
    bt, lt = operations._token(business_id), operations._token(lead_id)
    refreshed = AsyncMock()
    monkeypatch.setattr(operations, "_send_detail", refreshed)

    captured: dict[str, dict[str, Any]] = {}

    def assign(**kwargs: Any) -> None:
        captured["assign"] = kwargs

    def unassign(**kwargs: Any) -> None:
        captured["unassign"] = kwargs

    def clear(**kwargs: Any) -> None:
        captured["clear"] = kwargs

    monkeypatch.setattr(operations, "assign_sales_lead", assign)
    monkeypatch.setattr(operations, "unassign_sales_lead", unassign)
    monkeypatch.setattr(operations, "clear_sales_next_action", clear)

    assign_callback = FakeCallback(f"cps:swme:{bt}:{lt}")
    await operations.assign_sales_lead_to_self(assign_callback, FakeState())
    assert captured["assign"]["lead_id"] == lead_id
    assert captured["assign"]["member_id"]

    unassign_callback = FakeCallback(f"cps:swmu:{bt}:{lt}")
    await operations.unassign_sales_lead_owner(unassign_callback, FakeState())
    assert captured["unassign"]["lead_id"] == lead_id

    clear_callback = FakeCallback(f"cps:swmx:{bt}:{lt}")
    await operations.clear_sales_next_action_owner(clear_callback, FakeState())
    assert captured["clear"]["lead_id"] == lead_id
    assert refreshed.await_count == 3

    def fail(**_kwargs: Any) -> None:
        raise ValueError("stale")

    monkeypatch.setattr(operations, "assign_sales_lead", fail)
    failed = FakeCallback(f"cps:swme:{bt}:{lt}")
    await operations.assign_sales_lead_to_self(failed, FakeState())
    assert failed.answers[-1][1]["show_alert"] is True


@pytest.mark.asyncio
async def test_next_action_prompt_validation_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id, lead_id = str(uuid4()), str(uuid4())
    bt, lt = operations._token(business_id), operations._token(lead_id)

    begin_state = FakeState()
    begin = FakeCallback(f"cps:swmn:{bt}:{lt}")
    await operations.begin_sales_next_action(begin, begin_state)
    assert begin_state.data["sales_business_id"] == business_id
    assert begin_state.data["sales_lead_id"] == lead_id
    assert "конкретное следующее действие" in begin.message.answers[-1][0]

    empty = FakeMessage(text="   ")
    await operations.capture_sales_next_action(empty, FakeState())
    assert "не может быть пустым" in empty.answers[-1][0]

    long = FakeMessage(text="x" * 501)
    await operations.capture_sales_next_action(long, FakeState())
    assert "500 символов" in long.answers[-1][0]

    stale = FakeMessage(text="Позвонить")
    stale_state = FakeState({"sales_business_id": business_id})
    await operations.capture_sales_next_action(stale, stale_state)
    assert stale_state.clear_count == 1
    assert "устарела" in stale.answers[-1][0]

    captured: dict[str, Any] = {}

    def save(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(operations, "set_sales_next_action", save)
    monkeypatch.setattr(operations, "_send_detail", AsyncMock())
    success = FakeMessage(text="  Позвонить завтра  ")
    success_state = FakeState(
        {"sales_business_id": business_id, "sales_lead_id": lead_id}
    )
    await operations.capture_sales_next_action(success, success_state)
    assert captured["next_action"] == "Позвонить завтра"
    assert captured["due_at"] is None
    assert success_state.clear_count == 1

    def fail(**_kwargs: Any) -> None:
        raise ValueError("closed")

    monkeypatch.setattr(operations, "set_sales_next_action", fail)
    failed = FakeMessage(text="Ещё раз")
    failed_state = FakeState(
        {"sales_business_id": business_id, "sales_lead_id": lead_id}
    )
    await operations.capture_sales_next_action(failed, failed_state)
    assert failed_state.clear_count == 1
    assert "Не удалось сохранить" in failed.answers[-1][0]


@pytest.mark.asyncio
async def test_due_action_guards_and_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id, lead_id = str(uuid4()), str(uuid4())
    bt, lt = operations._token(business_id), operations._token(lead_id)

    async def missing(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(operations, "_load_item", missing)
    stale = FakeCallback(f"cps:swmd:{bt}:{lt}:24")
    await operations.set_sales_due_owner(stale, FakeState())
    assert stale.answers[-1][1]["show_alert"] is True

    item = _lead()

    async def loaded(**_kwargs: Any) -> dict[str, Any]:
        return item

    monkeypatch.setattr(operations, "_load_item", loaded)
    unknown = FakeCallback(f"cps:swmd:{bt}:{lt}:999")
    await operations.set_sales_due_owner(unknown, FakeState())
    assert "Неизвестный срок" in unknown.answers[-1][0][0]

    captured: list[dict[str, Any]] = []

    def save(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(operations, "set_sales_next_action", save)
    monkeypatch.setattr(operations, "_send_detail", AsyncMock())

    no_due = FakeCallback(f"cps:swmd:{bt}:{lt}:n")
    await operations.set_sales_due_owner(no_due, FakeState())
    assert captured[-1]["due_at"] is None

    tomorrow = FakeCallback(f"cps:swmd:{bt}:{lt}:24")
    await operations.set_sales_due_owner(tomorrow, FakeState())
    assert captured[-1]["due_at"] is not None
    assert captured[-1]["next_action"] == "Позвонить клиенту"

    def fail(**_kwargs: Any) -> None:
        raise ValueError("race")

    monkeypatch.setattr(operations, "set_sales_next_action", fail)
    failed = FakeCallback(f"cps:swmd:{bt}:{lt}:1")
    await operations.set_sales_due_owner(failed, FakeState())
    assert "Не удалось сохранить срок" in failed.answers[-1][0][0]


@pytest.mark.asyncio
async def test_note_prompt_and_capture_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id, lead_id = str(uuid4()), str(uuid4())
    bt, lt = operations._token(business_id), operations._token(lead_id)

    state = FakeState()
    callback = FakeCallback(f"cps:swmo:{bt}:{lt}")
    await operations.begin_sales_note(callback, state)
    assert state.data["sales_lead_id"] == lead_id
    assert "заметку" in callback.message.answers[-1][0]

    stale = FakeMessage(text="Комментарий")
    await operations.capture_sales_note(stale, FakeState())
    assert "устарела" in stale.answers[-1][0]

    empty = FakeMessage(text=" ")
    valid_state = FakeState(
        {"sales_business_id": business_id, "sales_lead_id": lead_id}
    )
    await operations.capture_sales_note(empty, valid_state)
    assert "не может быть пустой" in empty.answers[-1][0]

    captured: dict[str, Any] = {}

    def save(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(operations, "add_sales_note", save)
    monkeypatch.setattr(operations, "_send_detail", AsyncMock())
    message = FakeMessage(text="Перезвонить после 18:00")
    success_state = FakeState(
        {"sales_business_id": business_id, "sales_lead_id": lead_id}
    )
    await operations.capture_sales_note(message, success_state)
    assert captured["note"] == "Перезвонить после 18:00"
    assert captured["dedupe_key"] == "telegram:500:700"
    assert "Заметка сохранена" in message.answers[-1][0]

    def fail(**_kwargs: Any) -> None:
        raise ValueError("duplicate")

    monkeypatch.setattr(operations, "add_sales_note", fail)
    failed = FakeMessage(text="Ещё заметка")
    failed_state = FakeState(
        {"sales_business_id": business_id, "sales_lead_id": lead_id}
    )
    await operations.capture_sales_note(failed, failed_state)
    assert "Не удалось сохранить заметку" in failed.answers[-1][0]


@pytest.mark.asyncio
async def test_stage_close_and_reopen_lifecycle_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id, lead_id = str(uuid4()), str(uuid4())
    bt, lt = operations._token(business_id), operations._token(lead_id)
    monkeypatch.setattr(operations, "_send_detail", AsyncMock())

    invalid_stage = FakeCallback(f"cps:swms:{bt}:{lt}:z")
    await operations.set_sales_stage_owner(invalid_stage, FakeState())
    assert "Неизвестный статус" in invalid_stage.answers[-1][0][0]

    transitions: list[dict[str, Any]] = []

    def transition(**kwargs: Any) -> None:
        transitions.append(kwargs)

    monkeypatch.setattr(operations, "transition_sales_lead", transition)
    contacted = FakeCallback(f"cps:swms:{bt}:{lt}:c")
    contacted_state = FakeState()
    await operations.set_sales_stage_owner(contacted, contacted_state)
    assert transitions[-1]["stage"].value == "contacted"
    assert contacted_state.clear_count == 1

    invalid_close = FakeCallback(f"cps:swmc:{bt}:{lt}:z")
    await operations.begin_close_sales_lead(invalid_close, FakeState())
    assert "Неизвестный результат" in invalid_close.answers[-1][0][0]

    close_state = FakeState()
    close = FakeCallback(f"cps:swmc:{bt}:{lt}:l")
    await operations.begin_close_sales_lead(close, close_state)
    assert close_state.data["sales_close_stage"] == "lost"
    assert "причину результата" in close.message.answers[-1][0]

    empty_reason = FakeMessage(text=" ")
    await operations.capture_close_reason(empty_reason, FakeState())
    assert "Причина не может быть пустой" in empty_reason.answers[-1][0]

    stale_reason = FakeMessage(text="нет бюджета")
    stale_state = FakeState(
        {
            "sales_business_id": business_id,
            "sales_lead_id": lead_id,
            "sales_close_stage": "invalid",
        }
    )
    await operations.capture_close_reason(stale_reason, stale_state)
    assert stale_state.clear_count == 1
    assert "устарела" in stale_reason.answers[-1][0]

    won_message = FakeMessage(text="Оплата получена")
    won_state = FakeState(
        {
            "sales_business_id": business_id,
            "sales_lead_id": lead_id,
            "sales_close_stage": "won",
        }
    )
    await operations.capture_close_reason(won_message, won_state)
    assert transitions[-1]["stage"].value == "won"
    assert transitions[-1]["reason"] == "Оплата получена"
    assert won_state.clear_count == 1

    reopened = FakeCallback(f"cps:swmr:{bt}:{lt}")
    reopened_state = FakeState()
    await operations.reopen_lost_sales_lead(reopened, reopened_state)
    assert transitions[-1]["stage"].value == "new"
    assert transitions[-1]["reason"] == "reopened_by_owner"
    assert reopened_state.clear_count == 1

    def fail(**_kwargs: Any) -> None:
        raise ValueError("stale")

    monkeypatch.setattr(operations, "transition_sales_lead", fail)
    failed_stage = FakeCallback(f"cps:swms:{bt}:{lt}:q")
    await operations.set_sales_stage_owner(failed_stage, FakeState())
    assert failed_stage.answers[-1][1]["show_alert"] is True

    failed_close = FakeMessage(text="Причина")
    failed_close_state = FakeState(
        {
            "sales_business_id": business_id,
            "sales_lead_id": lead_id,
            "sales_close_stage": "lost",
        }
    )
    await operations.capture_close_reason(failed_close, failed_close_state)
    assert failed_close_state.clear_count == 1
    assert "нельзя сохранить" in failed_close.answers[-1][0]

    failed_reopen = FakeCallback(f"cps:swmr:{bt}:{lt}")
    await operations.reopen_lost_sales_lead(failed_reopen, FakeState())
    assert failed_reopen.answers[-1][1]["show_alert"] is True


@pytest.mark.asyncio
async def test_followup_owner_prompt_schedule_and_keyboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id, lead_id = str(uuid4()), str(uuid4())
    bt, lt = operations._token(business_id), operations._token(lead_id)
    item = _lead()
    item.update(
        id=lead_id,
        source_kind="telegram",
        contact_basis="inbound",
        followup_suppressed=False,
        active_followup_id=None,
        active_followup_scheduled_at=None,
    )
    labels = _labels(operations._detail_keyboard(business_id, item, user_id=101))
    assert "✉️ Напомнить клиенту" in labels
    assert "🚫 Клиент просит не писать" in labels

    async def loaded(**_kwargs: Any) -> dict[str, Any]:
        return item

    monkeypatch.setattr(operations, "_load_item", loaded)
    state = FakeState()
    begin = FakeCallback(f"cps:swff:{bt}:{lt}")
    await operations.begin_sales_followup(begin, state)
    assert state.data["sales_business_id"] == business_id
    assert state.data["sales_lead_id"] == lead_id
    assert "исходному каналу" in begin.message.answers[-1][0]

    message = FakeMessage(text="  Анна, добрый день.  Подсказать?  ")
    await operations.capture_sales_followup_text(message, state)
    assert state.data["sales_followup_text"] == "Анна, добрый день. Подсказать?"
    assert "Когда отправить?" in message.answers[-1][0]
    assert "Завтра" in _labels(message.answers[-1][1]["reply_markup"])

    captured: dict[str, Any] = {}

    def schedule(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(scheduled_at="2026-08-21T09:00:00+00:00")

    monkeypatch.setattr(operations, "schedule_sales_followup", schedule)
    monkeypatch.setattr(operations, "_send_detail", AsyncMock())
    callback = FakeCallback(f"cps:swft:{bt}:{lt}:24")
    await operations.schedule_sales_followup_owner(callback, state)
    assert captured["lead_id"] == lead_id
    assert captured["message_text"] == "Анна, добрый день. Подсказать?"
    assert captured["request_key"] == "telegram-callback:callback-1"
    assert callback.answers[-1][0][0] == "Follow-up запланирован"
    assert state.clear_count == 1

    active = dict(item)
    active["active_followup_id"] = str(uuid4())
    active_labels = _labels(
        operations._detail_keyboard(business_id, active, user_id=101)
    )
    assert "✖️ Отменить follow-up" in active_labels
    assert "✉️ Напомнить клиенту" not in active_labels

    suppressed = dict(item)
    suppressed["followup_suppressed"] = True
    suppressed_labels = _labels(
        operations._detail_keyboard(business_id, suppressed, user_id=101)
    )
    assert "✉️ Напомнить клиенту" not in suppressed_labels
    assert "🚫 Клиент просит не писать" not in suppressed_labels


@pytest.mark.asyncio
async def test_followup_cancel_and_opt_out_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id, lead_id = str(uuid4()), str(uuid4())
    bt, lt = operations._token(business_id), operations._token(lead_id)
    monkeypatch.setattr(operations, "_send_detail", AsyncMock())
    calls: dict[str, dict[str, Any]] = {}

    def cancel(**kwargs: Any) -> int:
        calls["cancel"] = kwargs
        return 1

    def suppress(**kwargs: Any) -> int:
        calls["suppress"] = kwargs
        return 1

    monkeypatch.setattr(operations, "cancel_sales_followup", cancel)
    monkeypatch.setattr(operations, "suppress_sales_followup_channel", suppress)

    cancel_callback = FakeCallback(f"cps:swfz:{bt}:{lt}")
    await operations.cancel_sales_followup_owner(cancel_callback, FakeState())
    assert calls["cancel"]["lead_id"] == lead_id
    assert cancel_callback.answers[-1][0][0] == "Follow-up отменён"

    confirm = FakeCallback(f"cps:swfoq:{bt}:{lt}")
    await operations.confirm_sales_followup_opt_out(confirm, FakeState())
    confirm_labels = _labels(confirm.message.answers[-1][1]["reply_markup"])
    assert "Да, больше не писать" in confirm_labels
    assert "Нет, вернуться" in confirm_labels

    apply_callback = FakeCallback(f"cps:swfoc:{bt}:{lt}")
    await operations.apply_sales_followup_opt_out(apply_callback, FakeState())
    assert calls["suppress"]["lead_id"] == lead_id
    assert calls["suppress"]["reason"] == "opt_out"
    assert apply_callback.answers[-1][0][0] == "Запрет на follow-up сохранён"


@pytest.mark.asyncio
async def test_followup_owner_fail_closed_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id, lead_id = str(uuid4()), str(uuid4())
    bt, lt = operations._token(business_id), operations._token(lead_id)
    blocked = _lead()
    blocked.update(
        id=lead_id,
        source_kind="telegram",
        followup_suppressed=False,
        active_followup_id=str(uuid4()),
        active_followup_scheduled_at="2026-08-21T09:00:00+00:00",
    )

    async def loaded(**_kwargs: Any) -> dict[str, Any]:
        return blocked

    monkeypatch.setattr(operations, "_load_item", loaded)
    begin = FakeCallback(f"cps:swff:{bt}:{lt}")
    await operations.begin_sales_followup(begin, FakeState())
    assert begin.answers[-1][1]["show_alert"] is True
    assert "недоступен" in begin.answers[-1][0][0]
    assert "запланирован" in operations._item_text(blocked, user_id=101)

    suppressed = dict(blocked)
    suppressed["active_followup_id"] = None
    suppressed["followup_suppressed"] = True
    assert "не отправлять" in operations._item_text(suppressed, user_id=101)

    empty = FakeMessage(text=" \x00 ")
    await operations.capture_sales_followup_text(empty, FakeState())
    assert "не может быть пустым" in empty.answers[-1][0]

    oversized = FakeMessage(text="x" * 4001)
    await operations.capture_sales_followup_text(oversized, FakeState())
    assert "4000" in oversized.answers[-1][0]

    stale_message = FakeMessage(text="Нормальный текст")
    stale_state = FakeState()
    await operations.capture_sales_followup_text(stale_message, stale_state)
    assert stale_state.clear_count == 1
    assert "устарела" in stale_message.answers[-1][0]

    invalid_state = FakeState(
        {
            "sales_business_id": business_id,
            "sales_lead_id": lead_id,
            "sales_followup_text": "Напоминание",
        }
    )
    invalid_due = FakeCallback(f"cps:swft:{bt}:{lt}:999")
    await operations.schedule_sales_followup_owner(invalid_due, invalid_state)
    assert invalid_state.clear_count == 1
    assert invalid_due.answers[-1][1]["show_alert"] is True

    def fail_schedule(**_kwargs: Any) -> None:
        raise ValueError("stale")

    monkeypatch.setattr(operations, "schedule_sales_followup", fail_schedule)
    failed_state = FakeState(
        {
            "sales_business_id": business_id,
            "sales_lead_id": lead_id,
            "sales_followup_text": "Напоминание",
        }
    )
    failed_schedule = FakeCallback(f"cps:swft:{bt}:{lt}:1")
    await operations.schedule_sales_followup_owner(failed_schedule, failed_state)
    assert failed_state.clear_count == 1
    assert failed_schedule.answers[-1][1]["show_alert"] is True

    def fail_operation(**_kwargs: Any) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(operations, "cancel_sales_followup", fail_operation)
    failed_cancel = FakeCallback(f"cps:swfz:{bt}:{lt}")
    await operations.cancel_sales_followup_owner(failed_cancel, FakeState())
    assert failed_cancel.answers[-1][1]["show_alert"] is True

    monkeypatch.setattr(operations, "suppress_sales_followup_channel", fail_operation)
    failed_opt_out = FakeCallback(f"cps:swfoc:{bt}:{lt}")
    await operations.apply_sales_followup_opt_out(failed_opt_out, FakeState())
    assert failed_opt_out.answers[-1][1]["show_alert"] is True
