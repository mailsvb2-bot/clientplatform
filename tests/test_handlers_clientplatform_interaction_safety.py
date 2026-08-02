from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
from aiogram import Router
from aiogram.types import InlineKeyboardMarkup

from clientplatform.application.customer_role_guard import (
    active_member_business_ids,
    assert_external_customer,
)
from clientplatform.domain.activity import ActivityInvariantViolation
from clientplatform.infrastructure.postgres_safe_activity_repository import ActivityRepository
from handlers.clientplatform_interaction_safety import (
    ClientPlatformInteractionSafetyMiddleware,
    _callback_conflicts_with_state,
    _command_like,
    install_interaction_safety,
)


class FakeCursor:
    def __init__(self, *, one: Any = None, all_rows: list[Any] | None = None) -> None:
        self._one = one
        self._all = list(all_rows or [])

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list[Any]:
        return list(self._all)


class FakeConnection:
    def __init__(self, *, one: Any = None, all_rows: list[Any] | None = None) -> None:
        self.one = one
        self.all_rows = list(all_rows or [])
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> FakeCursor:
        self.calls.append((sql, tuple(params)))
        return FakeCursor(one=self.one, all_rows=self.all_rows)


def test_command_like_values_are_never_valid_profile_fields() -> None:
    assert _command_like("") is True
    assert _command_like("   /mybot") is True
    assert _command_like("/start payload") is True
    assert _command_like("Сантехник") is False


def test_cross_flow_callbacks_are_rejected_while_text_answer_is_pending() -> None:
    assert (
        _callback_conflicts_with_state(
            "ManagedBotSetupState:username",
            "cp:editact:business",
        )
        is True
    )
    assert (
        _callback_conflicts_with_state(
            "ClientPlatformControlState:activity_description",
            "cpb:n:business",
        )
        is True
    )
    assert (
        _callback_conflicts_with_state(
            "ManagedBotSetupState:username",
            "cpb:c:business:request",
        )
        is False
    )
    assert _callback_conflicts_with_state(None, "cp:clients:business") is False


def test_callback_actions_are_deduplicated_per_user_and_payload() -> None:
    middleware = ClientPlatformInteractionSafetyMiddleware()

    assert (
        middleware._is_duplicate_action(bot_id=1, user_id=7, data="cp:client:abc")
        is False
    )
    assert (
        middleware._is_duplicate_action(bot_id=1, user_id=7, data="cp:client:abc")
        is True
    )
    assert (
        middleware._is_duplicate_action(bot_id=1, user_id=8, data="cp:client:abc")
        is False
    )


def test_self_invite_is_blocked_before_claim_mutation() -> None:
    conn = FakeConnection(one=(1,))
    repository = ActivityRepository(conn)

    with pytest.raises(ActivityInvariantViolation, match="собственного бизнеса"):
        repository._assert_invite_claim_is_external(
            token="invite-token",
            telegram_user_id=101,
        )

    sql, params = conn.calls[-1]
    assert "JOIN business_members" in sql
    assert "bm.status='active'" in sql
    assert params[0] == 101
    assert len(params[1]) == 64


def test_existing_self_customer_links_are_hidden_and_rejected() -> None:
    business_id = "9a9b0ad1-01ab-41bf-9f89-7a60ad56d6a3"
    conn = FakeConnection(
        one=(1,),
        all_rows=[{"business_id": business_id}],
    )

    assert active_member_business_ids(conn, telegram_user_id=101) == {business_id}
    with pytest.raises(ValueError, match="другого Telegram-аккаунта"):
        assert_external_customer(
            conn,
            telegram_user_id=101,
            business_id=business_id,
        )


def test_interaction_safety_install_is_idempotent_and_adds_rename_entry() -> None:
    root = Router(name="test-root")
    control = ModuleType("fake_control")

    def keyboard(_business_id: str, _capabilities: list[object]) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[])

    async def send_dashboard(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def resume_business(*_args: Any, **_kwargs: Any) -> None:
        return None

    control._dashboard_keyboard = keyboard
    control._send_dashboard = send_dashboard
    control._resume_business = resume_business
    control._uuid_token = lambda value: f"token-{value}"
    control._actor = lambda *_args, **_kwargs: None
    control._send_capability_setup = lambda *_args, **_kwargs: None

    install_interaction_safety(root, control)
    first_keyboard = control._dashboard_keyboard
    install_interaction_safety(root, control)

    assert control._dashboard_keyboard is first_keyboard
    markup = control._dashboard_keyboard("business", [])
    assert markup.inline_keyboard[-1][0].text == "Изменить название"
    assert markup.inline_keyboard[-1][0].callback_data == "cps:rename:token-business"
    assert control._clientplatform_interaction_safety_installed is True
