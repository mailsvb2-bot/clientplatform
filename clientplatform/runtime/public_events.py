from __future__ import annotations

import asyncio
import logging
from html import escape
from urllib.parse import quote
from aiohttp import web

from config.settings import settings

from clientplatform.application.event_commercial_consent import (
    grant_event_commercial_consent_in_transaction,
    normalize_marketing_channels,
    public_event_advertiser_label_in_transaction,
    revoke_event_commercial_consent_by_registration_token_in_transaction,
)
from clientplatform.application.event_public_surface import (
    SECURITY_HEADERS,
    render_event_landing_body,
)
from clientplatform.application.event_registration_channels import (
    issue_event_registration_channel_links_in_transaction,
)
from clientplatform.application.event_sessions import select_event_session_for_join
from clientplatform.application.events import (
    EventUnavailable,
    get_public_event,
    register_public_attendee_in_transaction,
)
from clientplatform.infrastructure.event_repository import EventNotFound, EventRepository
from clientplatform.infrastructure.event_session_repository import EventSessionRepository
from services.db import get_db, get_db_ro
from services.db.core import ambient_savepoint
from services.messenger.links import build_entry_targets


LOGGER = logging.getLogger(__name__)
EVENT_REGISTRATION_MAX_BODY_BYTES = 32 * 1024



