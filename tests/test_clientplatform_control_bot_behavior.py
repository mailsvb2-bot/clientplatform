from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

import pytest

from clientplatform.domain.activity import (
    ActivityInvariantViolation,
    ActivityNotFound,
    CapabilityStatus,
)
from clientplatform.domain.bookings import BookingInvariantViolation, BookingSlotStatus
from clientplatform.domain.programs import ContentKind
from handlers import clientplatform_control as handlers


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id
        self.username = "owner"
        self.full_name = "Owner Name"


class FakeMessage:
    def __init__(
        self,
        *,
        user_id: int | None = 101,
        text: str | None = None,
        bot: Any = None,
    ) -> None:
        self.from_user = FakeUser(user_id) if user_id is not None else None
        self.text = text
        self.bot = bot
        self.audio = None
        self.voice = None
        self.video = None
        self.document = None
        self.photo: list[Any] = []
        self.answers: list[tuple[str, dict[str, Any]]] = []
        self.documents: list[tuple[Any, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))

    async def answer_document(self, document: Any, **kwargs: Any) -> None:
        self.documents.append((document, kwargs))


class FakeBot:
    def __init__(self, *, bot_id: int = 900001, username: str | None = "clientplatform_bot") -> None:
        self.id = bot_id
        self._username = username

    async def get_me(self) -> Any:
        return SimpleNamespace(username=self._username)


class FakeCallback:
    def __init__(
        self,
        data: str,
        *,
        user_id: int = 101,
        message: Any | None = None,
        bot: Any | None = None,
    ) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message or FakeMessage(user_id=user_id)
        self.bot = bot or FakeBot()
        self.answers: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))


class FakeState:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = dict(data or {})
        self.states: list[Any] = []
        self.clear_count = 0

    async def set_state(self, value: Any) -> None:
        self.states.append(value)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def clear(self) -> None:
        self.clear_count += 1
        self.data.clear()


async def direct_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def patch_runtime_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "Message", FakeMessage)
    monkeypatch.setattr(handlers, "CallbackQuery", FakeCallback)
    monkeypatch.setattr(handlers.asyncio, "to_thread", direct_to_thread)


def business_access(business_id: str, name: str = "Практика") -> Any:
    return SimpleNamespace(business=SimpleNamespace(id=business_id, name=name))


def capability(
    connector_key: str,
    *,
    title: str | None = None,
    status: CapabilityStatus = CapabilityStatus.ACTIVE,
    capability_id: str | None = None,
) -> Any:
    return SimpleNamespace(
        id=capability_id or str(uuid4()),
        connector_key=connector_key,
        title=title or connector_key.title(),
        status=status,
    )


def test_filter_and_message_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "control_bot_enabled", lambda: True)
    message = FakeMessage(text="/start payload value")
    assert handlers._user_id(message) == 101
    assert handlers._start_payload(message) == "payload value"
    assert handlers._start_payload(FakeMessage(text="/start")) == ""
    assert handlers._callback_message(FakeCallback("x", message=message)) is message

    with pytest.raises(ValueError, match="Telegram user"):
        handlers._user_id(FakeMessage(user_id=None))
    with pytest.raises(ValueError, match="accessible message"):
        handlers._callback_message(FakeCallback("x", message=object()))


@pytest.mark.asyncio
async def test_control_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "control_bot_enabled", lambda: True)
    assert await handlers.ClientPlatformControlEnabled()(object()) is True
    monkeypatch.setattr(handlers, "control_bot_enabled", lambda: False)
    assert await handlers.ClientPlatformControlEnabled()(object()) is False


