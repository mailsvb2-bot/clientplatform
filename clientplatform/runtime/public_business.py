from __future__ import annotations

"""First-party public business page and lead form."""

import asyncio
from html import escape

from aiohttp import web

from clientplatform.application.public_business_entry import (
    capture_public_business_lead,
    get_public_business_entry,
)
from clientplatform.domain.customers import CustomerIdentityConflict


PUBLIC_BUSINESS_MAX_BODY_BYTES = 16 * 1024
_SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}


def _page(title: str, body: str, *, status: int = 200) -> web.Response:
    html = (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;"
        "padding:0 18px;line-height:1.5}.card{border:1px solid #ddd;border-radius:18px;"
        "padding:28px}label{display:block;margin-top:12px}input,button{width:100%;"
        "box-sizing:border-box;padding:13px;margin:7px 0;font-size:16px}button{font-weight:700;"
        "cursor:pointer}.check{display:flex;gap:10px;align-items:flex-start;margin-top:14px}"
        ".check input{width:auto;margin-top:5px}.hp{position:absolute;left:-10000px}</style>"
        "</head><body><main class=card>"
        + body
        + "</main></body></html>"
    )
    return web.Response(
        status=status,
        text=html,
        content_type="text/html",
        charset="utf-8",
        headers=_SECURITY_HEADERS,
    )


def _form_body(entry, *, source: str, campaign_ref: str, error: str = "") -> str:
    activity = (
        f"<p>{escape(entry.activity_description)}</p>"
        if entry.activity_description
        else ""
    )
    error_html = f"<p><b>{escape(error)}</b></p>" if error else ""
    return (
        f"<h1>{escape(entry.business_name)}</h1>"
        + activity
        + "<p>Оставьте контакты — заявка попадёт прямо в ClientPlatform этого бизнеса.</p>"
        + error_html
        + "<form method=post>"
        "<label>Имя</label><input name=name maxlength=200 required autocomplete=name>"
        "<label>E-mail</label><input name=email maxlength=320 type=email autocomplete=email>"
        "<label>Телефон</label><input name=phone maxlength=40 autocomplete=tel "
        "placeholder='Можно оставить телефон вместо e-mail'>"
        "<div class=hp aria-hidden=true><label>Компания</label>"
        "<input name=company tabindex=-1 autocomplete=off></div>"
        "<label class=check><input type=checkbox name=consent value=yes required>"
        "<span>Согласен на обработку указанных данных для ответа на эту заявку.</span></label>"
        f"<input type=hidden name=source value='{escape(source, quote=True)}'>"
        f"<input type=hidden name=campaign_ref value='{escape(campaign_ref, quote=True)}'>"
        "<button type=submit>Отправить заявку</button></form>"
    )


async def public_business_page(request: web.Request) -> web.Response:
    token = str(request.match_info.get("business_token") or "").strip()
    try:
        entry = await asyncio.to_thread(
            get_public_business_entry,
            business_token=token,
        )
    except ValueError:
        return _page("Не найдено", "<h1>Страница бизнеса недоступна</h1>", status=404)
    source = str(request.query.get("source") or "").strip()[:120]
    campaign_ref = str(request.query.get("campaign_ref") or "").strip()[:200]
    return _page(
        entry.business_name,
        _form_body(entry, source=source, campaign_ref=campaign_ref),
    )


async def public_business_submit(request: web.Request) -> web.Response:
    if (
        request.content_length is not None
        and request.content_length > PUBLIC_BUSINESS_MAX_BODY_BYTES
    ):
        return web.Response(status=413, text="payload_too_large", headers=_SECURITY_HEADERS)
    token = str(request.match_info.get("business_token") or "").strip()
    try:
        entry = await asyncio.to_thread(
            get_public_business_entry,
            business_token=token,
        )
    except ValueError:
        return _page("Не найдено", "<h1>Страница бизнеса недоступна</h1>", status=404)
    try:
        form = await request.post()
    except (UnicodeDecodeError, ValueError):
        return _page(
            entry.business_name,
            _form_body(entry, source="", campaign_ref="", error="Не удалось прочитать форму."),
            status=400,
        )

    one = lambda key: str(form.get(key) or "").strip()
    source = one("source")[:120]
    campaign_ref = one("campaign_ref")[:200]
    if one("company"):
        # Honeypot: do not disclose that automated submission was discarded.
        return _page(entry.business_name, "<h1>Спасибо</h1><p>Заявка принята.</p>")
    if one("consent").lower() not in {"yes", "1", "true", "on"}:
        return _page(
            entry.business_name,
            _form_body(
                entry,
                source=source,
                campaign_ref=campaign_ref,
                error="Нужно подтвердить согласие на обработку данных.",
            ),
            status=400,
        )
    try:
        await asyncio.to_thread(
            capture_public_business_lead,
            business_token=token,
            display_name=one("name"),
            email=one("email") or None,
            phone=one("phone") or None,
            source=source or None,
            campaign_ref=campaign_ref or None,
        )
    except CustomerIdentityConflict:
        return _page(
            entry.business_name,
            _form_body(
                entry,
                source=source,
                campaign_ref=campaign_ref,
                error="Эти контакты уже относятся к разным клиентским карточкам. Укажите один контакт.",
            ),
            status=409,
        )
    except ValueError:
        return _page(
            entry.business_name,
            _form_body(
                entry,
                source=source,
                campaign_ref=campaign_ref,
                error="Проверьте имя, e-mail или телефон.",
            ),
            status=400,
        )

    return _page(
        entry.business_name,
        "<h1>Спасибо</h1>"
        "<p>Заявка передана бизнесу в ClientPlatform. С Вами свяжутся по указанному контакту.</p>",
    )


def register_public_business_routes(app: web.Application) -> None:
    app.router.add_get(
        "/clientplatform/b/{business_token}",
        public_business_page,
    )
    app.router.add_post(
        "/clientplatform/b/{business_token}",
        public_business_submit,
    )
    app["clientplatform_public_business_ingress"] = True


__all__ = [
    "PUBLIC_BUSINESS_MAX_BODY_BYTES",
    "public_business_page",
    "public_business_submit",
    "register_public_business_routes",
]
