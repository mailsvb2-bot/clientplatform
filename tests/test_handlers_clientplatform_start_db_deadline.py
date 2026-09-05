from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from clientplatform.application.cockpit_action_routing import (
    build_cockpit_action_start_payload,
)
from handlers import clientplatform_entry as entry
from services.db import core as db_core


class _StatusMessage:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.deleted = False

    async def edit_text(self, text: str) -> None:
        self.edits.append(text)

    async def delete(self) -> None:
        self.deleted = True


class _Message:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.status = _StatusMessage()

    async def answer(self, text: str, **_kwargs: Any) -> _StatusMessage | None:
        self.answers.append(text)
        if text == "Открываю…":
            return self.status
        return None


class _State:
    pass


@pytest.mark.asyncio
async def test_start_db_deadline_reaches_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _Message()
    observed: list[float | None] = []
    monkeypatch.setattr(entry.control, "_user_id", lambda _message: 42)

    async def dispatch(
        _message: Any,
        _state: Any,
        *,
        user_id: int,
        managed_bot_business_id: str | None,
    ) -> None:
        assert user_id == 42
        assert managed_bot_business_id is None
        observed.append(await asyncio.to_thread(db_core._DB_OPERATION_DEADLINE.get))

    monkeypatch.setattr(entry, "_dispatch_clientplatform_start", dispatch)

    await entry.clientplatform_entry_start(message, _State())

    assert len(observed) == 1
    assert observed[0] is not None
    assert float(observed[0]) > time.monotonic()
    assert message.answers == ["Открываю…"]
    assert message.status.edits == []
    assert message.status.deleted is True


@pytest.mark.asyncio
async def test_internal_db_deadline_uses_safe_start_timeout_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _Message()
    monkeypatch.setattr(entry.control, "_user_id", lambda _message: 42)

    async def dispatch(
        _message: Any,
        _state: Any,
        *,
        user_id: int,
        managed_bot_business_id: str | None,
    ) -> None:
        del user_id, managed_bot_business_id
        raise db_core.DatabaseOperationDeadlineExceeded(
            "database_operation_deadline_exceeded"
        )

    monkeypatch.setattr(entry, "_dispatch_clientplatform_start", dispatch)

    await entry.clientplatform_entry_start(message, _State())

    assert message.answers == ["Открываю…"]
    assert len(message.status.edits) == 1
    assert "отвечает дольше обычного" in message.status.edits[0]
    assert "database_operation_deadline_exceeded" not in message.status.edits[0]
    assert message.status.deleted is False


def test_start_storage_deadline_leaves_response_margin() -> None:
    assert 0 < entry._START_STORAGE_DEADLINE_SECONDS < entry._START_TIMEOUT_SECONDS
    assert (
        entry._START_TIMEOUT_SECONDS - entry._START_STORAGE_DEADLINE_SECONDS
        >= 2.0
    )


class _OwnerMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class _OwnerState:
    def __init__(self) -> None:
        self.cleared = 0
        self.states: list[Any] = []

    async def clear(self) -> None:
        self.cleared += 1

    async def set_state(self, value: Any) -> None:
        self.states.append(value)


