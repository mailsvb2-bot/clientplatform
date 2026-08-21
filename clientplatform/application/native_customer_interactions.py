from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from clientplatform.application.bookings import (
    book_customer_slot_by_customer_in_transaction,
)
from clientplatform.domain.bookings import BookingInvariantViolation, BookingNotFound
from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from clientplatform.domain.customers import (
    CustomerIdentity,
    CustomerIdentityStatus,
)
from clientplatform.domain.messenger_channels import MessengerIngressRoute
from clientplatform.domain.programs import DeliveryInvariantViolation, EnrollmentNotFound
from clientplatform.infrastructure import DispatchOutboxRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.customer_progress_repository import CustomerProgressRepository
from clientplatform.infrastructure.program_progress_repository import ProgramProgressRepository
from services.db import get_db


_COMMAND_PREFIX = "cpi:"
_PROGRAM_PAGE_SIZE = 7
_LESSON_PAGE_SIZE = 6
_SLOT_PAGE_SIZE = 6
_INT_RE = re.compile(r"0|[1-9][0-9]{0,4}")
_ALIASES = {
    "start": ("menu", ()),
    "/start": ("menu", ()),
    "начать": ("menu", ()),
    "меню": ("menu", ()),
    "главное меню": ("menu", ()),
    "мои программы": ("programs", ("0",)),
    "программы": ("programs", ("0",)),
    "запись": ("slots", ("0",)),
    "записаться": ("slots", ("0",)),
    "доступная запись": ("slots", ("0",)),
    "свободное время": ("slots", ("0",)),
}


@dataclass(frozen=True, slots=True)
class ParsedCustomerInteraction:
    action: str
    args: tuple[str, ...]
    explicit: bool = True


def _compact(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().replace("ё", "е").split())


def parse_native_customer_interaction(value: object) -> ParsedCustomerInteraction | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    alias = _ALIASES.get(_compact(raw))
    if alias is not None:
        return ParsedCustomerInteraction(alias[0], tuple(alias[1]))
    if not raw.casefold().startswith(_COMMAND_PREFIX):
        return None
    parts = raw.split(":")
    if len(parts) < 2 or any(not part for part in parts):
        return ParsedCustomerInteraction("stale", ())
    action = parts[1].strip().casefold()
    args = tuple(part.strip() for part in parts[2:])
    if action not in {"menu", "programs", "program", "done", "slots", "book"}:
        return ParsedCustomerInteraction("stale", ())
    return ParsedCustomerInteraction(action, args)


def is_native_customer_interaction_input(value: object) -> bool:
    return parse_native_customer_interaction(value) is not None


def _button(label: str, command: str) -> CustomerInteractionButton:
    return CustomerInteractionButton(label=label[:40], command=command)


def _menu_message(*, linked: bool = False) -> CustomerInteractionMessage:
    heading = (
        "✅ Канал подключён к Вашей карточке клиента.\n\n"
        if linked
        else ""
    )
    return CustomerInteractionMessage(
        text=(
            heading
            + "ClientPlatform\n\n"
            + "Здесь можно открыть свои программы и выбрать доступное время записи."
        ),
        rows=(
            (_button("📚 Мои программы", "cpi:programs:0"),),
            (_button("📅 Доступная запись", "cpi:slots:0"),),
        ),
    )


def _stale_message() -> CustomerInteractionMessage:
    return CustomerInteractionMessage(
        text="Эта кнопка уже неактуальна. Откройте нужный раздел заново.",
        rows=((_button("🏠 Меню", "cpi:menu"),),),
    )


def _page_index(raw: str | None) -> int | None:
    value = str(raw or "").strip()
    if not _INT_RE.fullmatch(value):
        return None
    return int(value)


def _window(items: list[Any], page: int, size: int) -> tuple[list[Any], int, int]:
    if page < 0:
        raise ValueError("page must be non-negative")
    count = max(1, (len(items) + size - 1) // size)
    if page >= count:
        raise IndexError("page is outside result set")
    start = page * size
    return items[start : start + size], page, count


def _programs_message(
    repository: ProgramProgressRepository,
    *,
    business_id: str,
    customer_id: str,
    page: int,
) -> CustomerInteractionMessage:
    programs = repository.list_customer_programs_by_customer(
        business_id=business_id, customer_id=customer_id
    )
    if not programs:
        return CustomerInteractionMessage(
            text="Вам пока не выдали ни одной программы.",
            rows=((_button("🏠 Меню", "cpi:menu"),),),
        )
    current, index, count = _window(programs, page, _PROGRAM_PAGE_SIZE)
    lines = [
        f"• {item.program_title} — {item.completed_lessons}/{item.total_lessons} "
        f"({item.percent_complete}%)"
        for item in current
    ]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (
            _button(
                item.program_title,
                f"cpi:program:{item.enrollment_id}:0",
            ),
        )
        for item in current
    ]
    navigation: list[CustomerInteractionButton] = []
    if index > 0:
        navigation.append(_button("⬅️ Назад", f"cpi:programs:{index - 1}"))
    if index + 1 < count:
        navigation.append(_button("Вперёд ➡️", f"cpi:programs:{index + 1}"))
    if navigation:
        rows.append(tuple(navigation))
    rows.append((_button("🏠 Меню", "cpi:menu"),))
    return CustomerInteractionMessage(
        text=(
            "Мои программы\n\n"
            + "\n".join(lines)
            + f"\n\nСтраница {index + 1}/{count}"
        ),
        rows=tuple(rows),
    )


