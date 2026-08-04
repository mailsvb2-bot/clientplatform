from __future__ import annotations

"""Provider-neutral promotion copy adapted from the BusinesAIOS ads pipeline.

ClientPlatform deliberately keeps this module deterministic and side-effect free.
It receives one business, offering and open booking slot and returns safe copy;
it does not import BusinesAIOS, choose advertising budgets or publish externally.
"""

from dataclasses import dataclass

from clientplatform.domain.promotions import (
    CreativeGuardrails,
    PromotionCreative,
    stable_creative_id,
    validate_creative,
)


@dataclass(frozen=True, slots=True)
class PromotionBrief:
    business_name: str
    activity_description: str
    offering_title: str
    offering_description: str
    local_start: str
    duration_minutes: int


_CONSULTATION_MARKERS = (
    "психолог",
    "психотерап",
    "консультац",
    "коуч",
    "настав",
    "преподав",
    "заняти",
    "терап",
)
_LOCAL_SERVICE_MARKERS = (
    "сантех",
    "ремонт",
    "установ",
    "мастер",
    "автосервис",
    "автослес",
    "электрик",
    "окн",
    "уборк",
)


def _compact(value: object, *, maximum: int) -> str:
    normalized = " ".join(str(value or "").split())
    return normalized[:maximum].rstrip()


def _business_kind(brief: PromotionBrief) -> str:
    context = " ".join(
        (
            brief.business_name,
            brief.activity_description,
            brief.offering_title,
            brief.offering_description,
        )
    ).lower()
    if any(marker in context for marker in _CONSULTATION_MARKERS):
        return "consultation"
    if any(marker in context for marker in _LOCAL_SERVICE_MARKERS):
        return "local_service"
    return "general"


def _candidate(
    *,
    brief: PromotionBrief,
    style: str,
    headline: str,
    primary_text: str,
    description: str,
) -> PromotionCreative:
    clean_headline = _compact(headline, maximum=60)
    clean_primary = _compact(primary_text, maximum=420)
    clean_description = _compact(description, maximum=100)
    return PromotionCreative(
        creative_id=stable_creative_id(
            brief.business_name,
            brief.offering_title,
            brief.local_start,
            brief.duration_minutes,
            style,
            clean_headline,
            clean_primary,
        ),
        headline=clean_headline,
        primary_text=clean_primary,
        description=clean_description,
        cta="Записаться",
        style=style,
    )


def _safe_fallback(brief: PromotionBrief) -> PromotionCreative:
    headline = _compact(brief.offering_title, maximum=60) or "Запись на услугу"
    return _candidate(
        brief=brief,
        style="fallback",
        headline=headline,
        primary_text=(
            f"Доступно время: {brief.local_start}. "
            "Посмотрите подробности и запишитесь на удобное время."
        ),
        description=f"{brief.duration_minutes} минут · запись онлайн",
    )


def generate_promotion_candidates(
    brief: PromotionBrief,
    *,
    guardrails: CreativeGuardrails | None = None,
) -> list[PromotionCreative]:
    """Return safe, domain-aware variants without inventing facts or outcomes."""

    rules = guardrails or CreativeGuardrails()
    kind = _business_kind(brief)
    details = _compact(
        brief.offering_description or brief.activity_description,
        maximum=190,
    )
    business = _compact(brief.business_name, maximum=80)
    offering = _compact(brief.offering_title, maximum=100)
    when = _compact(brief.local_start, maximum=40)

    if kind == "consultation":
        variants = [
            _candidate(
                brief=brief,
                style="trust",
                headline=offering,
                primary_text=(
                    f"{details}. Доступно время: {when}. "
                    "Можно спокойно посмотреть формат встречи и записаться онлайн."
                ),
                description=f"{brief.duration_minutes} минут · {business}",
            ),
            _candidate(
                brief=brief,
                style="expert",
                headline=f"Встреча с {business}",
                primary_text=(
                    f"{offering}. {details}. Ближайшее свободное время — {when}."
                ),
                description="Выберите удобное время без переписки",
            ),
            _candidate(
                brief=brief,
                style="concise",
                headline=f"Свободное время: {offering}",
                primary_text=f"{when}, {brief.duration_minutes} минут. Запись онлайн.",
                description=business,
            ),
        ]
    elif kind == "local_service":
        variants = [
            _candidate(
                brief=brief,
                style="availability",
                headline=f"Свободное время: {offering}",
                primary_text=(
                    f"{business}. {details}. Можно записаться на {when}. "
                    "Откройте карточку и подтвердите время."
                ),
                description=f"{brief.duration_minutes} минут · запись онлайн",
            ),
            _candidate(
                brief=brief,
                style="service",
                headline=offering,
                primary_text=(
                    f"{details}. Доступное время: {when}. "
                    "Запишитесь напрямую без звонков и ожидания ответа."
                ),
                description=business,
            ),
            _candidate(
                brief=brief,
                style="concise",
                headline=f"{offering} · {when}",
                primary_text=f"Свободное время у {business}. Нажмите, чтобы записаться.",
                description=f"Продолжительность: {brief.duration_minutes} минут",
            ),
        ]
    else:
        variants = [
            _candidate(
                brief=brief,
                style="direct",
                headline=offering,
                primary_text=(
                    f"{details}. Свободное время: {when}. "
                    "Посмотрите подробности и запишитесь онлайн."
                ),
                description=f"{brief.duration_minutes} минут · {business}",
            ),
            _candidate(
                brief=brief,
                style="business",
                headline=f"Предложение от {business}",
                primary_text=f"{offering}. {details}. Доступно: {when}.",
                description="Прямая запись без лишней переписки",
            ),
            _candidate(
                brief=brief,
                style="concise",
                headline=f"Свободно: {offering}",
                primary_text=f"{when}, {brief.duration_minutes} минут. Записаться онлайн.",
                description=business,
            ),
        ]

    safe = [candidate for candidate in variants if validate_creative(candidate, rules)[0]]
    if safe:
        return safe
    fallback = _safe_fallback(brief)
    if validate_creative(fallback, rules)[0]:
        return [fallback]
    return [
        _candidate(
            brief=brief,
            style="fixed_safe_fallback",
            headline="Запись на услугу",
            primary_text=(
                f"Доступно время: {when}. "
                "Посмотрите подробности и запишитесь онлайн."
            ),
            description=f"Продолжительность: {brief.duration_minutes} минут",
        )
    ]


def _score_candidate(candidate: PromotionCreative) -> float:
    score = 0.1
    if 10 <= len(candidate.headline) <= 45:
        score += 0.05
    lowered = (candidate.primary_text + " " + candidate.description).lower()
    if any(
        marker in lowered
        for marker in ("запис", "консультац", "встреч", "услуг", "время")
    ):
        score += 0.05
    if "!" not in lowered:
        score += 0.02
    return score


def select_promotion_creative(
    candidates: list[PromotionCreative],
    *,
    guardrails: CreativeGuardrails | None = None,
) -> PromotionCreative:
    if not candidates:
        raise ValueError("promotion creative selection requires candidates")
    rules = guardrails or CreativeGuardrails()
    valid = [candidate for candidate in candidates if validate_creative(candidate, rules)[0]]
    if not valid:
        raise ValueError("promotion creative selection requires a safe candidate")
    return max(valid, key=lambda candidate: (_score_candidate(candidate), candidate.creative_id))


__all__ = [
    "PromotionBrief",
    "generate_promotion_candidates",
    "select_promotion_creative",
]
