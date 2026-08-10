from __future__ import annotations

from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    User,
)

from clientplatform.application.customer_role_guard import (
    active_member_business_ids,
    assert_external_customer,
)
from clientplatform.domain.activity import (
    ActivityInvariantViolation,
    BusinessProfileStatus,
    CapabilityStatus,
)
from clientplatform.infrastructure.postgres_safe_activity_repository import ActivityRepository
from handlers import clientplatform_control as control
from handlers import clientplatform_interaction_safety as safety
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


def telegram_message(*, text: str = "текст", user_id: int = 7) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        text=text,
    )


def telegram_callback(*, data: str, user_id: int = 7) -> CallbackQuery:
    message = telegram_message(user_id=user_id)
    assert message.from_user is not None
    return CallbackQuery(
        id=f"callback-{data}-{user_id}",
        from_user=message.from_user,
        chat_instance="chat-instance",
        message=message,
        data=data,
    )


def fsm_context(*, user_id: int = 7) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


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
    assert (
        _callback_conflicts_with_state(
            "ClientPlatformSafetyState:business_name",
            "cps:rename:business",
        )
        is True
    )
    assert (
        _callback_conflicts_with_state(
            "OtherBuilderState:title",
            "cpb:n:business",
        )
        is True
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


@pytest.mark.asyncio
async def test_middleware_serializes_deduplicates_and_blocks_conflicting_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[tuple[str | None, bool]] = []
    edits: list[InlineKeyboardMarkup | None] = []
    handled: list[str] = []

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        *,
        show_alert: bool = False,
        **_kwargs: Any,
    ) -> None:
        answers.append((text, show_alert))

    async def edit_reply_markup(
        _message: Message,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        **_kwargs: Any,
    ) -> None:
        edits.append(reply_markup)

    async def handler(event: Any, _data: dict[str, Any]) -> str:
        handled.append(type(event).__name__)
        return "handled"

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    monkeypatch.setattr(Message, "edit_reply_markup", edit_reply_markup)

    middleware = ClientPlatformInteractionSafetyMiddleware()
    state = fsm_context()
    data = {"bot": SimpleNamespace(id=1), "state": state}
    callback = telegram_callback(data="cp:book:business:slot")

    assert await middleware(handler, callback, data) == "handled"
    assert handled == ["CallbackQuery"]
    assert edits == [None]

    assert await middleware(handler, callback, data) is None
    assert answers[-1] == ("Действие уже выполняется.", False)
    assert handled == ["CallbackQuery"]

    await state.set_state(control.ClientPlatformControlState.activity_description)
    conflict = telegram_callback(data="cpb:n:business")
    assert await middleware(handler, conflict, data) is None
    assert answers[-1] == (
        "Сначала завершите текущий шаг или отправьте /cancel.",
        True,
    )

    message = telegram_message()
    assert await middleware(handler, message, data) == "handled"
    assert handled[-1] == "Message"


