from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

import pytest

from clientplatform.domain.programs import EnrollmentStatus, ProgressStatus
from handlers import clientplatform_control as handlers


class FakeUser:
    def __init__(self, user_id: int = 700001) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, data: str, *, user_id: int = 700001) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))


async def direct_to_thread(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def direct_application_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "Message", FakeMessage)
    monkeypatch.setattr(handlers, "CallbackQuery", FakeCallback)
    monkeypatch.setattr(handlers.asyncio, "to_thread", direct_to_thread)


@pytest.mark.asyncio
async def test_customer_program_list_empty_and_populated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    business_token = handlers._uuid_token(business_id)
    empty = FakeCallback(f"cp:cprograms:{business_token}")
    monkeypatch.setattr(handlers, "list_customer_programs", lambda **_kwargs: [])
    await handlers.open_customer_programs(empty)
    assert "пока не выдали" in empty.message.answers[-1][0]

    enrollment_id = str(uuid4())
    monkeypatch.setattr(
        handlers,
        "list_customer_programs",
        lambda **_kwargs: [
            SimpleNamespace(
                program_title="Спокойный сон",
                completed_lessons=1,
                total_lessons=3,
                percent_complete=33,
                enrollment_id=enrollment_id,
            )
        ],
    )
    populated = FakeCallback(f"cp:cprograms:{business_token}")
    await handlers.open_customer_programs(populated)
    text, kwargs = populated.message.answers[-1]
    assert "Спокойный сон — 1/3 (33%)" in text
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == (
        f"cp:cprog:{business_token}:{handlers._uuid_token(enrollment_id)}"
    )


@pytest.mark.asyncio
async def test_customer_program_detail_only_offers_delivered_lesson(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_token = handlers._uuid_token(str(uuid4()))
    enrollment_token = handlers._uuid_token(str(uuid4()))
    delivered = SimpleNamespace(
        progress_status=ProgressStatus.DELIVERED,
        position=1,
        title="Первый урок",
        can_complete=True,
    )
    pending = SimpleNamespace(
        progress_status=ProgressStatus.PENDING,
        position=2,
        title="Второй урок",
        can_complete=False,
    )
    monkeypatch.setattr(
        handlers,
        "get_customer_program",
        lambda **_kwargs: SimpleNamespace(
            summary=SimpleNamespace(
                program_title="Спокойный сон",
                completed_lessons=0,
                total_lessons=2,
                percent_complete=0,
            ),
            lessons=(delivered, pending),
        ),
    )
    callback = FakeCallback(f"cp:cprog:{business_token}:{enrollment_token}")
    await handlers.open_customer_program(callback)
    text, kwargs = callback.message.answers[-1]
    assert "📬 1. Первый урок" in text
    assert "⏳ 2. Второй урок" in text
    buttons = [
        button
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert [button.text for button in buttons].count("Готово · урок 1") == 1
    assert all(button.text != "Готово · урок 2" for button in buttons)

    monkeypatch.setattr(
        handlers,
        "get_customer_program",
        lambda **_kwargs: SimpleNamespace(
            summary=SimpleNamespace(
                program_title="Пустая программа",
                completed_lessons=0,
                total_lessons=0,
                percent_complete=0,
            ),
            lessons=(),
        ),
    )
    empty = FakeCallback(f"cp:cprog:{business_token}:{enrollment_token}")
    await handlers.open_customer_program(empty)
    assert "пока нет материалов" in empty.message.answers[-1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("queued", "status", "expected"),
    [
        (True, EnrollmentStatus.ACTIVE, "поставлен в отправку"),
        (False, EnrollmentStatus.COMPLETED, "Программа завершена"),
        (False, EnrollmentStatus.ACTIVE, "Урок отмечен выполненным"),
    ],
)
async def test_customer_lesson_completion_messages(
    monkeypatch: pytest.MonkeyPatch,
    queued: bool,
    status: EnrollmentStatus,
    expected: str,
) -> None:
    business_token = handlers._uuid_token(str(uuid4()))
    enrollment_token = handlers._uuid_token(str(uuid4()))
    monkeypatch.setattr(
        handlers,
        "complete_customer_lesson",
        lambda **_kwargs: SimpleNamespace(
            next_material_queued=queued,
            program=SimpleNamespace(
                summary=SimpleNamespace(
                    enrollment_status=status,
                    completed_lessons=1,
                    total_lessons=2,
                    percent_complete=50,
                )
            ),
        ),
    )
    callback = FakeCallback(
        f"cp:done:{business_token}:{enrollment_token}:1"
    )
    await handlers.complete_customer_program_lesson(callback)
    assert callback.answers[-1][0] == ("Прогресс сохранён",)
    text, kwargs = callback.message.answers[-1]
    assert expected in text
    assert "1/2 (50%)" in text
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == (
        f"cp:cprog:{business_token}:{enrollment_token}"
    )