def _program_message(
    repository: ProgramProgressRepository,
    *,
    business_id: str,
    customer_id: str,
    enrollment_id: str,
    page: int,
) -> CustomerInteractionMessage:
    program = repository.get_customer_program_by_customer(
        business_id=business_id,
        customer_id=customer_id,
        enrollment_id=enrollment_id,
    )
    lessons = list(program.lessons)
    current, index, count = _window(lessons or [None], page, _LESSON_PAGE_SIZE)
    icons = {
        "pending": "⏳",
        "delivered": "📬",
        "opened": "👀",
        "completed": "✅",
        "skipped": "⏭",
    }
    lines: list[str] = []
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    for lesson in current:
        if lesson is None:
            lines.append("В программе пока нет материалов.")
            continue
        lines.append(
            f"{icons.get(lesson.progress_status.value, '•')} "
            f"{lesson.position}. {lesson.title}"
        )
        if lesson.can_complete:
            rows.append(
                (
                    _button(
                        f"✅ Готово · урок {lesson.position}",
                        f"cpi:done:{enrollment_id}:{lesson.position}:{index}",
                    ),
                )
            )
    navigation: list[CustomerInteractionButton] = []
    if index > 0:
        navigation.append(
            _button("⬅️ Назад", f"cpi:program:{enrollment_id}:{index - 1}")
        )
    if index + 1 < count:
        navigation.append(
            _button("Вперёд ➡️", f"cpi:program:{enrollment_id}:{index + 1}")
        )
    if navigation:
        rows.append(tuple(navigation))
    rows.append((_button("📚 Все программы", "cpi:programs:0"),))
    if len(rows) < 10:
        rows.append((_button("🏠 Меню", "cpi:menu"),))
    return CustomerInteractionMessage(
        text=(
            f"{program.summary.program_title}\n\n"
            + "\n".join(lines)
            + "\n\n"
            + f"Пройдено: {program.summary.completed_lessons}/"
            + f"{program.summary.total_lessons} ({program.summary.percent_complete}%)"
        ),
        rows=tuple(rows),
    )


def _completion_message(result: Any, *, enrollment_id: str, page: int) -> CustomerInteractionMessage:
    if result.next_material_queued:
        detail = "Урок отмечен выполненным. Следующий материал уже поставлен в отправку."
    elif result.program.summary.enrollment_status.value == "completed":
        detail = "Урок отмечен выполненным. Программа завершена."
    else:
        detail = "Урок отмечен выполненным."
    summary = result.program.summary
    return CustomerInteractionMessage(
        text=(
            detail
            + "\n\n"
            + f"Пройдено: {summary.completed_lessons}/{summary.total_lessons} "
            + f"({summary.percent_complete}%)"
        ),
        rows=(
            (_button("📖 Открыть программу", f"cpi:program:{enrollment_id}:{page}"),),
            (_button("🏠 Меню", "cpi:menu"),),
        ),
    )


def _slots_message(
    repository: BookingRepository,
    *,
    business_id: str,
    customer_id: str,
    page: int,
) -> CustomerInteractionMessage:
    slots = repository.list_open_slots_for_customer_id(
        business_id=business_id, customer_id=customer_id
    )
    if not slots:
        return CustomerInteractionMessage(
            text="Сейчас свободного времени нет. Специалист сможет добавить его позже.",
            rows=((_button("🏠 Меню", "cpi:menu"),),),
        )
    current, index, count = _window(slots, page, _SLOT_PAGE_SIZE)
    lines = [
        f"• {slot.offering_title} — {slot.local_start}, {slot.slot.duration_minutes} мин."
        for slot in current
    ]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (
            _button(
                f"{slot.local_start} · {slot.offering_title}",
                f"cpi:book:{slot.slot.id}",
            ),
        )
        for slot in current
    ]
    navigation: list[CustomerInteractionButton] = []
    if index > 0:
        navigation.append(_button("⬅️ Назад", f"cpi:slots:{index - 1}"))
    if index + 1 < count:
        navigation.append(_button("Вперёд ➡️", f"cpi:slots:{index + 1}"))
    if navigation:
        rows.append(tuple(navigation))
    rows.append((_button("🏠 Меню", "cpi:menu"),))
    return CustomerInteractionMessage(
        text="Доступная запись\n\n" + "\n".join(lines) + f"\n\nСтраница {index + 1}/{count}",
        rows=tuple(rows),
    )


