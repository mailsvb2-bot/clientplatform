from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("aiogram")

from clientplatform.domain.activity import BusinessProfileStatus, CapabilityStatus
from clientplatform.domain.business_profile import BusinessProfileDetails
from handlers import clientplatform_control as control
from handlers import clientplatform_first_result as first_result


class _State:
    def __init__(self, data: dict[str, object] | None = None) -> None:
        self.data = dict(data or {})
        self.state = None
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, value):
        self.state = value

    async def clear(self):
        self.cleared = True
        self.state = None
        self.data.clear()


class _Message:
    def __init__(self, *, user_id: int = 101, text: str = "") -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.answers: list[tuple[str, object | None]] = []

    async def answer(self, text: str, reply_markup=None, **_kwargs):
        self.answers.append((text, reply_markup))


class _Callback:
    def __init__(self, *, data: str, message: _Message, user_id: int = 101) -> None:
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[object, object]] = []

    async def answer(self, text=None, show_alert=False, **_kwargs):
        self.answers.append((text, show_alert))


def _profile(*, description: str = "Консультации онлайн", status=BusinessProfileStatus.DRAFT):
    return SimpleNamespace(activity_description=description, status=status)


def _structured(*, confirmed: bool):
    return SimpleNamespace(
        details=BusinessProfileDetails(prices=("5000 ₽",), geo=("Москва",)),
        confirmed=confirmed,
    )


