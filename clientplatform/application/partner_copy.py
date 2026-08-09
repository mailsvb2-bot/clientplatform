from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from clientplatform.domain.partners import (
    PartnerCampaign,
    PartnerCandidate,
    PartnerContentPack,
)


@dataclass(frozen=True, slots=True)
class PartnerCopyContext:
    business_name: str
    activity_description: str
    offerings: tuple[str, ...]
    campaign: PartnerCampaign
    candidate: PartnerCandidate
    public_target_url: str = ""


class PartnerCopyGenerator(Protocol):
    def generate(self, context: PartnerCopyContext) -> PartnerContentPack: ...


class DeterministicPartnerCopyGenerator:
    """Prepare factual, bounded partner copy without inventing relationship facts."""

    def generate(self, context: PartnerCopyContext) -> PartnerContentPack:
        candidate = context.candidate
        goal = context.campaign.goal
        target_url = str(context.public_target_url or goal.target_url or "").strip()
        business = _compact(context.business_name, 120)
        activity = _compact(context.activity_description, 300)
        offerings = ", ".join(_compact(item, 100) for item in context.offerings[:4])
        event = _compact(goal.event_title, 180)
        target = _compact(goal.offer_summary or offerings or activity, 360)

        if candidate.recent_topic:
            topic_line = (
                f"У проекта сейчас заметна тема «{_compact(candidate.recent_topic, 180)}». "
            )
        elif candidate.audience_summary:
            topic_line = (
                "По публичному описанию аудитории близка тема: "
                f"{_compact(candidate.audience_summary, 220)}. "
            )
        else:
            topic_line = ""

        if event:
            angle = f"полезный материал или эфир вокруг «{event}»"
            public_hook = event
        else:
            angle = "полезный гостевой материал для аудитории"
            public_hook = target or "Практический разбор"

        outreach = (
            f"Здравствуйте! {topic_line}"
            f"Я представляю {business}. Предлагаю не рекламную вставку, а {angle}: "
            "мы подготовим текст/анонс и возьмём содержательную часть на себя. "
            "С вашей стороны — только решение, подходит ли это аудитории. "
            "Если интересно, пришлю готовый вариант поста без обязательств."
        )
        ready_post = _ready_post(
            public_hook=public_hook,
            target=target,
            business=business,
            target_url=target_url,
        )
        followup = (
            "Здравствуйте! Один раз напомню о предложении выше. "
            "Готовый текст уже подготовлен, чтобы не требовать времени на редактуру. "
            "Если тема не подходит — повторно писать не будем."
        )
        cta = (
            f"Подробнее: {target_url}"
            if target_url
            else "Ответьте «интересно» — пришлю готовый материал."
        )
        pack = PartnerContentPack(
            candidate_id=candidate.id,
            subject=f"Материал для аудитории {candidate.name}",
            outreach_message=_bounded(outreach, 3900),
            ready_post=_bounded(ready_post, 4900),
            followup_message=_bounded(followup, 1900),
            collaboration_angle=_bounded(angle, 480),
            cta=_bounded(cta, 280),
        )
        validate_partner_content(pack)
        return pack


_DENY_PHRASES = (
    "100% гарантия",
    "гарантированный результат",
    "избавим навсегда",
    "вылечим",
    "исцеляет",
    "без побочных эффектов",
)


def validate_partner_content(pack: PartnerContentPack) -> None:
    text = " ".join(
        (
            pack.subject,
            pack.outreach_message,
            pack.ready_post,
            pack.followup_message,
            pack.collaboration_angle,
            pack.cta,
        )
    ).casefold()
    for phrase in _DENY_PHRASES:
        if phrase.casefold() in text:
            raise ValueError(f"partner content contains forbidden claim: {phrase}")
    if pack.outreach_message.count("!") > 3:
        raise ValueError("partner outreach is too promotional")
    if "мы изучили ваш канал" in text or "давно слежу" in text:
        raise ValueError("partner outreach must not invent familiarity")


def _ready_post(*, public_hook: str, target: str, business: str, target_url: str) -> str:
    headline = _compact(public_hook, 180) or "Практический разбор"
    benefit = _compact(target, 700)
    body = (
        f"{headline}\n\n"
        f"{benefit}\n\n"
        f"Материал подготовил {business}. Без обещаний чудес: задача — дать понятную пользу, "
        "показать метод и оставить человеку возможность самому решить, подходит ли ему следующий шаг."
    )
    if target_url:
        body += f"\n\nУзнать подробности: {target_url}"
    return body


def _compact(value: object, maximum: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:maximum].rstrip()


def _bounded(value: str, maximum: int) -> str:
    value = " ".join(value.split()).strip()
    return value if len(value) <= maximum else value[:maximum].rstrip()


__all__ = [
    "DeterministicPartnerCopyGenerator",
    "PartnerCopyContext",
    "PartnerCopyGenerator",
    "validate_partner_content",
]