def _page(title: str, body: str, *, status: int = 200) -> web.Response:
    html = (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;"
        "padding:0 18px;line-height:1.5}.card{border:1px solid #ddd;border-radius:18px;"
        "padding:28px}input,button{width:100%;box-sizing:border-box;padding:13px;"
        "margin:7px 0;font-size:16px}button{font-weight:700;cursor:pointer}"
        ".channel-link{display:block;padding:12px 14px;margin:8px 0;border:1px solid #ccc;"
        "border-radius:12px;text-decoration:none;color:inherit;font-weight:700}.hp{position:absolute;"
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


def _landing(
    event,
    *,
    source: str = "",
    campaign_ref: str = "",
    advertiser_label: str | None = None,
) -> web.Response:
    return _page(
        event.title,
        render_event_landing_body(
            event,
            source=source,
            campaign_ref=campaign_ref,
            advertiser_label=advertiser_label,
        ),
    )


def _public_advertiser_label(public_slug: str) -> str | None:
    with get_db_ro() as conn:
        return public_event_advertiser_label_in_transaction(
            conn, public_slug=public_slug
        )


def _registration_channel_entry_url(
    conn,
    *,
    business_id: str,
    platform: str,
    payload: str,
) -> str | None:
    if platform == "telegram":
        managed = conn.execute(
            """
            SELECT c.id,mb.username
            FROM connections c
            JOIN managed_bots mb
              ON mb.connection_id=c.id AND mb.business_id=c.business_id
             AND mb.platform='telegram' AND mb.status='active'
            WHERE c.business_id=? AND c.platform='telegram'
              AND c.connection_type='telegram_managed_bot'
              AND c.status='active'
              AND mb.username IS NOT NULL AND TRIM(mb.username)!=''
            ORDER BY c.created_at,c.id
            LIMIT 2
            """,
            (business_id,),
        ).fetchall()
        if len(managed) == 1:
            username = str(
                managed[0]["username"] if hasattr(managed[0], "keys") else managed[0][1]
            ).strip().lstrip("@")
            if username:
                return f"https://t.me/{quote(username, safe='')}?start={quote(payload, safe='')}"
        shared = conn.execute(
            """
            SELECT id
            FROM connections
            WHERE business_id=? AND platform='telegram'
              AND connection_type='telegram_shared_bot' AND status='active'
            ORDER BY created_at,id
            LIMIT 2
            """,
            (business_id,),
        ).fetchall()
        if len(shared) != 1:
            return None

    elif platform == "vk":
        rows = conn.execute(
            """
            SELECT external_account_id
            FROM connections
            WHERE business_id=? AND platform='vk'
              AND connection_type='vk_community' AND status='active'
            ORDER BY created_at,id
            LIMIT 2
            """,
            (business_id,),
        ).fetchall()
        if len(rows) != 1:
            return None
        group_id = str(
            rows[0]["external_account_id"] if hasattr(rows[0], "keys") else rows[0][0]
        ).strip().lstrip("-")
        if not group_id.isdigit() or int(group_id) <= 0:
            return None
        return f"https://vk.com/im?sel=-{group_id}&start={quote(payload, safe='')}"

    elif platform == "max":
        managed = conn.execute(
            """
            SELECT mb.username
            FROM connections c
            JOIN managed_bots mb
              ON mb.connection_id=c.id AND mb.business_id=c.business_id
             AND mb.platform='max' AND mb.status='active'
            WHERE c.business_id=? AND c.platform='max'
              AND c.connection_type='max_personal_bot' AND c.status='active'
              AND mb.username IS NOT NULL AND TRIM(mb.username)!=''
            ORDER BY c.created_at,c.id
            LIMIT 2
            """,
            (business_id,),
        ).fetchall()
        if len(managed) == 1:
            bot_name = str(
                managed[0]["username"] if hasattr(managed[0], "keys") else managed[0][0]
            ).strip().lstrip("@")
            base = str(getattr(settings, "MAX_BOT_LINK_BASE", "") or "").strip()
            if bot_name and base:
                rendered = base.replace("{bot}", quote(bot_name, safe=""))
                encoded = quote(payload, safe="")
                if "{payload}" in rendered:
                    rendered = rendered.replace("{payload}", encoded)
                    if "{" not in rendered and "}" not in rendered:
                        return rendered
                elif "{" not in rendered and "}" not in rendered:
                    separator = "&" if "?" in rendered else "?"
                    return f"{rendered}{separator}start={encoded}"
        shared = conn.execute(
            """
            SELECT id
            FROM connections
            WHERE business_id=? AND platform='max'
              AND connection_type='max_shared_bot' AND status='active'
            ORDER BY created_at,id
            LIMIT 2
            """,
            (business_id,),
        ).fetchall()
        if len(shared) != 1:
            return None
    else:
        return None

    target = next(
        (
            item
            for item in build_entry_targets(payload)
            if item.get("platform") == platform
        ),
        None,
    )
    if target is None:
        return None
    url = str(target.get("url") or "").strip()
    return url or None


async def public_event_landing(request: web.Request) -> web.Response:
    slug = str(request.match_info.get("slug") or "").strip()
    try:
        event = await asyncio.to_thread(get_public_event, public_slug=slug)
    except EventNotFound:
        return _page("Не найдено", "<h1>Мероприятие не найдено</h1>", status=404)
    source = str(request.query.get("source") or "").strip()[:160]
    campaign_ref = str(request.query.get("campaign_ref") or "").strip()[:240]
    advertiser_label = await asyncio.to_thread(_public_advertiser_label, slug)
    return _landing(
        event,
        source=source,
        campaign_ref=campaign_ref,
        advertiser_label=advertiser_label,
    )


async def public_event_register(request: web.Request) -> web.Response:
    if request.content_length is not None and request.content_length > EVENT_REGISTRATION_MAX_BODY_BYTES:
        return web.Response(status=413, text="payload_too_large", headers=SECURITY_HEADERS)
    try:
        form = await request.post()
    except (ValueError, UnicodeDecodeError):
        return _page("Ошибка", "<h1>Не удалось прочитать форму</h1>", status=400)

    one = lambda key: str(form.get(key) or "").strip()
    if one("company"):
        return _page("Готово", "<h1>Регистрация принята</h1>")
    marketing_requested = one("marketing_consent").lower() in {"yes", "1", "true", "on"}
    marketing_channels: tuple[str, ...] = ()
    if marketing_requested:
        try:
            marketing_channels = normalize_marketing_channels(
                form.getall("marketing_channel", []),
                public_form=True,
            )
        except ValueError:
            return _page(
                "Ошибка",
                "<h1>Выберите хотя бы один канал для рекламных сообщений</h1>",
                status=400,
            )
    registration_channel_links = ()
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
            marketing_recorded = False
            email_marketing_requested = "email" in marketing_channels
            if marketing_requested and result.created and email_marketing_requested:
                try:
                    with ambient_savepoint(conn):
                        grant_event_commercial_consent_in_transaction(
                            conn,
                            business_id=result.registration.business_id,
                            event_id=result.registration.event_id,
                            registration_id=result.registration.id,
                            channels=("email",),
                            expected_text_sha256=one("marketing_consent_hash"),
                        )
                    marketing_recorded = True
                except Exception:  # validator: allow-wide-except
                    LOGGER.exception(
                        "event e-mail commercial consent persistence failed; follow-up stays disabled",
                        extra={
                            "business_id": result.registration.business_id,
                            "event_id": result.registration.event_id,
                        },
                    )
            try:
                with ambient_savepoint(conn):
                    registration_channel_links = (
                        issue_event_registration_channel_links_in_transaction(
                            conn,
                            registration=result.registration,
                            marketing_platforms=(
                                tuple(
                                    channel
                                    for channel in marketing_channels
                                    if channel in {"telegram", "vk", "max"}
                                )
                                if result.created
                                else ()
                            ),
                            expected_marketing_text_sha256=(
                                one("marketing_consent_hash")
                                if marketing_requested and result.created
                                else None
                            ),
                        )
                    )
            except Exception:  # validator: allow-wide-except - registration remains durable
                LOGGER.exception(
                    "event registration-scoped messenger link issuance failed",
                    extra={
                        "business_id": result.registration.business_id,
                        "event_id": result.registration.event_id,
                    },
                )
    except EventUnavailable:
        return _page("Регистрация закрыта", "<h1>Регистрация уже закрыта</h1>", status=410)
    except (ValueError, EventNotFound):
        return _page("Ошибка", "<h1>Проверьте введённые данные</h1>", status=400)

    state = "уже была подтверждена" if not result.created else "подтверждена"
    requested_messenger_marketing = tuple(
        channel for channel in marketing_channels if channel in {"telegram", "vk", "max"}
    )
    if marketing_requested and not result.created:
        marketing_note = (
            "<p>Повторная регистрация не изменяет рекламное согласие. "
            "Для управления им используйте персональную ссылку из сообщения.</p>"
        )
    elif marketing_requested:
        parts = []
        if marketing_recorded:
            parts.append("E-mail подтверждён")
        if requested_messenger_marketing:
            parts.append(
                "выбранные мессенджеры включатся для предложений только после "
                "подтверждения одноразовой кнопкой ниже"
            )
        marketing_note = (
            "<p>" + escape("; ".join(parts)) + ". В каждом рекламном сообщении будет ссылка для отказа.</p>"
            if parts
            else "<p>Согласие на рекламные сообщения не было сохранено.</p>"
        )
    else:
        marketing_note = ""
    channel_labels = {
        "email": "E-mail",
        "telegram": "Telegram",
        "vk": "VK",
        "max": "MAX",
    }
    active_channels = [
        channel_labels.get(channel, channel)
        for channel in tuple(getattr(result.notifications, "channels", ()) or ())
    ]
    note = (
        "<p>Организационные напоминания будут отправлены: "
        + escape(", ".join(active_channels))
        + ".</p>"
        if result.notifications.enabled and active_channels
        else "<p>Регистрация сохранена. Организатор сообщит детали входа отдельно.</p>"
    )
    entry_by_platform: dict[str, str] = {}
    marketing_by_platform: dict[str, bool] = {}
    if registration_channel_links:
        with get_db_ro() as conn:
            for issued in registration_channel_links:
                payload = f"ecv_{issued.token}"
                url = _registration_channel_entry_url(
                    conn,
                    business_id=result.registration.business_id,
                    platform=issued.platform,
                    payload=payload,
                )
                if url:
                    entry_by_platform[issued.platform] = url
                    marketing_by_platform[issued.platform] = bool(
                        issued.marketing_requested
                    )
    labels = {"telegram": "Telegram", "vk": "ВКонтакте", "max": "MAX"}
    reminder_opt_in = ""
    if entry_by_platform:
        links = "".join(
            (
                f"<a class='channel-link' href='{escape(url, quote=True)}'>"
                + (
                    f"Подтвердить {escape(labels[platform])}: напоминания + предложения"
                    if marketing_by_platform.get(platform)
                    else f"Получать напоминания в {escape(labels[platform])}"
                )
                + "</a>"
            )
            for platform, url in entry_by_platform.items()
        )
        reminder_opt_in = (
            "<h2>Подтвердить мессенджер</h2>"
            "<p>Ник или ID вводить не нужно: откроется выбранный мессенджер. "
            "Подтверждение относится только к этой регистрации и не объединяет "
            "аккаунт с чужой CRM-карточкой по введённому e-mail. "
            "Ссылка одноразовая и действует ограниченное время.</p>"
            + links
        )
    return _page(
        "Готово",
        f"<h1>Регистрация {state}</h1>{note}{reminder_opt_in}{marketing_note}",
    )


async def public_event_marketing_unsubscribe(request: web.Request) -> web.Response:
    token = str(request.match_info.get("token") or "").strip()
    if not token:
        return _page("Ссылка недействительна", "<h1>Ссылка недействительна</h1>", status=404)
    action = f"/e/marketing/unsubscribe/{token}"
    return _page(
        "Отключить рекламные сообщения",
        "<h1>Отключить рекламные сообщения?</h1>"
        "<p>Организационные сообщения по уже зарегистрированному мероприятию останутся включены.</p>"
        f"<form method=post action='{escape(action, quote=True)}'>"
        "<button type=submit>Отключить рекламные сообщения</button></form>",
    )


async def public_event_marketing_unsubscribe_confirm(request: web.Request) -> web.Response:
    token = str(request.match_info.get("token") or "").strip()
    try:
        with get_db() as conn:
            revoke_event_commercial_consent_by_registration_token_in_transaction(
                conn, token=token
            )
    except ValueError:
        return _page("Ссылка недействительна", "<h1>Ссылка недействительна</h1>", status=404)
    return _page(
        "Рассылка отключена",
        "<h1>Рекламные сообщения отключены</h1>"
        "<p>Организационные сообщения по уже зарегистрированному мероприятию могут продолжать приходить.</p>",
    )


async def public_event_join(request: web.Request) -> web.Response:
    token = str(request.match_info.get("token") or "").strip()
    raw_position = str(request.match_info.get("position") or "").strip()
    try:
        position = int(raw_position) if raw_position else None
        if position is not None and position < 1:
            raise ValueError("invalid session position")
    except ValueError:
        return _page("Ссылка недействительна", "<h1>Ссылка недействительна</h1>", status=404)

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
            sessions = EventSessionRepository(conn).list_for_event_record(event=event)
            try:
                session = select_event_session_for_join(sessions, position=position)
            except (LookupError, ValueError) as exc:
                raise EventNotFound("event session not found") from exc
            if not session.join_is_ready or not session.join_url:
                return _page(
                    "Ссылка на эфир ещё не добавлена",
                    "<h1>Ссылка на эфир появится здесь позже</h1>"
                    "<p>Регистрация сохранена. Откройте эту же персональную ссылку ближе к началу мероприятия.</p>",
                    status=200,
                )
            repository.mark_join_click(
                registration=registration,
                event=event,
            )
            location = session.join_url
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
    app.router.add_get(
        "/e/marketing/unsubscribe/{token}", public_event_marketing_unsubscribe
    )
    app.router.add_post(
        "/e/marketing/unsubscribe/{token}", public_event_marketing_unsubscribe_confirm
    )
    app.router.add_get("/e/join/{token}/{position}", public_event_join)
    app.router.add_get("/e/join/{token}", public_event_join)
    app.router.add_get("/e/offer/{token}", public_event_offer)
    app["clientplatform_event_ingress"] = True


__all__ = [
    "EVENT_REGISTRATION_MAX_BODY_BYTES",
    "SECURITY_HEADERS",
    "public_event_join",
    "public_event_landing",
    "public_event_marketing_unsubscribe",
    "public_event_marketing_unsubscribe_confirm",
    "public_event_offer",
    "public_event_register",
    "register_public_event_routes",
]