@pytest.mark.asyncio
async def test_business_name_prompt_and_receive_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[str] = []
    dashboards: list[str] = []
    message = telegram_message(text="/mybot")
    state = fsm_context()

    async def answer_message(
        _message: Message,
        text: str,
        **_kwargs: Any,
    ) -> None:
        answers.append(text)

    async def actor(_user_id: int, business_id: str) -> object:
        assert business_id == "business-id"
        return object()

    async def send_dashboard(
        _message: Message,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        assert user_id == 7
        dashboards.append(business_id)

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(safety.control, "_actor", actor)
    monkeypatch.setattr(safety.control, "_user_id", lambda _message: 7)
    monkeypatch.setattr(safety.control, "_send_dashboard", send_dashboard)
    monkeypatch.setattr(
        safety,
        "rename_business",
        lambda *, actor, name: SimpleNamespace(name=name),
    )

    await safety._send_business_name_prompt(
        message,
        state=state,
        business_id="business-id",
        repair=True,
    )
    assert "ошибочно сохранилась" in answers[-1]

    await safety.receive_business_rename(message, state)
    assert "не должно быть пустым" in answers[-1]
    assert dashboards == []

    valid = telegram_message(text="Сантехник")
    await safety.receive_business_rename(valid, state)
    assert answers[-1] == "Название обновлено: Сантехник"
    assert dashboards == ["business-id"]
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_begin_and_cancel_business_rename_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_answers: list[str | None] = []
    prompts: list[tuple[str, bool]] = []
    dashboards: list[str] = []
    state = fsm_context()
    callback = telegram_callback(data="cps:rename:token")
    message = callback.message
    assert isinstance(message, Message)

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        **_kwargs: Any,
    ) -> None:
        callback_answers.append(text)

    async def actor(_user_id: int, business_id: str) -> object:
        assert business_id == "business-id"
        return object()

    async def prompt(
        _message: Message,
        *,
        state: FSMContext,
        business_id: str,
        repair: bool,
    ) -> None:
        assert isinstance(state, FSMContext)
        prompts.append((business_id, repair))

    async def dashboard(
        _message: Message,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        assert user_id == 7
        dashboards.append(business_id)

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    monkeypatch.setattr(safety.control, "_token_uuid", lambda _token: "business-id")
    monkeypatch.setattr(safety.control, "_actor", actor)
    monkeypatch.setattr(safety.control, "_callback_message", lambda _callback: message)
    monkeypatch.setattr(safety.control, "_send_dashboard", dashboard)
    monkeypatch.setattr(safety, "_send_business_name_prompt", prompt)

    await safety.begin_business_rename(callback, state)
    assert prompts == [("business-id", False)]
    assert callback_answers[-1] is None

    cancel = telegram_callback(data="cps:cancel:token")
    monkeypatch.setattr(safety.control, "_callback_message", lambda _callback: message)
    await state.set_state(safety.ClientPlatformSafetyState.business_name)
    await safety.cancel_business_rename(cancel, state)
    assert callback_answers[-1] == "Отменено"
    assert dashboards == ["business-id"]
    assert await state.get_state() is None


def test_self_invite_is_blocked_before_claim_mutation() -> None:
    conn = FakeConnection(one=(1,))
    repository = ActivityRepository(conn)

    with pytest.raises(ActivityInvariantViolation, match="собственного бизнеса"):
        repository._assert_invite_claim_is_external(
            token="invite_token_abcdefghijklmnopqrstuvwxyz",
            telegram_user_id=101,
        )

    sql, params = conn.calls[-1]
    assert "JOIN business_members" in sql
    assert "bm.status='active'" in sql
    assert params[0] == 101
    assert len(params[1]) == 64

    ActivityRepository(FakeConnection(one=None))._assert_invite_claim_is_external(
        token="invite_token_abcdefghijklmnopqrstuvwxyz",
        telegram_user_id=102,
    )


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

    assert_external_customer(
        FakeConnection(one=None),
        telegram_user_id=102,
        business_id=business_id,
    )
    tuple_conn = FakeConnection(all_rows=[(business_id,)])
    assert active_member_business_ids(tuple_conn, telegram_user_id=103) == {business_id}


def fake_control_module() -> tuple[ModuleType, dict[str, Any]]:
    module = ModuleType("fake_control")
    runtime: dict[str, Any] = {
        "profile": SimpleNamespace(status=BusinessProfileStatus.DRAFT),
        "capabilities": [],
        "dashboards": [],
        "setups": [],
        "resumes": [],
    }

    def keyboard(
        _business_id: str,
        _capabilities: list[object],
    ) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[])

    async def actor(_user_id: int, _business_id: str) -> object:
        return object()

    async def send_dashboard(
        _message: Message,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        runtime["dashboards"].append((user_id, business_id))

    async def setup(
        _message: Message,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        runtime["setups"].append((user_id, business_id))

    async def resume(
        _message: Message,
        *,
        user_id: int,
        business_id: str,
        state: FSMContext,
    ) -> None:
        assert isinstance(state, FSMContext)
        runtime["resumes"].append((user_id, business_id))

    module._dashboard_keyboard = keyboard
    module._send_dashboard = send_dashboard
    module._resume_business = resume
    module._uuid_token = lambda value: f"token-{value}"
    module._actor = actor
    module._send_capability_setup = setup
    module.get_business_profile = lambda *, actor: runtime["profile"]
    module.list_business_capabilities = lambda *, actor: runtime["capabilities"]
    return module, runtime


@pytest.mark.asyncio
async def test_dashboard_and_resume_guards_cover_repair_and_ready_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Router(name="test-root-runtime")
    module, runtime = fake_control_module()
    install_interaction_safety(root, module)
    message = telegram_message()
    state = fsm_context()

    await module._send_dashboard(
        message,
        user_id=7,
        business_id="business-id",
    )
    assert runtime["setups"] == [(7, "business-id")]
    assert runtime["dashboards"] == []

    runtime["profile"] = SimpleNamespace(status=BusinessProfileStatus.READY)
    runtime["capabilities"] = [SimpleNamespace(status=CapabilityStatus.ACTIVE)]
    await module._send_dashboard(
        message,
        user_id=7,
        business_id="business-id",
    )
    assert runtime["dashboards"] == [(7, "business-id")]

    runtime["profile"] = SimpleNamespace(activity_description="legacy double")
    await module._send_dashboard(
        message,
        user_id=7,
        business_id="business-id",
    )
    assert runtime["dashboards"][-1] == (7, "business-id")

    prompt = AsyncMock()
    monkeypatch.setattr(safety, "_send_business_name_prompt", prompt)
    monkeypatch.setattr(
        safety,
        "list_accessible_businesses",
        lambda *, user_id: [
            SimpleNamespace(
                business=SimpleNamespace(id="business-id", name="/mybot")
            )
        ],
    )
    await module._resume_business(
        message,
        user_id=7,
        business_id="business-id",
        state=state,
    )
    prompt.assert_awaited_once()
    assert runtime["resumes"] == []

    monkeypatch.setattr(
        safety,
        "list_accessible_businesses",
        lambda *, user_id: [
            SimpleNamespace(
                business=SimpleNamespace(id="business-id", name="Сантехник")
            )
        ],
    )
    await module._resume_business(
        message,
        user_id=7,
        business_id="business-id",
        state=state,
    )
    assert runtime["resumes"] == [(7, "business-id")]


def test_interaction_safety_install_is_idempotent_and_adds_rename_entry() -> None:
    root = Router(name="test-root")
    module, _runtime = fake_control_module()

    install_interaction_safety(root, module)
    first_keyboard = module._dashboard_keyboard
    install_interaction_safety(root, module)

    assert module._dashboard_keyboard is first_keyboard
    markup = module._dashboard_keyboard("business", [])
    assert markup.inline_keyboard[-1][0].text == "Изменить название"
    assert markup.inline_keyboard[-1][0].callback_data == "cps:rename:token-business"
    assert module._clientplatform_interaction_safety_installed is True