def _button_callbacks(markup) -> set[str]:
    return {
        str(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }


def test_new_activity_stays_draft_and_asks_for_plain_language_confirmation(monkeypatch) -> None:
    business_id = str(uuid4())
    actor = object()
    message = _Message(text="Психолог онлайн. Цена 5000 ₽. Город: Москва")
    state = _State({"business_id": business_id, "editing_activity": False})
    saved_details: list[tuple[BusinessProfileDetails, bool]] = []

    async def fake_actor(_user_id: int, selected_business_id: str):
        assert selected_business_id == business_id
        return actor

    def save_profile(**kwargs):
        assert kwargs["actor"] is actor
        return _profile(description=str(kwargs["activity_description"]))

    def save_details(**kwargs):
        saved_details.append((kwargs["details"], kwargs["reset_confirmation"]))
        return _structured(confirmed=False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("new onboarding must not auto-enable or complete the profile")

    monkeypatch.setattr(control, "_actor", fake_actor)
    monkeypatch.setattr(control, "save_business_profile", save_profile)
    monkeypatch.setattr(control, "save_business_profile_details", save_details)
    monkeypatch.setattr(control, "enable_business_capability", forbidden)
    monkeypatch.setattr(control, "complete_business_profile", forbidden)

    asyncio.run(control.receive_activity_description(message, state))

    assert state.cleared is True
    assert len(saved_details) == 1
    details, reset_confirmation = saved_details[0]
    assert reset_confirmation is True
    assert details.prices == ("5000 ₽",)
    assert details.geo == ("Москва",)
    text, markup = message.answers[-1]
    assert text.startswith("Я правильно понял?")
    assert "API" not in text
    assert "provider" not in text.lower()
    callbacks = _button_callbacks(markup)
    assert any(item.startswith("cp:onboardconfirm:") for item in callbacks)
    assert any(item.startswith("cp:onboardedit:") for item in callbacks)


def test_resume_unconfirmed_draft_returns_to_review(monkeypatch) -> None:
    business_id = str(uuid4())
    actor = object()
    message = _Message()
    state = _State()

    async def fake_actor(_user_id: int, _business_id: str):
        return actor

    monkeypatch.setattr(control, "_actor", fake_actor)
    monkeypatch.setattr(control, "get_business_profile", lambda **_kwargs: _profile())
    monkeypatch.setattr(
        control,
        "get_business_profile_details",
        lambda **_kwargs: _structured(confirmed=False),
    )

    asyncio.run(control._resume_business(message, user_id=101, business_id=business_id, state=state))

    text, markup = message.answers[-1]
    assert text.startswith("Я правильно понял?")
    assert any(
        item.startswith("cp:onboardconfirm:")
        for item in _button_callbacks(markup)
    )


def test_resume_confirmed_draft_returns_to_first_result(monkeypatch) -> None:
    business_id = str(uuid4())
    actor = object()
    message = _Message()
    state = _State()

    async def fake_actor(_user_id: int, _business_id: str):
        return actor

    monkeypatch.setattr(control, "_actor", fake_actor)
    monkeypatch.setattr(control, "get_business_profile", lambda **_kwargs: _profile())
    monkeypatch.setattr(
        control,
        "get_business_profile_details",
        lambda **_kwargs: _structured(confirmed=True),
    )

    asyncio.run(control._resume_business(message, user_id=101, business_id=business_id, state=state))

    text, markup = message.answers[-1]
    assert text.startswith("Что Вы хотите получить первым?")
    callbacks = _button_callbacks(markup)
    assert any(item.startswith("cps:firstbook:") for item in callbacks)
    assert any(item.startswith("cps:firstmat:") for item in callbacks)
    assert any(item.startswith("cp:onboardmore:") for item in callbacks)
    assert not any(item.startswith("cps:firstclient:") for item in callbacks)


def test_confirm_onboarding_is_tenant_checked_before_first_result(monkeypatch) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    actor = object()
    message = _Message()
    callback = _Callback(data=f"cp:onboardconfirm:{token}", message=message)
    state = _State({"untrusted": "state"})
    calls: list[str] = []

    async def fake_actor(_user_id: int, selected_business_id: str):
        assert selected_business_id == business_id
        calls.append("actor")
        return actor

    def confirm(*, actor: object):
        calls.append("confirm")
        return _structured(confirmed=True)

    monkeypatch.setattr(control, "_actor", fake_actor)
    monkeypatch.setattr(control, "_callback_message", lambda _callback: message)
    monkeypatch.setattr(control, "confirm_business_profile_details", confirm)

    asyncio.run(control.confirm_onboarding_profile(callback, state))

    assert calls == ["actor", "confirm"]
    assert callback.answers == [("Подтверждено", False)]
    text, markup = message.answers[-1]
    assert text.startswith("Что Вы хотите получить первым?")
    assert any(item.startswith("cps:firstbook:") for item in _button_callbacks(markup))


def test_first_result_requires_confirmed_draft(monkeypatch) -> None:
    actor = object()
    monkeypatch.setattr(
        first_result.control,
        "get_business_profile",
        lambda **_kwargs: _profile(status=BusinessProfileStatus.DRAFT),
    )
    monkeypatch.setattr(
        first_result,
        "get_business_profile_details",
        lambda **_kwargs: _structured(confirmed=False),
    )

    with pytest.raises(ValueError, match="Сначала подтвердите"):
        asyncio.run(first_result._prepare_first_result(actor, connector_key="programs"))


def test_first_result_activates_only_chosen_capability_then_completes(monkeypatch) -> None:
    actor = object()
    enabled: list[str] = []
    completed: list[object] = []

    monkeypatch.setattr(
        first_result.control,
        "get_business_profile",
        lambda **_kwargs: _profile(status=BusinessProfileStatus.DRAFT),
    )
    monkeypatch.setattr(
        first_result,
        "get_business_profile_details",
        lambda **_kwargs: _structured(confirmed=True),
    )
    monkeypatch.setattr(
        first_result.control,
        "list_business_capabilities",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        first_result.control,
        "enable_business_capability",
        lambda **kwargs: enabled.append(str(kwargs["connector_key"])),
    )
    monkeypatch.setattr(
        first_result.control,
        "complete_business_profile",
        lambda **kwargs: completed.append(kwargs["actor"]),
    )

    asyncio.run(first_result._prepare_first_result(actor, connector_key="programs"))

    assert enabled == ["programs"]
    assert completed == [actor]


def test_ready_business_keeps_existing_first_result_behavior(monkeypatch) -> None:
    actor = object()
    capability = SimpleNamespace(connector_key="programs", status=CapabilityStatus.ACTIVE)
    completed: list[object] = []

    monkeypatch.setattr(
        first_result.control,
        "get_business_profile",
        lambda **_kwargs: _profile(status=BusinessProfileStatus.READY),
    )
    monkeypatch.setattr(
        first_result.control,
        "list_business_capabilities",
        lambda **_kwargs: [capability],
    )
    monkeypatch.setattr(
        first_result,
        "get_business_profile_details",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("READY profile must not need re-confirmation")),
    )
    monkeypatch.setattr(
        first_result.control,
        "complete_business_profile",
        lambda **kwargs: completed.append(kwargs["actor"]),
    )

    asyncio.run(first_result._prepare_first_result(actor, connector_key="programs"))

    assert completed == []
