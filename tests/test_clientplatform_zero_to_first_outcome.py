from __future__ import annotations

import asyncio
import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.domain.activity import ActivityNotFound, BusinessProfileStatus, CapabilityStatus
from clientplatform.domain.business_profile import BusinessProfileDetails


_AIOGRAM_AVAILABLE = importlib.util.find_spec("aiogram") is not None
if _AIOGRAM_AVAILABLE:
    from handlers import clientplatform_button_surface_contract as button_surface_contract
    from handlers import clientplatform_control as control
    from handlers import clientplatform_dashboard_dispatch as dashboard_dispatch
    from handlers import clientplatform_first_result as first_result
else:  # pragma: no cover - dependency-light Canon intentionally has no aiogram
    button_surface_contract = None
    control = None
    dashboard_dispatch = None
    first_result = None


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


def _business_access(business_id: str):
    return SimpleNamespace(business=SimpleNamespace(id=business_id, name="Практика"))


def _button_callbacks(markup) -> set[str]:
    return {
        str(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }


@unittest.skipUnless(_AIOGRAM_AVAILABLE, "aiogram is not installed")
class ClientPlatformZeroToFirstOutcomeTests(unittest.TestCase):
    def test_new_activity_stays_draft_and_asks_for_plain_language_confirmation(self) -> None:
        assert control is not None
        business_id = str(uuid4())
        actor = object()
        message = _Message(text="Психолог онлайн. Цена 5000 ₽. Город: Москва")
        state = _State({"business_id": business_id, "editing_activity": False})
        saved_details: list[tuple[BusinessProfileDetails, bool]] = []

        async def fake_actor(_user_id: int, selected_business_id: str):
            self.assertEqual(selected_business_id, business_id)
            return actor

        def save_profile(**kwargs):
            self.assertIs(kwargs["actor"], actor)
            return _profile(description=str(kwargs["activity_description"]))

        def save_details(**kwargs):
            saved_details.append((kwargs["details"], kwargs["reset_confirmation"]))
            return _structured(confirmed=False)

        def forbidden(*_args, **_kwargs):
            raise AssertionError("new onboarding must not auto-enable or complete the profile")

        with (
            patch.object(control, "_actor", fake_actor),
            patch.object(control, "save_business_profile", save_profile),
            patch.object(control, "save_business_profile_details", save_details),
            patch.object(control, "enable_business_capability", forbidden),
            patch.object(control, "complete_business_profile", forbidden),
        ):
            asyncio.run(control.receive_activity_description(message, state))

        self.assertTrue(state.cleared)
        self.assertEqual(len(saved_details), 1)
        details, reset_confirmation = saved_details[0]
        self.assertTrue(reset_confirmation)
        self.assertEqual(details.prices, ("5000 ₽",))
        self.assertEqual(details.geo, ("Москва",))
        text, markup = message.answers[-1]
        self.assertTrue(text.startswith("Я правильно понял?"))
        self.assertNotIn("API", text)
        self.assertNotIn("provider", text.lower())
        callbacks = _button_callbacks(markup)
        self.assertTrue(any(item.startswith("cp:onboardconfirm:") for item in callbacks))
        self.assertTrue(any(item.startswith("cp:onboardedit:") for item in callbacks))

    def test_resume_unconfirmed_draft_returns_to_review(self) -> None:
        assert control is not None
        business_id = str(uuid4())
        actor = object()
        message = _Message()
        state = _State()

        async def fake_actor(_user_id: int, _business_id: str):
            return actor

        with (
            patch.object(control, "_actor", fake_actor),
            patch.object(control, "get_business_profile", lambda **_kwargs: _profile()),
            patch.object(
                control,
                "get_business_profile_details",
                lambda **_kwargs: _structured(confirmed=False),
            ),
            patch.object(
                control,
                "list_accessible_businesses",
                lambda **_kwargs: [_business_access(business_id)],
            ),
        ):
            asyncio.run(
                control._resume_business(
                    message,
                    user_id=101,
                    business_id=business_id,
                    state=state,
                )
            )

        text, markup = message.answers[-1]
        self.assertTrue(text.startswith("Я правильно понял?"))
        self.assertTrue(
            any(item.startswith("cp:onboardconfirm:") for item in _button_callbacks(markup))
        )

    def test_composed_resume_keeps_draft_in_u007_review_after_safety_wrapper(self) -> None:
        assert dashboard_dispatch is not None
        assert button_surface_contract is not None
        business_id = str(uuid4())
        actor = object()
        message = _Message(text="/start")
        state = _State({"stale": "wizard"})
        guarded_calls: list[str] = []
        review_calls: list[str] = []

        async def guarded_resume(*_args, **_kwargs):
            guarded_calls.append("guarded")

        async def fake_actor(_user_id: int, selected_business_id: str):
            self.assertEqual(selected_business_id, business_id)
            return actor

        async def send_review(_message, *, actor: object, business_id: str):
            self.assertIs(actor, actor_ref)
            review_calls.append(business_id)

        actor_ref = actor
        fake_control = SimpleNamespace(
            _resume_business=guarded_resume,
            _actor=fake_actor,
            get_business_profile=lambda **_kwargs: _profile(),
            get_business_profile_details=lambda **_kwargs: _structured(confirmed=False),
            list_accessible_businesses=lambda **_kwargs: [_business_access(business_id)],
            _send_onboarding_review=send_review,
            _send_onboarding_first_result=lambda *_args, **_kwargs: None,
            ActivityNotFound=ActivityNotFound,
        )

        with patch.object(
            button_surface_contract,
            "install_button_surface_contract",
            lambda _module: None,
        ):
            dashboard_dispatch.install_dynamic_dashboard_dispatch(fake_control)
            asyncio.run(
                fake_control._resume_business(
                    message,
                    user_id=101,
                    business_id=business_id,
                    state=state,
                )
            )

        self.assertTrue(state.cleared)
        self.assertEqual(review_calls, [business_id])
        self.assertEqual(guarded_calls, [])

    def test_resume_confirmed_draft_returns_to_first_result(self) -> None:
        assert control is not None
        business_id = str(uuid4())
        actor = object()
        message = _Message()
        state = _State()

        async def fake_actor(_user_id: int, _business_id: str):
            return actor

        with (
            patch.object(control, "_actor", fake_actor),
            patch.object(control, "get_business_profile", lambda **_kwargs: _profile()),
            patch.object(
                control,
                "get_business_profile_details",
                lambda **_kwargs: _structured(confirmed=True),
            ),
            patch.object(
                control,
                "list_accessible_businesses",
                lambda **_kwargs: [_business_access(business_id)],
            ),
        ):
            asyncio.run(
                control._resume_business(
                    message,
                    user_id=101,
                    business_id=business_id,
                    state=state,
                )
            )

        text, markup = message.answers[-1]
        self.assertTrue(text.startswith("Что Вы хотите получить первым?"))
        callbacks = _button_callbacks(markup)
        self.assertTrue(any(item.startswith("cps:firstbook:") for item in callbacks))
        self.assertTrue(any(item.startswith("cps:firstmat:") for item in callbacks))
        self.assertTrue(any(item.startswith("cp:onboardmore:") for item in callbacks))
        self.assertFalse(any(item.startswith("cps:firstclient:") for item in callbacks))

    def test_confirm_onboarding_is_tenant_checked_before_first_result(self) -> None:
        assert control is not None
        business_id = str(uuid4())
        token = control._uuid_token(business_id)
        actor = object()
        message = _Message()
        callback = _Callback(data=f"cp:onboardconfirm:{token}", message=message)
        state = _State({"untrusted": "state"})
        calls: list[str] = []

        async def fake_actor(_user_id: int, selected_business_id: str):
            self.assertEqual(selected_business_id, business_id)
            calls.append("actor")
            return actor

        def confirm(*, actor: object):
            calls.append("confirm")
            return _structured(confirmed=True)

        with (
            patch.object(control, "_actor", fake_actor),
            patch.object(control, "_callback_message", lambda _callback: message),
            patch.object(control, "confirm_business_profile_details", confirm),
        ):
            asyncio.run(control.confirm_onboarding_profile(callback, state))

        self.assertEqual(calls, ["actor", "confirm"])
        self.assertEqual(callback.answers, [("Подтверждено", False)])
        text, markup = message.answers[-1]
        self.assertTrue(text.startswith("Что Вы хотите получить первым?"))
        self.assertTrue(any(item.startswith("cps:firstbook:") for item in _button_callbacks(markup)))

    def test_first_result_requires_confirmed_draft(self) -> None:
        assert first_result is not None
        actor = object()
        with (
            patch.object(
                first_result.control,
                "get_business_profile",
                lambda **_kwargs: _profile(status=BusinessProfileStatus.DRAFT),
            ),
            patch.object(
                first_result,
                "get_business_profile_details",
                lambda **_kwargs: _structured(confirmed=False),
            ),
            self.assertRaisesRegex(ValueError, "Сначала подтвердите"),
        ):
            asyncio.run(first_result._prepare_first_result(actor, connector_key="programs"))

    def test_first_result_activates_only_chosen_capability_then_completes(self) -> None:
        assert first_result is not None
        actor = object()
        enabled: list[str] = []
        completed: list[object] = []

        with (
            patch.object(
                first_result.control,
                "get_business_profile",
                lambda **_kwargs: _profile(status=BusinessProfileStatus.DRAFT),
            ),
            patch.object(
                first_result,
                "get_business_profile_details",
                lambda **_kwargs: _structured(confirmed=True),
            ),
            patch.object(
                first_result.control,
                "list_business_capabilities",
                lambda **_kwargs: [],
            ),
            patch.object(
                first_result.control,
                "enable_business_capability",
                lambda **kwargs: enabled.append(str(kwargs["connector_key"])),
            ),
            patch.object(
                first_result.control,
                "complete_business_profile",
                lambda **kwargs: completed.append(kwargs["actor"]),
            ),
        ):
            asyncio.run(first_result._prepare_first_result(actor, connector_key="programs"))

        self.assertEqual(enabled, ["programs"])
        self.assertEqual(completed, [actor])

    def test_ready_business_keeps_existing_first_result_behavior(self) -> None:
        assert first_result is not None
        actor = object()
        capability = SimpleNamespace(connector_key="programs", status=CapabilityStatus.ACTIVE)
        completed: list[object] = []

        def forbidden_details(**_kwargs):
            raise AssertionError("READY profile must not need re-confirmation")

        with (
            patch.object(
                first_result.control,
                "get_business_profile",
                lambda **_kwargs: _profile(status=BusinessProfileStatus.READY),
            ),
            patch.object(
                first_result.control,
                "list_business_capabilities",
                lambda **_kwargs: [capability],
            ),
            patch.object(first_result, "get_business_profile_details", forbidden_details),
            patch.object(
                first_result.control,
                "complete_business_profile",
                lambda **kwargs: completed.append(kwargs["actor"]),
            ),
        ):
            asyncio.run(first_result._prepare_first_result(actor, connector_key="programs"))

        self.assertEqual(completed, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