@pytest.mark.asyncio
async def test_cpo_start_is_owner_intent_even_when_customer_links_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _OwnerMessage("/start cpo_landing")
    state = _OwnerState()
    monkeypatch.setattr(entry, "list_accessible_businesses", lambda **_kwargs: [])

    def customer_links_must_not_be_read(**_kwargs: Any):
        raise AssertionError("cpo owner start must not route through customer links")

    monkeypatch.setattr(entry, "list_customer_businesses", customer_links_must_not_be_read)

    await entry._dispatch_clientplatform_start(
        message,
        state,
        user_id=42,
        managed_bot_business_id=None,
    )

    assert state.cleared == 1
    assert len(message.answers) == 1
    text, kwargs = message.answers[0]
    assert "управляющий вход" in text
    keyboard = kwargs["reply_markup"]
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "Подключить мой бизнес"
    assert button.callback_data == "business"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_key", "expected_kind", "expected_lead"),
    [
        ("sales_handoff", "handoff", None),
        (
            "sales_plan:55555555-5555-4555-8555-555555555555",
            "work",
            None,
        ),
        (
            "sales_lead:44444444-4444-4444-8444-444444444444",
            "lead",
            "44444444-4444-4444-8444-444444444444",
        ),
    ],
)
async def test_cockpit_action_start_rechecks_alias_and_opens_existing_sales_view(
    monkeypatch: pytest.MonkeyPatch,
    action_key: str,
    expected_kind: str,
    expected_lead: str | None,
) -> None:
    business_id = "11111111-1111-4111-8111-111111111111"
    payload = build_cockpit_action_start_payload(
        business_id=business_id, action_key=action_key
    )
    message = _OwnerMessage(f"/start {payload}")
    state = _OwnerState()
    calls: list[tuple[str, int, str, str | None]] = []

    monkeypatch.setattr(
        entry,
        "resolve_cockpit_context",
        lambda **_kwargs: SimpleNamespace(
            onboarding_required=False,
            business_id=business_id,
            user_id=909,
        ),
    )

    async def handoff(_message: Any, *, user_id: int, business_id: str) -> None:
        calls.append(("handoff", user_id, business_id, None))

    async def work(_message: Any, *, user_id: int, business_id: str) -> None:
        calls.append(("work", user_id, business_id, None))

    async def lead(
        _message: Any, *, user_id: int, business_id: str, lead_id: str
    ) -> None:
        calls.append(("lead", user_id, business_id, lead_id))

    sales = SimpleNamespace(
        send_sales_handoff_view=handoff,
        send_sales_work_view=work,
    )
    operations = SimpleNamespace(send_sales_lead_view=lead)
    monkeypatch.setattr(
        entry.importlib,
        "import_module",
        lambda name, _package=None: operations
        if name == ".clientplatform_sales_operations"
        else sales,
    )

    await entry._dispatch_clientplatform_start(
        message, state, user_id=42, managed_bot_business_id=None
    )

    assert state.cleared == 1
    assert calls == [(expected_kind, 909, business_id, expected_lead)]
    assert message.answers == []


@pytest.mark.asyncio
async def test_cockpit_action_start_fails_closed_after_membership_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = "11111111-1111-4111-8111-111111111111"
    payload = build_cockpit_action_start_payload(
        business_id=business_id, action_key="sales_handoff"
    )
    message = _OwnerMessage(f"/start {payload}")
    state = _OwnerState()
    monkeypatch.setattr(
        entry,
        "resolve_cockpit_context",
        lambda **_kwargs: (_ for _ in ()).throw(
            entry.TenantAccessDenied("membership revoked")
        ),
    )

    def no_route_import(*_args: Any, **_kwargs: Any):
        raise AssertionError("revoked membership must stop before sales presentation")

    monkeypatch.setattr(entry.importlib, "import_module", no_route_import)

    await entry._dispatch_clientplatform_start(
        message, state, user_id=42, managed_bot_business_id=None
    )

    assert state.cleared == 1
    assert len(message.answers) == 1
    assert "Доступ или следующий шаг изменился" in message.answers[0][0]


@pytest.mark.asyncio
async def test_malformed_cockpit_action_start_does_not_fall_back_to_owner_landing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _OwnerMessage("/start cpo_c_invalid")
    state = _OwnerState()

    def no_access_lookup(**_kwargs: Any):
        raise AssertionError("malformed action route must not become generic owner entry")

    monkeypatch.setattr(entry, "list_accessible_businesses", no_access_lookup)
    await entry._dispatch_clientplatform_start(
        message, state, user_id=42, managed_bot_business_id=None
    )

    assert state.cleared == 1
    assert len(message.answers) == 1
    assert "устарела или повреждена" in message.answers[0][0]


@pytest.mark.asyncio
async def test_owner_business_button_starts_business_name_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _OwnerState()
    message = _OwnerMessage("")

    class Callback:
        async def answer(self) -> None:
            return None

    monkeypatch.setattr(entry.control, "_callback_message", lambda _callback: message)

    await entry.clientplatform_owner_business_start(Callback(), state)

    assert state.cleared == 1
    assert state.states == [entry.control.ClientPlatformControlState.business_name]
    assert len(message.answers) == 1
    assert "Как называется Ваше дело" in message.answers[0][0]
