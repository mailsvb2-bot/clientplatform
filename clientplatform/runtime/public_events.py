from __future__ import annotations

import asyncio
from html import escape
from aiohttp import web

from clientplatform.application.event_public_surface import (
    SECURITY_HEADERS,
    render_event_landing_body,
)
from clientplatform.application.events import (
    EventUnavailable,
    get_public_event,
    register_public_attendee_in_transaction,
)
from clientplatform.infrastructure.event_repository import EventNotFound, EventRepository
from services.db import get_db


EVENT_REGISTRATION_MAX_BODY_BYTES = 32 * 1024



def _page(title: str, body: str, *, status: int = 200) -> web.Response:
    html = (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;"
        "padding:0 18px;line-height:1.5}.card{border:1px solid #ddd;border-radius:18px;"
        "padding:28px}input,button{width:100%;box-sizing:border-box;padding:13px;"
        "margin:7px 0;font-size:16px}button{font-weight:700;cursor:pointer}.hp{position:absolute;"
        "left:-10000px}</style></head><body><main class=card>"
        + body
        + "</main></body></html>"
    )
    return web.Response(
        status=status,
        text=html,
        content_type="text/html",
        charset="utf-8",
        headers=SECURITY_HEADERS,
    )


def _landing(event, *, source: str = "", campaign_ref: str = "") -> web.Response:
    return _page(
        event.title,
        render_event_landing_body(event, source=source, campaign_ref=campaign_ref),
    )


async def public_event_landing(request: web.Request) -> web.Response:
    slug = str(request.match_info.get("slug") or "").strip()
    try:
        event = await asyncio.to_thread(get_public_event, public_slug=slug)
    except EventNotFound:
        return _page("Не найдено", "<h1>Мероприятие не найдено</h1>", status=404)
    source = str(request.query.get("source") or "").strip()[:160]
    campaign_ref = str(request.query.get("campaign_ref") or "").strip()[:240]
    return _landing(event, source=source, campaign_ref=campaign_ref)


async def public_event_register(request: web.Request) -> web.Response:
    if request.content_length is not None and request.content_length > EVENT_REGISTRATION_MAX_BODY_BYTES:
        return web.Response(status=413, text="payload_too_large", headers=SECURITY_HEADERS)
    try:
        form = await request.post()
    except (ValueError, UnicodeDecodeError):
        return _page("Ошибка", "<h1>Не удалось прочитать форму</h1>", status=400)

    one = lambda key: str(form.get(key) or "").strip()
    if one("company"):
        # Honeypot: do not persist PII and do not reveal bot detection.
        return _page("Готово", "<h1>Регистрация принята</h1>")
    try:
        with get_db() as conn:
            result = register_public_attendee_in_transaction(
                conn,
                public_slug=str(request.match_info.get("slug") or ""),
                name=one("name"),
                email=one("email"),
                phone=one("phone") or None,
                source=one("source") or None,
                campaign_ref=one("campaign_ref") or None,
                consent=one("consent").lower() in {"yes", "1", "true", "on"},
            )
    except EventUnavailable:
        return _page("Регистрация закрыта", "<h1>Регистрация уже закрыта</h1>", status=410)
    except (ValueError, EventNotFound):
        return _page("Ошибка", "<h1>Проверьте введённые данные</h1>", status=400)

    state = "уже была подтверждена" if not result.created else "подтверждена"
    note = (
        "<p>Организационные письма будут отправлены на указанный e-mail.</p>"
        if result.notifications.enabled
        else "<p>Регистрация сохранена. Организатор сообщит детали входа отдельно.</p>"
    )
    return _page(
        "Готово",
        f"<h1>Регистрация {state}</h1>{note}",
    )


async def public_event_join(request: web.Request) -> web.Response:
    token = str(request.match_info.get("token") or "").strip()
    try:
        with get_db() as conn:
            repository = EventRepository(conn)
            registration = repository.get_registration_by_token(token=token)
            row = conn.execute(
                "SELECT public_slug FROM clientplatform_events WHERE id=? AND business_id=? LIMIT 1",
                (registration.event_id, registration.business_id),
            ).fetchone()
            if row is None:
                raise EventNotFound("event not found")
            slug = str(row["public_slug"] if hasattr(row, "keys") else row[0])
            event = repository.get_public_owner_event(public_slug=slug)
            repository.mark_join_click(
                registration=registration,
                event=event,
            )
            location = event.join_url
    except EventNotFound:
        return _page("Ссылка недействительна", "<h1>Ссылка недействительна</h1>", status=404)
    return web.Response(
        status=302,
        headers={**SECURITY_HEADERS, "Location": location},
    )


async def public_event_offer(request: web.Request) -> web.Response:
    token = str(request.match_info.get("token") or "").strip()
    try:
        with get_db() as conn:
            repository = EventRepository(conn)
            registration = repository.get_registration_by_token(token=token)
            row = conn.execute(
                "SELECT public_slug FROM clientplatform_events WHERE id=? AND business_id=? LIMIT 1",
                (registration.event_id, registration.business_id),
            ).fetchone()
            if row is None:
                raise EventNotFound("event not found")
            event = repository.get_public_owner_event(
                public_slug=str(row["public_slug"] if hasattr(row, "keys") else row[0])
            )
            if not event.offer_url:
                raise EventNotFound("offer not found")
            repository.mark_offer_clicked(registration=registration, event=event)
            location = event.offer_url
    except EventNotFound:
        return _page("Недоступно", "<h1>Предложение недоступно</h1>", status=404)
    return web.Response(
        status=302,
        headers={**SECURITY_HEADERS, "Location": location},
    )


def register_public_event_routes(app: web.Application) -> None:
    app.router.add_get("/e/{slug}", public_event_landing)
    app.router.add_post("/e/{slug}/register", public_event_register)
    app.router.add_get("/e/join/{token}", public_event_join)
    app.router.add_get("/e/offer/{token}", public_event_offer)
    app["clientplatform_event_ingress"] = True


__all__ = [
    "EVENT_REGISTRATION_MAX_BODY_BYTES",
    "SECURITY_HEADERS",
    "public_event_join",
    "public_event_landing",
    "public_event_offer",
    "public_event_register",
    "register_public_event_routes",
]
