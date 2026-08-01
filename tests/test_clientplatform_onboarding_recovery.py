from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

import pytest

from clientplatform.domain.activity import ActivityNotFound
from handlers import clientplatform_entry as entry
from handlers import clientplatform_onboarding_recovery as recovery


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, text: str = "ремонты", user_id: int = 101) -> None:
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class FakeState:
    def __init__(
        self,
        *,
        current_state: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.current_state = current_state
        self.data = dict(data or {})
        self.states: list[Any] = []

    async def get_state(self) -> str | None:
        return self.current_state

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def set_state(self, value: Any) -> None:
        self.states.append(value)
        self.current_state = getattr(value, "state", value)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)


async def direct_to_thread(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    return func(*args, **kwargs)


def access(business_id: str) -> Any:
    return SimpleNamespace(
        business=SimpleNamespace(id=business_id, name="Ремонты"),
    )


@pytest.fixture(autouse=True)
def direct_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery.asyncio, "to_thread", direct_to_thread)


@pytest.mark.asyncio
async def test_plain_repairs_answer_recovers_missing_fsm_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    actor = object()
    monkeypatch.setattr(
        recovery,
        "list_accessible_businesses",
        lambda **_kwargs: [access(business_id)],
    )

    async def fake_actor(_user_id: int, selected_business_id: str) -> object:
        assert selected_business_id == business_id
        return actor

    monkeypatch.setattr(recovery.control, "_actor", fake_actor)
    monkeypatch.setattr(
        recovery,
        "get_business_profile",
        lambda **_kwargs: (_ for _ in ()).throw(ActivityNotFound("missing")),
    )

    result = await recovery.IncompleteActivityDescriptionFilter()(
        FakeMessage("ремонты"),
        FakeState(),
    )

    assert result == {"recovered_business_id": business_id}


@pytest.mark.asyncio
async def test_recovery_does_not_intercept_a_complete_or_other_active_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    calls = 0

    def list_accesses(**_kwargs: Any) -> list[Any]:
        nonlocal calls
        calls += 1
        return [access(business_id)]

    monkeypatch.setattr(recovery, "list_accessible_businesses", list_accesses)

    other_state = FakeState(current_state="ClientPlatformControlState:program_title")
    assert (
        await recovery.IncompleteActivityDescriptionFilter()(
            FakeMessage("не относится к анкете"),
            other_state,
        )
        is False
    )

    complete_context = FakeState(
        current_state=recovery.control.ClientPlatformControlState.activity_description.state,
        data={"business_id": business_id},
    )
    assert (
        await recovery.IncompleteActivityDescriptionFilter()(
            FakeMessage("обычный штатный ответ"),
            complete_context,
        )
        is False
    )
    assert calls == 0


@pytest.mark.asyncio
async def test_recovery_restores_context_and_uses_canonical_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    message = FakeMessage("ремонты")
    state = FakeState()
    observed: list[tuple[str, str, bool]] = []

    async def fake_receive_activity_description(
        received_message: FakeMessage,
        received_state: FakeState,
    ) -> None:
        observed.append(
            (
                received_message.text,
                str(received_state.data["business_id"]),
                bool(received_state.data["editing_activity"]),
            )
        )

    monkeypatch.setattr(
        recovery.control,
        "receive_activity_description",
        fake_receive_activity_description,
    )

    await recovery.recover_activity_description(
        message,
        state,
        recovered_business_id=business_id,
    )

    assert state.states[-1] == recovery.control.ClientPlatformControlState.activity_description
    assert observed == [("ремонты", business_id, False)]


@pytest.mark.asyncio
async def test_unexpected_clientplatform_failure_is_not_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = FakeMessage()

    async def not_handled(_event: object) -> bool:
        return False

    monkeypatch.setattr(entry.control, "clientplatform_control_error", not_handled)
    monkeypatch.setattr(entry, "Message", FakeMessage)

    event = SimpleNamespace(
        exception=RuntimeError("database transport failed"),
        update=SimpleNamespace(message=message, callback_query=None),
    )

    assert await entry.clientplatform_entry_error(event) is True
    assert "/start" in message.answers[-1][0]
    assert "database transport failed" not in message.answers[-1][0]