def _booking_success_message(claim: Any) -> CustomerInteractionMessage:
    slot = claim.slot
    return CustomerInteractionMessage(
        text=(
            "✅ Запись подтверждена.\n\n"
            + f"{slot.offering_title} — {slot.local_start}, "
            + f"{slot.slot.duration_minutes} мин."
        ),
        rows=(
            (_button("📅 Другие свободные окна", "cpi:slots:0"),),
            (_button("🏠 Меню", "cpi:menu"),),
        ),
    )


def _booking_unavailable_message() -> CustomerInteractionMessage:
    return CustomerInteractionMessage(
        text="Это время уже недоступно. Показываю актуальные варианты.",
        rows=(
            (_button("📅 Открыть свободное время", "cpi:slots:0"),),
            (_button("🏠 Меню", "cpi:menu"),),
        ),
    )


def _resolve_message(
    conn: Any,
    *,
    parsed: ParsedCustomerInteraction,
    business_id: str,
    customer_id: str,
) -> CustomerInteractionMessage:
    args = parsed.args
    if parsed.action == "menu":
        return _menu_message()
    if parsed.action == "stale":
        return _stale_message()
    if parsed.action == "programs":
        page = _page_index(args[0] if len(args) == 1 else None)
        if page is None:
            return _stale_message()
        try:
            return _programs_message(
                ProgramProgressRepository(conn),
                business_id=business_id,
                customer_id=customer_id,
                page=page,
            )
        except IndexError:
            return _stale_message()
    if parsed.action == "program":
        if len(args) != 2:
            return _stale_message()
        page = _page_index(args[1])
        if page is None:
            return _stale_message()
        try:
            return _program_message(
                ProgramProgressRepository(conn),
                business_id=business_id,
                customer_id=customer_id,
                enrollment_id=args[0],
                page=page,
            )
        except (EnrollmentNotFound, ValueError, IndexError):
            return _stale_message()
    if parsed.action == "done":
        if len(args) != 3:
            return _stale_message()
        page = _page_index(args[2])
        position = _page_index(args[1])
        if page is None or position is None or position <= 0:
            return _stale_message()
        try:
            result = CustomerProgressRepository(conn).complete_lesson_by_customer(
                business_id=business_id,
                customer_id=customer_id,
                enrollment_id=args[0],
                lesson_position=position,
            )
        except (EnrollmentNotFound, DeliveryInvariantViolation, ValueError):
            return _stale_message()
        return _completion_message(result, enrollment_id=args[0], page=page)
    if parsed.action == "slots":
        page = _page_index(args[0] if len(args) == 1 else None)
        if page is None:
            return _stale_message()
        try:
            return _slots_message(
                BookingRepository(conn),
                business_id=business_id,
                customer_id=customer_id,
                page=page,
            )
        except (BookingNotFound, ValueError, IndexError):
            return _stale_message()
    if parsed.action == "book":
        if len(args) != 1:
            return _stale_message()
        try:
            claim = book_customer_slot_by_customer_in_transaction(
                conn,
                business_id=business_id,
                customer_id=customer_id,
                slot_id=args[0],
            )
        except (BookingNotFound, BookingInvariantViolation, ValueError):
            return _booking_unavailable_message()
        return _booking_success_message(claim)
    return _stale_message()


def process_native_customer_interaction(
    *,
    route: MessengerIngressRoute,
    identity: CustomerIdentity,
    raw_text: object,
    provider_event_id: str,
    linked: bool = False,
) -> bool:
    """Persist one deterministic VK/MAX customer UX response, if applicable."""

    if identity.status != CustomerIdentityStatus.ACTIVE:
        raise ValueError("native customer interaction requires an active identity")
    if identity.business_id != route.business_id:
        raise ValueError("native customer identity belongs to another business")
    if identity.platform.value != route.platform.value:
        raise ValueError("native customer identity platform does not match route")

    parsed = ParsedCustomerInteraction("menu", ()) if linked else parse_native_customer_interaction(raw_text)
    if parsed is None:
        return False
    event_id = str(provider_event_id or "").strip()
    if not event_id or len(event_id) > 160:
        raise ValueError("provider_event_id must be 1..160 characters")

    with get_db() as conn:
        message = (
            _menu_message(linked=True)
            if linked
            else _resolve_message(
                conn,
                parsed=parsed,
                business_id=route.business_id,
                customer_id=identity.customer_id,
            )
        )
        DispatchOutboxRepository(conn).materialize_customer_interaction(
            business_id=route.business_id,
            connection_id=route.connection_id,
            customer_identity_id=identity.id,
            customer_id=identity.customer_id,
            platform=route.platform.value,
            interaction=message,
            interaction_key=f"{route.id}:{event_id}:customer-ui-v1",
        )
    return True


__all__ = [
    "is_native_customer_interaction_input",
    "parse_native_customer_interaction",
    "process_native_customer_interaction",
]
