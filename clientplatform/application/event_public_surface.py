from __future__ import annotations

from html import escape
from urllib.parse import quote

from clientplatform.application.event_commercial_consent import (
    build_event_commercial_consent_text,
    commercial_consent_text_sha256,
)
from clientplatform.domain.event_landing import EventLandingContent
from clientplatform.domain.events import PublicEvent


SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}


def _marketing_block(advertiser_label: object | None) -> str:
    if not advertiser_label:
        return ""
    consent_text = build_event_commercial_consent_text(advertiser_label)
    consent_hash = commercial_consent_text_sha256(advertiser_label)
    return (
        "<fieldset><legend>Полезные материалы и предложения (необязательно)</legend>"
        f"<input type=hidden name=marketing_consent_hash value='{escape(consent_hash, quote=True)}'>"
        f"<label><input style='width:auto' type=checkbox name=marketing_consent value=yes> "
        f"{escape(consent_text)}</label>"
        "<p>Выберите каналы. E-mail подтверждается этой формой. Для Telegram, "
        "ВКонтакте и MAX после регистрации появится одноразовая кнопка: коммерческие "
        "сообщения в мессенджере включатся только после подтверждения владения аккаунтом.</p>"
        "<label><input style='width:auto' type=checkbox name=marketing_channel value=email checked> E-mail</label> "
        "<label><input style='width:auto' type=checkbox name=marketing_channel value=telegram> Telegram</label> "
        "<label><input style='width:auto' type=checkbox name=marketing_channel value=vk> ВКонтакте</label> "
        "<label><input style='width:auto' type=checkbox name=marketing_channel value=max> MAX</label>"
        "</fieldset>"
    )


def _registration_form(
    event: PublicEvent,
    *,
    source: object,
    campaign_ref: object,
    advertiser_label: object | None,
) -> str:
    source_value = str(source or "").strip()[:160]
    campaign_value = str(campaign_ref or "").strip()[:240]
    action = f"/e/{quote(event.public_slug, safe='')}/register"
    return (
        "<section id=registration class='landing-registration'>"
        "<form method=post action='"
        + escape(action, quote=True)
        + "'>"
        "<fieldset><legend>Регистрация на мероприятие</legend>"
        "<label>Имя</label><input name=name maxlength=120 required autocomplete=name>"
        "<label>E-mail</label><input name=email maxlength=320 type=email required autocomplete=email>"
        "<label>Телефон (необязательно)</label><input name=phone maxlength=40 autocomplete=tel>"
        "<div class=hp aria-hidden=true><input name=company tabindex=-1 autocomplete=off></div>"
        "<label><input style='width:auto' type=checkbox name=consent value=yes required> "
        "Согласен на обработку данных для регистрации и получения организационных сообщений "
        "об этом мероприятии.</label></fieldset>"
        f"<input type=hidden name=source value='{escape(source_value, quote=True)}'>"
        f"<input type=hidden name=campaign_ref value='{escape(campaign_value, quote=True)}'>"
        + _marketing_block(advertiser_label)
        + "<button type=submit>Зарегистрироваться</button></form></section>"
    )


def _points(title: str, values: tuple[str, ...], *, css_class: str) -> str:
    if not values:
        return ""
    items = "".join(f"<li>{escape(item)}</li>" for item in values)
    return (
        f"<section class='landing-section {escape(css_class, quote=True)}'>"
        f"<h2>{escape(title)}</h2><ul class=landing-points>{items}</ul></section>"
    )


def _sales_landing(
    event: PublicEvent,
    landing: EventLandingContent,
    *,
    source: object,
    campaign_ref: object,
    advertiser_label: object | None,
) -> str:
    faq = ""
    if landing.faq:
        faq_items = "".join(
            "<details><summary>"
            + escape(item.question)
            + "</summary><p>"
            + escape(item.answer)
            + "</p></details>"
            for item in landing.faq
        )
        faq = (
            "<section class='landing-section landing-faq'>"
            f"<h2>{escape(landing.faq_title)}</h2>{faq_items}</section>"
        )
    speaker = ""
    if landing.speaker_text:
        speaker = (
            "<section class='landing-section landing-speaker'>"
            f"<h2>{escape(landing.speaker_title)}</h2>"
            f"<p>{escape(landing.speaker_text)}</p></section>"
        )
    subtitle = (
        f"<p class=landing-lead>{escape(landing.hero_subtitle)}</p>"
        if landing.hero_subtitle
        else ""
    )
    eyebrow = (
        f"<p class=landing-eyebrow>{escape(landing.eyebrow)}</p>"
        if landing.eyebrow
        else ""
    )
    return (
        f"<div class='landing landing--{escape(landing.theme.value, quote=True)}'>"
        "<section class=landing-hero>"
        + eyebrow
        + f"<h1>{escape(landing.hero_title)}</h1>"
        + subtitle
        + f"<p class=landing-date><b>{escape(event.local_start_label())}</b></p>"
        + "<a class=landing-cta href='#registration'>Зарегистрироваться</a>"
        + "</section>"
        + _points(
            landing.audience_title,
            landing.audience_points,
            css_class="landing-audience",
        )
        + _points(
            landing.outcomes_title,
            landing.outcome_points,
            css_class="landing-outcomes",
        )
        + _points(
            landing.agenda_title,
            landing.agenda_points,
            css_class="landing-agenda",
        )
        + speaker
        + faq
        + "<section class='landing-section landing-final-cta'>"
        + f"<h2>{escape(landing.cta_title)}</h2>"
        + (f"<p>{escape(landing.cta_text)}</p>" if landing.cta_text else "")
        + "</section>"
        + _registration_form(
            event,
            source=source,
            campaign_ref=campaign_ref,
            advertiser_label=advertiser_label,
        )
        + "</div>"
    )


def render_event_landing_body(
    event: PublicEvent,
    *,
    source: object = "",
    campaign_ref: object = "",
    advertiser_label: object | None = None,
    landing: EventLandingContent | None = None,
) -> str:
    if landing is not None:
        return _sales_landing(
            event,
            landing,
            source=source,
            campaign_ref=campaign_ref,
            advertiser_label=advertiser_label,
        )
    return (
        f"<h1>{escape(event.title)}</h1>"
        f"<p>{escape(event.description)}</p>"
        f"<p><b>{escape(event.local_start_label())}</b></p>"
        + _registration_form(
            event,
            source=source,
            campaign_ref=campaign_ref,
            advertiser_label=advertiser_label,
        )
    )


__all__ = ["SECURITY_HEADERS", "render_event_landing_body"]
