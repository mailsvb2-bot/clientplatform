from __future__ import annotations

"""Stable, business-aware quick actions for owner messenger home screens.

The quick menu is intentionally derived only from durable business facts
(activity description + enabled capability kinds + staff role). Runtime alerts,
metrics and "next best action" projections must not reorder this menu: those
belong to the separate attention/current-work surfaces.
"""

from dataclasses import dataclass
from typing import Iterable

from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.tenancy import PlatformRole


@dataclass(frozen=True, slots=True)
class OwnerQuickAction:
    key: str
    label: str
    need: str


_SUPPORT_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.SUPPORT,
    }
)
_ACQUISITION_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.CONTENT_MANAGER,
        PlatformRole.MARKETER,
    }
)
_MANAGE_WORK_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.CONTENT_MANAGER,
    }
)
_EVENT_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.MARKETER,
    }
)

_CONSULTATION_WORDS = (
    "консультац",
    "психолог",
    "психотерап",
    "коуч",
    "прием",
    "приём",
)
_EDUCATION_WORDS = (
    "школ",
    "репетитор",
    "преподав",
    "обуч",
    "заняти",
    "урок",
)
_AUTO_WORDS = (
    "автосервис",
    "автомоб",
    "машин",
    "шиномонтаж",
)
_EVENT_WORDS = (
    "вебинар",
    "эфир",
    "семинар",
    "мастер-класс",
    "мастер класс",
    "тренинг",
)
_PROGRAM_WORDS = (
    "курс",
    "программ",
    "обуч",
    "урок",
    "школ",
)
_SERVICE_WORDS = (
    "услуг",
    "запис",
    "салон",
    "маникюр",
    "парикмах",
    "барбер",
    "косметолог",
    "ремонт",
)


def _normalized(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().replace("ё", "е").split())


def _contains(text: str, words: Iterable[str]) -> bool:
    return any(_normalized(word) in text for word in words)


def _active_capability_keys(capabilities: Iterable[object]) -> frozenset[str]:
    keys: set[str] = set()
    for item in capabilities:
        status = getattr(item, "status", None)
        if status not in {CapabilityStatus.ACTIVE, CapabilityStatus.ACTIVE.value, "active"}:
            continue
        key = str(getattr(item, "connector_key", "") or "").strip().casefold()
        if key:
            keys.add(key)
    return frozenset(keys)


def _booking_label(activity: str) -> str:
    if _contains(activity, _AUTO_WORDS):
        return "🚗 Записать машину"
    # A specialist can also sell educational programs; explicit consultation
    # wording must win over generic words such as "обучение" or "программа".
    if _contains(activity, _CONSULTATION_WORDS):
        return "📅 Записать на консультацию"
    if _contains(activity, _EDUCATION_WORDS):
        return "📅 Записать на занятие"
    return "📅 Запись клиентов"


def _event_label(activity: str) -> str:
    return "🎥 Провести вебинар" if "вебинар" in activity else "🎥 Провести мероприятие"


def build_owner_quick_actions(
    *,
    activity_description: object,
    capabilities: Iterable[object],
    role: PlatformRole,
    limit: int = 7,
) -> tuple[OwnerQuickAction, ...]:
    """Return a stable quick menu for one business and staff role.

    ``limit`` includes the final full-surface escape. The function deliberately
    does not inspect current alerts, campaign metrics or recent behavior, so the
    everyday menu remains predictable between visits. Every visible quick action
    must also be reachable for the supplied role; a forbidden shortcut is worse
    UX than omitting it and leaving the full capability surface available.
    """

    if limit < 2:
        raise ValueError("owner quick menu limit must be at least 2")

    activity = _normalized(activity_description)
    capability_keys = _active_capability_keys(capabilities)
    consultation_or_service = bool(
        capability_keys.intersection({"consultations", "services", "custom"})
    ) or _contains(activity, (*_CONSULTATION_WORDS, *_SERVICE_WORDS, *_AUTO_WORDS))
    programs = "programs" in capability_keys or _contains(activity, _PROGRAM_WORDS)
    events = _contains(activity, _EVENT_WORDS)

    candidates: list[OwnerQuickAction] = []

    if role in _SUPPORT_ROLES:
        candidates.append(
            OwnerQuickAction(
                "customers",
                "💬 Клиенты и обращения",
                "увидеть клиентов и тех, кому нужен ответ",
            )
        )
    if consultation_or_service and role in _MANAGE_WORK_ROLES:
        candidates.append(
            OwnerQuickAction(
                "booking",
                _booking_label(activity),
                "открыть запись и свободное время",
            )
        )
    if events and role in _EVENT_ROLES:
        candidates.append(
            OwnerQuickAction(
                "events",
                _event_label(activity),
                "создать мероприятие и работать с участниками",
            )
        )
    if programs and role in _MANAGE_WORK_ROLES:
        candidates.append(
            OwnerQuickAction(
                "programs",
                "🎓 Материалы и программы",
                "создать или выдать курс, урок или материал",
            )
        )
    if role in _ACQUISITION_ROLES:
        candidates.append(
            OwnerQuickAction(
                "acquire",
                "👥 Найти клиентов",
                "привлечь новых людей под текущую задачу",
            )
        )
    if role in _SUPPORT_ROLES:
        candidates.append(
            OwnerQuickAction(
                "sales",
                "💰 Продажи",
                "продолжить работу с теми, кто интересовался или не купил",
            )
        )

    # Preserve semantic uniqueness if later rules converge on the same action.
    unique: list[OwnerQuickAction] = []
    seen: set[str] = set()
    for item in candidates:
        if item.key in seen:
            continue
        seen.add(item.key)
        unique.append(item)

    reserved = 2 if role in _SUPPORT_ROLES else 1
    visible = unique[: max(0, limit - reserved)]
    if role in _SUPPORT_ROLES:
        visible.append(
            OwnerQuickAction(
                "results",
                "📊 Результаты",
                "увидеть, что происходит и что требует внимания",
            )
        )
    visible.append(
        OwnerQuickAction(
            "all",
            "▦ Все возможности",
            "открыть полный набор возможностей ClientPlatform",
        )
    )
    return tuple(visible)


def quick_menu_intro(*, business_name: object) -> str:
    name = " ".join(str(business_name or "").strip().split()) or "Ваш бизнес"
    return (
        f"🏠 {name}\n\n"
        "Быстрые действия подобраны под этот бизнес. "
        "Они остаются на своих местах, чтобы к ним не приходилось привыкать заново.\n\n"
        "Что нужно сделать?"
    )


__all__ = ["OwnerQuickAction", "build_owner_quick_actions", "quick_menu_intro"]
