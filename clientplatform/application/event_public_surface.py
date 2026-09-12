from __future__ import annotations

from html import escape
from urllib.parse import quote

from clientplatform.application.event_commercial_consent import (
    build_event_commercial_consent_text,
    commercial_consent_text_sha256,
)
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


def render_event_landing_body(
    event: PublicEvent,
    *,
    source: object = "",
    campaign_ref: object = "",
    advertiser_label: object | None = None,
) -> str:
    source_value = str(source or "").strip()[:160]
    campaign_value = str(campaign_ref or "").strip()[:240]
    action = f"/e/{quote(event.public_slug, safe='')}/register"
    marketing_block = ""
    if advertiser_label:
        consent_text = build_event_commercial_consent_text(advertiser_label)
        consent_hash = commercial_consent_text_sha256(advertiser_label)
        marketing_block = (
            "<fieldset><legend>Необязательное согласие на сообщения о предложениях</legend>"
            f"<input type=hidden name=marketing_consent_hash value='{escape(consent_hash, quote=True)}'>"
            f"<label><input style='width:auto' type=checkbox name=marketing_consent value=yes> "
            f"{escape(consent_text)}</label>"
            "<p>Каналы:</p>"
            "<label><input style='width:auto' type=checkbox name=marketing_channel value=email checked> E-mail</label> "
            "<label><input style='width:auto' type=checkbox name=marketing_channel value=max> MAX</label> "
            "<label><input style='width:auto' type=checkbox name=marketing_channel value=vk> VK</label>"
            "</fieldset>"
        )
    return (
        f"<h1>{escape(event.title)}</h1>"
        f"<p>{escape(event.description)}</p>"
        f"<p><b>{escape(event.local_start_label())}</b></p>"
        f"<form method=post action='{escape(action, quote=True)}'>"
        "<label>Имя</label><input name=name maxlength=120 required autocomplete=name>"
        "<label>E-mail</label><input name=email maxlength=320 type=email required autocomplete=email>"
        "<label>Телефон</label><input name=phone maxlength=40 autocomplete=tel>"
        "<div class=hp aria-hidden=true><input name=company tabindex=-1 autocomplete=off></div>"
        "<label><input style='width:auto' type=checkbox name=consent value=yes required> "
        "Согласен на обработку данных для регистрации и получения организационных сообщений "
        "об этом мероприятии.</label>"
        f"<input type=hidden name=source value='{escape(source_value, quote=True)}'>"
        f"<input type=hidden name=campaign_ref value='{escape(campaign_value, quote=True)}'>"
        + marketing_block
        + "<button type=submit>Зарегистрироваться</button></form>"
    )


__all__ = ["SECURITY_HEADERS", "render_event_landing_body"]