def test_keyboard_builders_and_content_detection() -> None:
    business_id = str(uuid4())
    access = business_access(business_id, "Моя практика")
    choice = handlers._business_choice_keyboard([access])
    assert choice.inline_keyboard[0][0].text == "Моя практика"
    assert choice.inline_keyboard[0][0].callback_data.startswith("cp:business:")

    client_choice = handlers._client_business_keyboard(
        [SimpleNamespace(business_id=business_id, business_name="Моя практика")]
    )
    assert client_choice.inline_keyboard[0][0].callback_data.startswith("cp:client:")
    client_portal = handlers._client_portal_keyboard(business_id)
    assert client_portal.inline_keyboard[0][0].text == "Мои программы"
    assert client_portal.inline_keyboard[0][0].callback_data.startswith("cp:cprograms:")
    assert client_portal.inline_keyboard[1][0].text == "Посмотреть доступную запись"

    setup = handlers._capability_setup_keyboard(business_id, {"programs", "services"})
    labels = [row[0].text for row in setup.inline_keyboard]
    assert labels[0].startswith("✅")
    assert labels[1].startswith("➕")
    assert labels[2].startswith("✅")
    assert labels[-1] == "Готово"

    dashboard = handlers._dashboard_keyboard(
        business_id,
        [
            capability("programs", status=CapabilityStatus.ACTIVE),
            capability("services", status=CapabilityStatus.DISABLED),
        ],
    )
    flat = [button.text for row in dashboard.inline_keyboard for button in row]
    assert "Programs" in flat
    assert "Services" not in flat
    assert "Клиенты" in flat
    assert "Результаты" in flat

    media = FakeMessage(text="  текст урока  ")
    media.audio = SimpleNamespace(file_id="audio-id")
    assert handlers._message_content(media) == (ContentKind.AUDIO, "audio-id")
    media.audio = None
    media.voice = SimpleNamespace(file_id="voice-id")
    assert handlers._message_content(media) == (ContentKind.AUDIO, "voice-id")
    media.voice = None
    media.video = SimpleNamespace(file_id="video-id")
    assert handlers._message_content(media) == (ContentKind.VIDEO, "video-id")
    media.video = None
    media.document = SimpleNamespace(file_id="doc-id")
    assert handlers._message_content(media) == (ContentKind.DOCUMENT, "doc-id")
    media.document = None
    media.photo = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")]
    assert handlers._message_content(media) == (ContentKind.IMAGE, "large")
    media.photo = []
    assert handlers._message_content(media) == (ContentKind.TEXT, "текст урока")
    with pytest.raises(ValueError, match="поддерживаются"):
        handlers._message_content(FakeMessage(text=""))


@pytest.mark.asyncio
async def test_setup_dashboard_and_resume_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    actor = object()

    async def fake_actor(_uid: int, _bid: str) -> object:
        return actor

    monkeypatch.setattr(handlers, "_actor", fake_actor)
    monkeypatch.setattr(
        handlers,
        "list_business_capabilities",
        lambda **_kwargs: [
            capability("programs", status=CapabilityStatus.ACTIVE),
            capability("services", status=CapabilityStatus.DISABLED),
        ],
    )
    message = FakeMessage()
    await handlers._send_capability_setup(message, user_id=101, business_id=business_id)
    assert "Можно выбрать несколько" in message.answers[-1][0]

    monkeypatch.setattr(
        handlers,
        "get_business_profile",
        lambda **_kwargs: SimpleNamespace(activity_description="Консультирую родителей"),
    )
    monkeypatch.setattr(
        handlers,
        "list_accessible_businesses",
        lambda **_kwargs: [business_access(business_id, "Семейная практика")],
    )
    monkeypatch.setattr(handlers, "list_customers", lambda **_kwargs: [])
    monkeypatch.setattr(handlers, "list_programs", lambda **_kwargs: [])
    monkeypatch.setattr(handlers, "list_booking_slots", lambda **_kwargs: [])
    dashboard = FakeMessage()
    await handlers._send_dashboard(dashboard, user_id=101, business_id=business_id)
    assert "Семейная практика" in dashboard.answers[-1][0]
    assert "Консультирую родителей" in dashboard.answers[-1][0]

    state = FakeState()
    monkeypatch.setattr(
        handlers,
        "get_business_profile",
        lambda **_kwargs: (_ for _ in ()).throw(ActivityNotFound("missing")),
    )
    missing = FakeMessage()
    await handlers._resume_business(missing, user_id=101, business_id=business_id, state=state)
    assert state.states[-1] == handlers.ClientPlatformControlState.activity_description
    assert state.data["business_id"] == business_id
    assert "Расскажите своими словами" in missing.answers[-1][0]

    sent: list[tuple[int, str]] = []

    async def fake_dashboard(_message: Any, *, user_id: int, business_id: str) -> None:
        sent.append((user_id, business_id))

    monkeypatch.setattr(handlers, "get_business_profile", lambda **_kwargs: object())
    monkeypatch.setattr(handlers, "_send_dashboard", fake_dashboard)
    ready = FakeMessage()
    state = FakeState({"old": True})
    await handlers._resume_business(ready, user_id=101, business_id=business_id, state=state)
    assert state.clear_count == 1
    assert sent == [(101, business_id)]


@pytest.mark.asyncio
async def test_start_invite_new_multi_and_single_business(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    state = FakeState()
    monkeypatch.setattr(
        handlers,
        "claim_customer_invite",
        lambda **_kwargs: SimpleNamespace(
            business_id=business_id,
            business_name="Практика",
            already_connected=False,
        ),
    )
    invited = FakeMessage(text="/start cpj_secret-token")
    await handlers.clientplatform_start(invited, state)
    assert "Подключение завершено" in invited.answers[-1][0]
    assert state.clear_count == 1

    monkeypatch.setattr(
        handlers,
        "claim_customer_invite",
        lambda **_kwargs: SimpleNamespace(
            business_id=business_id,
            business_name="Практика",
            already_connected=True,
        ),
    )
    repeated = FakeMessage(text="/start cpj_secret-token")
    await handlers.clientplatform_start(repeated, FakeState())
    assert "уже были подключены" in repeated.answers[-1][0]

    monkeypatch.setattr(handlers, "list_accessible_businesses", lambda **_kwargs: [])
    monkeypatch.setattr(handlers, "list_customer_businesses", lambda **_kwargs: [])
    new_owner = FakeMessage(text="/start")
    new_state = FakeState()
    await handlers.clientplatform_start(new_owner, new_state)
    assert new_state.states[-1] == handlers.ClientPlatformControlState.business_name
    assert "название Вашего дела" in new_owner.answers[-1][0]

    monkeypatch.setattr(
        handlers,
        "list_customer_businesses",
        lambda **_kwargs: [
            SimpleNamespace(
                business_id=business_id,
                business_name="Практика",
                customer_id=str(uuid4()),
            )
        ],
    )
    client = FakeMessage(text="/start")
    client_state = FakeState({"old": True})
    await handlers.clientplatform_start(client, client_state)
    assert client_state.clear_count == 1
    assert "Вы подключены" in client.answers[-1][0]

    accesses = [business_access(str(uuid4()), "Первый"), business_access(str(uuid4()), "Второй")]
    monkeypatch.setattr(handlers, "list_accessible_businesses", lambda **_kwargs: accesses)
    multiple = FakeMessage(text="/start")
    multi_state = FakeState({"old": 1})
    await handlers.clientplatform_start(multiple, multi_state)
    assert multi_state.clear_count == 1
    assert "Выберите бизнес" in multiple.answers[-1][0]
    assert len(multiple.answers[-1][1]["reply_markup"].inline_keyboard) == 2

    one = [business_access(business_id)]
    monkeypatch.setattr(handlers, "list_accessible_businesses", lambda **_kwargs: one)
    resumed: list[str] = []

    async def fake_resume(_message: Any, **kwargs: Any) -> None:
        resumed.append(kwargs["business_id"])

    monkeypatch.setattr(handlers, "_resume_business", fake_resume)
    await handlers.clientplatform_start(FakeMessage(text="/start"), FakeState())
    assert resumed == [business_id]


@pytest.mark.asyncio
async def test_business_and_activity_input_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    monkeypatch.setattr(
        handlers,
        "create_business",
        lambda **_kwargs: business_access(business_id),
    )
    state = FakeState()
    message = FakeMessage(text="Моя практика")
    await handlers.receive_business_name(message, state)
    assert state.states[-1] == handlers.ClientPlatformControlState.activity_description
    assert state.data == {"business_id": business_id, "editing_activity": False}
    assert "готового списка профессий нет" in message.answers[-1][0]

    async def fake_actor(_uid: int, _bid: str) -> object:
        return object()

    monkeypatch.setattr(handlers, "_actor", fake_actor)
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(handlers, "save_business_profile", lambda **kwargs: saved.append(kwargs))
    enabled: list[str] = []
    completed: list[object] = []
    monkeypatch.setattr(
        handlers,
        "enable_business_capability",
        lambda **kwargs: enabled.append(kwargs["connector_key"]),
    )
    monkeypatch.setattr(
        handlers,
        "complete_business_profile",
        lambda **kwargs: completed.append(kwargs["actor"]),
    )
    dashboard_calls: list[str] = []

    async def fake_dashboard(_message: Any, **kwargs: Any) -> None:
        dashboard_calls.append(kwargs["business_id"])

    monkeypatch.setattr(handlers, "_send_dashboard", fake_dashboard)
    state = FakeState({"business_id": business_id, "editing_activity": False})
    activity = FakeMessage(text="Консультирую и провожу занятия")
    await handlers.receive_activity_description(activity, state)
    assert saved[0]["activity_description"] == "Консультирую и провожу занятия"
    assert enabled == ["programs", "consultations", "services"]
    assert completed == [saved[0]["actor"]]
    assert dashboard_calls == [business_id]
    assert "Всё готово" in activity.answers[-1][0]
    assert state.clear_count == 1

    dashboard_calls.clear()

    editing = FakeMessage(text="Ремонтирую автомобили")
    edit_state = FakeState({"business_id": business_id, "editing_activity": True})
    await handlers.receive_activity_description(editing, edit_state)
    assert "Описание деятельности обновлено" in editing.answers[-1][0]
    assert dashboard_calls == [business_id]


@pytest.mark.asyncio
async def test_business_choice_and_capability_toggle_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    token = handlers._uuid_token(business_id)
    resumed: list[str] = []

    async def fake_resume(_message: Any, **kwargs: Any) -> None:
        resumed.append(kwargs["business_id"])

    monkeypatch.setattr(handlers, "_resume_business", fake_resume)
    await handlers.choose_business(FakeCallback(f"cp:business:{token}"), FakeState())
    assert resumed == [business_id]

    async def fake_actor(_uid: int, _bid: str) -> object:
        return object()

    monkeypatch.setattr(handlers, "_actor", fake_actor)
    setup_calls: list[str] = []

    async def fake_setup(_message: Any, **kwargs: Any) -> None:
        setup_calls.append(kwargs["business_id"])

    monkeypatch.setattr(handlers, "_send_capability_setup", fake_setup)
    disabled: list[str] = []
    monkeypatch.setattr(
        handlers,
        "list_business_capabilities",
        lambda **_kwargs: [capability("programs", status=CapabilityStatus.ACTIVE)],
    )
    import clientplatform.application.activity as activity_app

    monkeypatch.setattr(
        activity_app,
        "disable_business_capability",
        lambda **kwargs: disabled.append(kwargs["connector_key"]),
    )
    active_cb = FakeCallback(f"cp:toggle:{token}:programs")
    await handlers.toggle_capability(active_cb, FakeState())
    assert disabled == ["programs"]
    assert setup_calls[-1] == business_id

    monkeypatch.setattr(handlers, "list_business_capabilities", lambda **_kwargs: [])
    custom_state = FakeState()
    custom_cb = FakeCallback(f"cp:toggle:{token}:custom")
    await handlers.toggle_capability(custom_cb, custom_state)
    assert custom_state.states[-1] == handlers.ClientPlatformControlState.custom_capability_title
    assert "дополнительный формат" in custom_cb.message.answers[-1][0]

    enabled: list[str] = []
    monkeypatch.setattr(
        handlers,
        "enable_business_capability",
        lambda **kwargs: enabled.append(kwargs["connector_key"]),
    )
    service_cb = FakeCallback(f"cp:toggle:{token}:services")
    await handlers.toggle_capability(service_cb, FakeState())
    assert enabled == ["services"]


@pytest.mark.asyncio
async def test_custom_finish_and_edit_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    token = handlers._uuid_token(business_id)

    async def fake_actor(_uid: int, _bid: str) -> object:
        return object()

    monkeypatch.setattr(handlers, "_actor", fake_actor)
    enabled: list[dict[str, Any]] = []
    monkeypatch.setattr(handlers, "enable_business_capability", lambda **kwargs: enabled.append(kwargs))

    async def no_op_setup(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(handlers, "_send_capability_setup", no_op_setup)
    state = FakeState({"business_id": business_id})
    await handlers.receive_custom_capability_title(FakeMessage(text="Диагностика"), state)
    assert enabled[0]["connector_key"] == "custom"
    assert enabled[0]["title"] == "Диагностика"

    monkeypatch.setattr(
        handlers,
        "complete_business_profile",
        lambda **_kwargs: (_ for _ in ()).throw(ActivityInvariantViolation("выберите формат")),
    )
    failed = FakeCallback(f"cp:finish:{token}")
    await handlers.finish_profile(failed, FakeState())
    assert failed.answers[-1][1]["show_alert"] is True

    monkeypatch.setattr(handlers, "complete_business_profile", lambda **_kwargs: object())
    dashboard_calls: list[str] = []

    async def fake_dashboard(_message: Any, **kwargs: Any) -> None:
        dashboard_calls.append(kwargs["business_id"])

    monkeypatch.setattr(handlers, "_send_dashboard", fake_dashboard)
    state = FakeState({"old": 1})
    done = FakeCallback(f"cp:finish:{token}")
    await handlers.finish_profile(done, state)
    assert state.clear_count == 1
    assert dashboard_calls == [business_id]

    edit_state = FakeState()
    edit = FakeCallback(f"cp:editact:{token}")
    await handlers.edit_activity(edit, edit_state)
    assert edit_state.states[-1] == handlers.ClientPlatformControlState.activity_description
    assert edit_state.data == {"business_id": business_id, "editing_activity": True}
    assert "новое описание" in edit.message.answers[-1][0]


@pytest.mark.asyncio
async def test_open_capability_program_and_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    business_token = handlers._uuid_token(business_id)

    async def fake_actor(_uid: int, _bid: str) -> object:
        return object()

    monkeypatch.setattr(handlers, "_actor", fake_actor)
    programs_cap = capability("programs", title="Программы")
    monkeypatch.setattr(handlers, "list_business_capabilities", lambda **_kwargs: [programs_cap])
    monkeypatch.setattr(
        handlers,
        "list_programs",
        lambda **_kwargs: [SimpleNamespace(id=str(uuid4()), title="Спокойный сон")],
    )
    state = FakeState()
    cb = FakeCallback(f"cp:cap:{business_token}:programs")
    await handlers.open_capability(cb, state)
    assert state.data["capability_id"] == programs_cap.id
    assert "Спокойный сон" in cb.message.answers[-1][0]
    buttons = [
        button.text
        for row in cb.message.answers[-1][1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert buttons == ["Создать программу", "Выдать клиенту"]

    consultations = capability("consultations", title="Консультации")
    monkeypatch.setattr(handlers, "list_business_capabilities", lambda **_kwargs: [consultations])
    monkeypatch.setattr(
        handlers,
        "list_business_offerings",
        lambda **_kwargs: [
            SimpleNamespace(id=str(uuid4()), title="Разбор", description="60 минут")
        ],
    )
    monkeypatch.setattr(handlers, "list_booking_slots", lambda **_kwargs: [])
    generic = FakeCallback(f"cp:cap:{business_token}:consultations")
    await handlers.open_capability(generic, FakeState())
    assert "Разбор — 60 минут" in generic.message.answers[-1][0]

    monkeypatch.setattr(handlers, "list_business_offerings", lambda **_kwargs: [])
    empty = FakeCallback(f"cp:cap:{business_token}:consultations")
    await handlers.open_capability(empty, FakeState())
    assert "Пока нет добавленных" in empty.message.answers[-1][0]


@pytest.mark.asyncio
async def test_offering_and_program_creation_flows(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    capability_id = str(uuid4())
    business_token = handlers._uuid_token(business_id)
    capability_token = handlers._uuid_token(capability_id)

    state = FakeState()
    callback = FakeCallback(f"cp:offeradd:{business_token}:{capability_token}")
    await handlers.start_offering(callback, state)
    assert state.states[-1] == handlers.ClientPlatformControlState.offering_title
    assert state.data == {"business_id": business_id, "capability_id": capability_id}

    title_state = FakeState({"business_id": business_id, "capability_id": capability_id})
    await handlers.receive_offering_title(FakeMessage(text="Первая консультация"), title_state)
    assert title_state.data["offering_title"] == "Первая консультация"
    assert title_state.states[-1] == handlers.ClientPlatformControlState.offering_description

    async def fake_actor(_uid: int, _bid: str) -> object:
        return object()

    monkeypatch.setattr(handlers, "_actor", fake_actor)
    monkeypatch.setattr(
        handlers,
        "create_business_offering",
        lambda **kwargs: SimpleNamespace(title=kwargs["title"]),
    )
    dashboard_calls: list[str] = []

    async def fake_dashboard(_message: Any, **kwargs: Any) -> None:
        dashboard_calls.append(kwargs["business_id"])

    monkeypatch.setattr(handlers, "_send_dashboard", fake_dashboard)
    description_state = FakeState(
        {
            "business_id": business_id,
            "capability_id": capability_id,
            "offering_title": "Первая консультация",
        }
    )
    message = FakeMessage(text="60 минут и план действий")
    await handlers.receive_offering_description(message, description_state)
    assert "Добавлено: Первая консультация" in message.answers[-1][0]
    assert dashboard_calls == [business_id]

    program_state = FakeState()
    start = FakeCallback(f"cp:progadd:{business_token}")
    await handlers.start_program(start, program_state)
    assert program_state.states[-1] == handlers.ClientPlatformControlState.program_title
    assert program_state.data["business_id"] == business_id

    await handlers.receive_program_title(FakeMessage(text="Спокойный сон"), program_state)
    assert program_state.data["program_title"] == "Спокойный сон"
    assert program_state.states[-1] == handlers.ClientPlatformControlState.lesson_title
    await handlers.receive_lesson_title(FakeMessage(text="Первое аудио"), program_state)
    assert program_state.data["lesson_title"] == "Первое аудио"
    assert program_state.states[-1] == handlers.ClientPlatformControlState.lesson_content

    monkeypatch.setattr(
        handlers,
        "create_single_lesson_program",
        lambda **kwargs: SimpleNamespace(program=SimpleNamespace(title=kwargs["program_title"])),
    )
    content_state = FakeState(
        {
            "business_id": business_id,
            "program_title": "Спокойный сон",
            "lesson_title": "Первое аудио",
        }
    )
    content = FakeMessage(text="Текст урока")
    await handlers.receive_lesson_content(content, content_state)
    assert "создана и готова" in content.answers[-1][0]
    assert dashboard_calls[-1] == business_id


@pytest.mark.asyncio
async def test_clients_invites_and_delivery_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    business_token = handlers._uuid_token(business_id)

    async def fake_actor(_uid: int, _bid: str) -> object:
        return object()

    monkeypatch.setattr(handlers, "_actor", fake_actor)
    monkeypatch.setattr(
        handlers,
        "tenant_customer_activity",
        lambda **_kwargs: SimpleNamespace(
            total=1,
            new_today=1,
            new_7d=1,
            active_today=1,
            by_platform={"telegram": 1},
            recent=(
                SimpleNamespace(
                    display_name="Иван",
                    username=None,
                    platforms=("telegram",),
                    first_contact_at="2026-08-14T08:00:00+00:00",
                    last_contact_at="2026-08-14T09:00:00+00:00",
                ),
            ),
        ),
    )
    clients = FakeCallback(f"cp:clients:{business_token}")
    await handlers.open_clients(clients)
    assert "• Иван" in clients.message.answers[-1][0]
    assert (
        clients.message.answers[-1][1]["reply_markup"].inline_keyboard[0][0].text
        == "Подключить клиента"
    )

    monkeypatch.setattr(
        handlers,
        "tenant_customer_activity",
        lambda **_kwargs: SimpleNamespace(
            total=0,
            new_today=0,
            new_7d=0,
            active_today=0,
            by_platform={},
            recent=(),
        ),
    )
    empty_clients = FakeCallback(f"cp:clients:{business_token}")
    await handlers.open_clients(empty_clients)
    assert "Пока нет подключённых" in empty_clients.message.answers[-1][0]

    monkeypatch.setattr(
        handlers,
        "issue_customer_invite",
        lambda **_kwargs: SimpleNamespace(token="invite-token"),
    )
    invite = FakeCallback(f"cp:invite:{business_token}", bot=FakeBot(username="cp_test_bot"))
    await handlers.create_invite(invite)
    assert "https://t.me/cp_test_bot?start=cpj_invite-token" in invite.message.answers[-1][0]

    no_username = FakeCallback(f"cp:invite:{business_token}", bot=FakeBot(username=None))
    with pytest.raises(RuntimeError, match="public username"):
        await handlers.create_invite(no_username)

    state = FakeState()
    monkeypatch.setattr(handlers, "list_programs", lambda **_kwargs: [])
    no_programs = FakeCallback(f"cp:deliver:{business_token}")
    await handlers.choose_program_for_delivery(no_programs, state)
    assert "Сначала создайте" in no_programs.message.answers[-1][0]

    program_id = str(uuid4())
    monkeypatch.setattr(
        handlers,
        "list_programs",
        lambda **_kwargs: [SimpleNamespace(id=program_id, title="Спокойный сон")],
    )
    programs = FakeCallback(f"cp:deliver:{business_token}")
    await handlers.choose_program_for_delivery(programs, state)
    assert "Какую программу" in programs.message.answers[-1][0]
    assert (
        programs.message.answers[-1][1]["reply_markup"].inline_keyboard[0][0].text
        == "Спокойный сон"
    )

    program_token = handlers._uuid_token(program_id)
    monkeypatch.setattr(handlers, "list_customers", lambda **_kwargs: [])
    no_customers = FakeCallback(f"cp:sendp:{business_token}:{program_token}")
    await handlers.choose_customer_for_delivery(no_customers, state)
    assert "Сначала подключите клиента" in no_customers.message.answers[-1][0]

    customer_id = str(uuid4())
    monkeypatch.setattr(
        handlers,
        "list_customers",
        lambda **_kwargs: [SimpleNamespace(id=customer_id, display_name=None)],
    )
    customers = FakeCallback(f"cp:sendp:{business_token}:{program_token}")
    await handlers.choose_customer_for_delivery(customers, state)
    assert "Кому выдать" in customers.message.answers[-1][0]
    assert (
        customers.message.answers[-1][1]["reply_markup"].inline_keyboard[0][0].text
        == "Клиент"
    )



@pytest.mark.asyncio
async def test_booking_owner_and_client_journeys(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    offering_id = str(uuid4())
    slot_id = str(uuid4())
    business_token = handlers._uuid_token(business_id)
    offering_token = handlers._uuid_token(offering_id)
    slot_token = handlers._uuid_token(slot_id)

    state = FakeState()
    start = FakeCallback(f"cp:slotadd:{business_token}:{offering_token}")
    await handlers.start_booking_slot(start, state)
    assert state.states[-1] == handlers.ClientPlatformControlState.booking_start
    assert state.data == {"business_id": business_id, "offering_id": offering_id}
    assert "ДД.ММ.ГГГГ" in start.message.answers[-1][0]

    await handlers.receive_booking_start(FakeMessage(text="31.07.2026 15:00"), state)
    assert state.data["booking_start"] == "31.07.2026 15:00"
    assert state.states[-1] == handlers.ClientPlatformControlState.booking_duration

    async def fake_actor(_uid: int, _bid: str) -> object:
        return object()

    monkeypatch.setattr(handlers, "_actor", fake_actor)
    slot = SimpleNamespace(
        slot=SimpleNamespace(
            id=slot_id,
            offering_id=offering_id,
            duration_minutes=60,
            status=BookingSlotStatus.OPEN,
        ),
        offering_title="Первая консультация",
        business_name="Практика",
        local_start="31.07.2026 15:00",
    )
    monkeypatch.setattr(handlers, "create_booking_slot", lambda **_kwargs: slot)
    dashboard_calls: list[str] = []

    async def fake_dashboard(_message: Any, **kwargs: Any) -> None:
        dashboard_calls.append(kwargs["business_id"])

    monkeypatch.setattr(handlers, "_send_dashboard", fake_dashboard)
    duration = FakeMessage(text="60")
    await handlers.receive_booking_duration(duration, state)
    assert "Время опубликовано" in duration.answers[-1][0]
    assert state.clear_count == 1
    assert dashboard_calls == [business_id]

    monkeypatch.setattr(handlers, "list_customer_booking_slots", lambda **_kwargs: [])
    no_slots = FakeCallback(f"cp:client:{business_token}", user_id=700001)
    await handlers.open_client_booking(no_slots)
    assert "свободного времени нет" in no_slots.message.answers[-1][0]

    monkeypatch.setattr(handlers, "list_customer_booking_slots", lambda **_kwargs: [slot])
    available = FakeCallback(f"cp:client:{business_token}", user_id=700001)
    await handlers.open_client_booking(available)
    assert "Первая консультация" in available.message.answers[-1][0]
    button = available.message.answers[-1][1]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == f"cp:book:{business_token}:{slot_token}"

    monkeypatch.setattr(
        handlers,
        "book_customer_slot",
        lambda **_kwargs: SimpleNamespace(slot=slot),
    )
    booked = FakeCallback(f"cp:book:{business_token}:{slot_token}", user_id=700001)
    await handlers.book_client_slot(booked)
    assert "Вы записаны" in booked.message.answers[-1][0]
    assert booked.answers[-1][0] == ("Запись подтверждена",)


@pytest.mark.asyncio
async def test_booking_confirmation_adds_phone_calendar_and_persistent_reminders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    customer_id = str(uuid4())
    slot_id = str(uuid4())
    business_token = handlers._uuid_token(business_id)
    slot_token = handlers._uuid_token(slot_id)
    view = SimpleNamespace(
        slot=SimpleNamespace(
            id=slot_id,
            business_id=business_id,
            starts_at="2026-08-10T13:00:00+00:00",
            ends_at="2026-08-10T14:00:00+00:00",
            duration_minutes=60,
        ),
        offering_title="Вебинар",
        business_name="Школа",
        timezone="Europe/Amsterdam",
        local_start="10.08.2026 15:00",
    )
    claim = SimpleNamespace(slot=view, customer_id=customer_id)
    monkeypatch.setattr(handlers, "book_customer_slot", lambda **_kwargs: claim)
    scheduled: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handlers,
        "schedule_booking_reminders",
        lambda **kwargs: scheduled.append(kwargs),
    )
    callback = FakeCallback(
        f"cp:book:{business_token}:{slot_token}",
        user_id=700001,
    )
    await handlers.book_client_slot(callback)
    assert scheduled[0]["telegram_user_id"] == 700001
    assert callback.message.documents
    _document, kwargs = callback.message.documents[-1]
    assert "напоминаниями за 24 часа" in kwargs["caption"]
    assert kwargs["reply_markup"].inline_keyboard[0][0].url.startswith(
        "https://calendar.google.com/"
    )


@pytest.mark.asyncio
async def test_client_portal_multiple_connections() -> None:
    links = [
        SimpleNamespace(
            business_id=str(uuid4()),
            business_name="Первая практика",
            customer_id=str(uuid4()),
        ),
        SimpleNamespace(
            business_id=str(uuid4()),
            business_name="Вторая практика",
            customer_id=str(uuid4()),
        ),
    ]
    message = FakeMessage(user_id=700001)
    await handlers._send_client_portal(message, links=links)
    assert "Выберите специалиста" in message.answers[-1][0]
    assert len(message.answers[-1][1]["reply_markup"].inline_keyboard) == 2


@pytest.mark.asyncio
async def test_booking_error_is_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    callback = FakeCallback("x")
    handled = await handlers.clientplatform_control_error(
        SimpleNamespace(
            exception=BookingInvariantViolation("время занято"),
            update=SimpleNamespace(message=None, callback_query=callback),
        )
    )
    assert handled is True
    assert callback.answers[-1][1]["show_alert"] is True


@pytest.mark.asyncio
async def test_send_program_results_and_error_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    customer_id = str(uuid4())
    business_token = handlers._uuid_token(business_id)
    customer_token = handlers._uuid_token(customer_id)

    missing = FakeCallback(f"cp:sendc:{business_token}:{customer_token}")
    await handlers.send_program_to_customer(missing, FakeState())
    assert missing.answers[-1][1]["show_alert"] is True

    async def fake_actor(_uid: int, _bid: str) -> object:
        return object()

    monkeypatch.setattr(handlers, "_actor", fake_actor)
    monkeypatch.setattr(
        handlers,
        "prepare_program_delivery",
        lambda **_kwargs: SimpleNamespace(
            program=SimpleNamespace(program=SimpleNamespace(title="Спокойный сон"))
        ),
    )
    state = FakeState({"selected_program_id": str(uuid4())})
    sent = FakeCallback(
        f"cp:sendc:{business_token}:{customer_token}",
        bot=FakeBot(bot_id=55),
    )
    await handlers.send_program_to_customer(sent, state)
    assert "поставлена в очередь" in sent.message.answers[-1][0]

    monkeypatch.setattr(
        handlers,
        "business_delivery_summary",
        lambda **_kwargs: SimpleNamespace(
            customers=2,
            programs=1,
            dispatch_pending=3,
            dispatch_sent=4,
            dispatch_attention=5,
        ),
    )
    monkeypatch.setattr(
        handlers,
        "list_business_program_progress",
        lambda **_kwargs: [
            SimpleNamespace(
                customer_display_name="Анна",
                program_title="Спокойный сон",
                completed_lessons=1,
                total_lessons=2,
                percent_complete=50,
            )
        ],
    )
    results = FakeCallback(f"cp:results:{business_token}")
    await handlers.show_results(results)
    text = results.message.answers[-1][0]
    assert "Клиенты: 2" in text
    assert "Успешно отправлено: 4" in text
    assert "Требуют внимания: 5" in text
    assert "Анна: Спокойный сон — 1/2 (50%)" in text

    assert (
        await handlers.clientplatform_control_error(
            SimpleNamespace(exception=RuntimeError("boom"), update=object())
        )
        is False
    )

    message = FakeMessage()
    handled = await handlers.clientplatform_control_error(
        SimpleNamespace(
            exception=ValueError("bad"),
            update=SimpleNamespace(message=message, callback_query=None),
        )
    )
    assert handled is True
    assert "Не получилось выполнить" in message.answers[-1][0]

    callback = FakeCallback("x")
    handled = await handlers.clientplatform_control_error(
        SimpleNamespace(
            exception=ActivityInvariantViolation("bad"),
            update=SimpleNamespace(message=None, callback_query=callback),
        )
    )
    assert handled is True
    assert callback.answers[-1][1]["show_alert"] is True

    unhandled = await handlers.clientplatform_control_error(
        SimpleNamespace(
            exception=ValueError("bad"),
            update=SimpleNamespace(message=None, callback_query=None),
        )
    )
    assert unhandled is False
