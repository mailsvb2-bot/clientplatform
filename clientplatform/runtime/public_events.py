from __future__ import annotations

import asyncio
import logging
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo
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
from clientplatform.application.event_landing_builder import (
    get_public_event_landing,
    get_public_event_landing_preview,
)
from clientplatform.application.event_registration_channels import (
    issue_event_registration_channel_links_in_transaction,
)
from clientplatform.application.event_conference_join import (
    issue_managed_event_session_join,
    is_managed_event_session_provider,
)
from clientplatform.application.event_sessions import select_event_session_for_join
from clientplatform.application.events import (
    EventUnavailable,
    cancel_public_registration_by_token_in_transaction,
    get_public_event,
    register_public_attendee_in_transaction,
)
from clientplatform.infrastructure.event_repository import EventNotFound, EventRepository
from clientplatform.infrastructure.event_session_repository import EventSessionRepository
from clientplatform.runtime.conference_provider import ConferenceProviderError
from clientplatform.runtime.ucr_gateway import UcrGatewayError
from services.db import get_db, get_db_ro
from services.db.core import ambient_savepoint
from services.messenger.links import build_entry_targets


LOGGER = logging.getLogger(__name__)
EVENT_REGISTRATION_MAX_BODY_BYTES = 32 * 1024



def _page(
    title: str,
    body: str,
    *,
    status: int = 200,
    extra_headers: dict[str, str] | None = None,
) -> web.Response:
    html = (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title>"
        "<style>"
        "*{box-sizing:border-box}html{scroll-behavior:smooth}"
        "body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "margin:0;background:#f6f7fb;color:#172033;line-height:1.55}"
        ".card{max-width:1040px;margin:0 auto;padding:32px 20px 64px}"
        "input,button{width:100%;box-sizing:border-box;padding:13px 14px;margin:7px 0;font-size:16px;"
        "border-radius:12px;border:1px solid #ccd2df;background:white;color:inherit}"
        "button{font-weight:750;cursor:pointer;background:#1f55e5;color:white;border-color:#1f55e5}"
        "fieldset{border:0;padding:0;margin:0 0 16px}legend{font-size:20px;font-weight:800;margin-bottom:10px}"
        "label{display:block;margin-top:8px}.channel-link{display:block;padding:12px 14px;margin:8px 0;"
        "border:1px solid #ccd2df;border-radius:12px;text-decoration:none;color:inherit;font-weight:700}"
        ".hp{position:absolute;left:-10000px}.landing{--accent:#315bea;--accent2:#7b61ff;--surface:#fff;"
        "--soft:#edf2ff;--ink:#15213a}.landing--bold{--accent:#c63d12;--accent2:#ff8a00;--soft:#fff0e7}"
        ".landing--minimal{--accent:#202532;--accent2:#697386;--soft:#f1f2f4}"
        ".landing-hero{padding:56px clamp(22px,5vw,64px);border-radius:28px;background:linear-gradient(135deg,var(--soft),#fff);"
        "border:1px solid #e2e6ef;box-shadow:0 18px 50px rgba(30,45,80,.08);margin-bottom:22px}"
        ".landing-eyebrow{text-transform:uppercase;letter-spacing:.08em;font-size:13px;font-weight:850;color:var(--accent)}"
        ".landing h1{font-size:clamp(36px,6vw,64px);line-height:1.02;letter-spacing:-.035em;margin:14px 0 18px;max-width:900px}"
        ".landing-lead{font-size:clamp(18px,2.5vw,24px);max-width:780px;color:#46516a}"
        ".landing-date{font-size:18px;margin:24px 0}.landing-cta{display:inline-block;padding:14px 22px;border-radius:14px;"
        "background:linear-gradient(135deg,var(--accent),var(--accent2));color:white;text-decoration:none;font-weight:850}"
        ".landing-section{background:var(--surface);border:1px solid #e2e6ef;border-radius:22px;padding:28px;"
        "margin:18px 0;box-shadow:0 8px 30px rgba(30,45,80,.04)}"
        ".landing-section h2{font-size:clamp(25px,3vw,34px);line-height:1.15;margin:0 0 18px}"
        ".landing-points{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:0;list-style:none}"
        ".landing-points li{padding:16px;border-radius:15px;background:var(--soft);font-weight:650}"
        ".landing-faq details{border-top:1px solid #e5e8ef;padding:14px 0}.landing-faq summary{cursor:pointer;font-weight:800}"
        ".landing-registration{background:#fff;border:1px solid #dfe4ee;border-radius:24px;padding:28px;margin-top:18px;"
        "box-shadow:0 16px 45px rgba(30,45,80,.08)}.landing-final-cta{text-align:center}"
        ".preview-banner{padding:12px 16px;border-radius:14px;background:#fff7d6;border:1px solid #ead47b;margin-bottom:16px;font-weight:750}"
        "@media(max-width:680px){.card{padding:16px 12px 40px}.landing-hero{padding:34px 20px;border-radius:20px}"
        ".landing-section,.landing-registration{padding:20px;border-radius:18px}.landing-points{grid-template-columns:1fr}}"
        "</style></head><body><main class=card>"
        + body
        + "</main></body></html>"
    )
    headers = dict(SECURITY_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    return web.Response(
        status=status,
        text=html,
        content_type="text/html",
        charset="utf-8",
        headers=headers,
    )


def _landing(
    event,
    *,
    source: str = "",
    campaign_ref: str = "",
    advertiser_label: str | None = None,
    landing=None,
    preview: bool = False,
) -> web.Response:
    body = render_event_landing_body(
        event,
        source=source,
        campaign_ref=campaign_ref,
        advertiser_label=advertiser_label,
        landing=landing,
        registration_enabled=not preview,
    )
    if preview:
        body = (
            "<div class=preview-banner>Предпросмотр черновика. Эта версия ещё не опубликована.</div>"
            + body
        )
    return _page(
        event.title,
        body,
        extra_headers={"X-Robots-Tag": "noindex, nofollow"} if preview else None,
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
    advertiser_label, landing = await asyncio.gather(
        asyncio.to_thread(_public_advertiser_label, slug),
        asyncio.to_thread(get_public_event_landing, public_slug=slug),
    )
    return _landing(
        event,
        source=source,
        campaign_ref=campaign_ref,
        advertiser_label=advertiser_label,
        landing=landing,
    )


async def public_event_landing_preview(request: web.Request) -> web.Response:
    slug = str(request.match_info.get("slug") or "").strip()
    token = str(request.match_info.get("token") or "").strip()
    try:
        event = await asyncio.to_thread(get_public_event, public_slug=slug)
    except EventNotFound:
        return _page("Не найдено", "<h1>Мероприятие не найдено</h1>", status=404)
    landing = await asyncio.to_thread(
        get_public_event_landing_preview,
        public_slug=slug,
        token=token,
    )
    if landing is None:
        return _page(
            "Предпросмотр недоступен",
            "<h1>Ссылка предпросмотра устарела</h1>",
            status=404,
            extra_headers={"X-Robots-Tag": "noindex, nofollow"},
        )
    advertiser_label = await asyncio.to_thread(_public_advertiser_label, slug)
    return _landing(
        event,
        advertiser_label=advertiser_label,
        landing=landing,
        preview=True,
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
            if result.created:
                try:
                    with ambient_savepoint(conn):
                        registration_channel_links = (
                            issue_event_registration_channel_links_in_transaction(
                                conn,
                                registration=result.registration,
                                marketing_platforms=tuple(
                                    channel
                                    for channel in marketing_channels
                                    if channel in {"telegram", "vk", "max"}
                                ),
                                expected_marketing_text_sha256=(
                                    one("marketing_consent_hash")
                                    if marketing_requested
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
    manage_token = str(getattr(result.registration, "token", "") or "").strip()
    manage_note = ""
    if manage_token:
        manage_path = f"/e/manage/{quote(manage_token, safe='')}"
        manage_note = (
            "<p><a class='channel-link' href='"
            + escape(manage_path, quote=True)
            + "'>Моя регистрация и расписание</a></p>"
        )
    return _page(
        "Готово",
        f"<h1>Регистрация {state}</h1>{note}{reminder_opt_in}{marketing_note}{manage_note}",
    )


def _registration_surface(token: str):
    with get_db_ro() as conn:
        repository = EventRepository(conn)
        registration = repository.get_registration_by_token(token=token)
        row = conn.execute(
            "SELECT public_slug FROM clientplatform_events "
            "WHERE id=? AND business_id=? AND status='published' LIMIT 1",
            (registration.event_id, registration.business_id),
        ).fetchone()
        if row is None:
            raise EventNotFound("event not found")
        public_slug = str(row["public_slug"] if hasattr(row, "keys") else row[0])
        event = repository.get_public_owner_event(public_slug=public_slug)
        sessions = EventSessionRepository(conn).list_for_event_record(event=event)
        verified_rows = conn.execute(
            """
            SELECT platform
            FROM clientplatform_event_registration_channels
            WHERE business_id=? AND event_id=? AND registration_id=?
            ORDER BY platform
            """,
            (registration.business_id, registration.event_id, registration.id),
        ).fetchall()
        verified = tuple(
            str(item["platform"] if hasattr(item, "keys") else item[0])
            for item in verified_rows
        )
        return registration, event, sessions, verified


def _manage_body(
    token: str,
    registration,
    event,
    sessions,
    verified: tuple[str, ...],
) -> str:
    labels = {"telegram": "Telegram", "vk": "ВКонтакте", "max": "MAX"}
    schedule = "".join(
        "<li>"
        + escape(
            f"День {session.position}: "
            + session.starts_at.astimezone(ZoneInfo(event.timezone_name)).strftime(
                "%d.%m.%Y %H:%M"
            )
        )
        + f" — <a href='/e/join/{quote(token, safe='')}/{session.position}'>Войти</a></li>"
        for session in sessions
    )
    if not schedule:
        schedule = "<li>Расписание появится здесь после настройки организатором.</li>"
    channels = (
        ", ".join(labels.get(item, item) for item in verified)
        if verified
        else "мессенджеры пока не подтверждены"
    )
    cancel_path = f"/e/manage/{quote(token, safe='')}/cancel"
    return (
        f"<h1>{escape(event.title)}</h1>"
        f"<p>{escape(registration.name)}, это Ваша персональная страница регистрации.</p>"
        f"<h2>Расписание</h2><ul>{schedule}</ul>"
        f"<p><b>Подтверждённые мессенджеры:</b> {escape(channels)}</p>"
        f"<form method=post action='{escape(cancel_path, quote=True)}'>"
        "<button type=submit>Отменить мою регистрацию</button></form>"
        "<p>После отмены будущие организационные и рекламные сообщения "
        "по этому вебинару будут остановлены.</p>"
    )


async def public_event_manage(request: web.Request) -> web.Response:
    token = str(request.match_info.get("token") or "").strip()
    try:
        registration, event, sessions, verified = await asyncio.to_thread(
            _registration_surface,
            token,
        )
    except EventNotFound:
        return _page(
            "Ссылка недействительна",
            "<h1>Регистрация не найдена</h1>",
            status=404,
        )
    return _page(
        "Моя регистрация",
        _manage_body(token, registration, event, sessions, verified),
    )


async def public_event_cancel_registration(request: web.Request) -> web.Response:
    token = str(request.match_info.get("token") or "").strip()
    try:
        with get_db() as conn:
            cancel_public_registration_by_token_in_transaction(conn, token=token)
    except (EventNotFound, EventUnavailable, ValueError):
        return _page(
            "Ссылка недействительна",
            "<h1>Регистрация уже недоступна</h1>",
            status=404,
        )
    return _page(
        "Регистрация отменена",
        "<h1>Регистрация отменена</h1>"
        "<p>Будущие организационные и рекламные сообщения по этому вебинару остановлены.</p>",
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


def _public_event_join_context(token: str, position: int | None):
    with get_db_ro() as conn:
        repository = EventRepository(conn)
        registration = repository.get_registration_by_token(token=token)
        row = conn.execute(
            "SELECT public_slug FROM clientplatform_events "
            "WHERE id=? AND business_id=? AND status='published' LIMIT 1",
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
        return registration, event, session


def _mark_public_event_join_click(*, token: str, expected_registration_id: str) -> None:
    with get_db() as conn:
        repository = EventRepository(conn)
        registration = repository.get_registration_by_token(token=token)
        if registration.id != expected_registration_id:
            raise EventNotFound("registration token changed")
        row = conn.execute(
            "SELECT public_slug FROM clientplatform_events "
            "WHERE id=? AND business_id=? AND status='published' LIMIT 1",
            (registration.event_id, registration.business_id),
        ).fetchone()
        if row is None:
            raise EventNotFound("event not found")
        event = repository.get_public_owner_event(
            public_slug=str(row["public_slug"] if hasattr(row, "keys") else row[0])
        )
        if not repository.authorize_join_redirect(
            registration=registration,
            event=event,
        ):
            raise EventNotFound("registration or event is no longer joinable")


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
        registration, event, session = await asyncio.to_thread(
            _public_event_join_context,
            token,
            position,
        )
    except EventNotFound:
        return _page("Ссылка недействительна", "<h1>Ссылка недействительна</h1>", status=404)

    if is_managed_event_session_provider(session):
        try:
            join = await issue_managed_event_session_join(
                registration=registration,
                event=event,
                session=session,
            )
        except (ConferenceProviderError, UcrGatewayError, ValueError):
            LOGGER.warning(
                "managed event conference join unavailable",
                extra={
                    "business_id": registration.business_id,
                    "event_id": registration.event_id,
                    "session_id": session.id,
                    "provider_key": session.provider_key,
                },
            )
            return _page(
                "Эфир временно недоступен",
                "<h1>Не удалось открыть эфир</h1>"
                "<p>Регистрация сохранена. Попробуйте открыть эту же персональную ссылку ещё раз.</p>",
                status=503,
            )
        location = join.url
    else:
        if not session.join_is_ready or not session.join_url:
            return _page(
                "Ссылка на эфир ещё не добавлена",
                "<h1>Ссылка на эфир появится здесь позже</h1>"
                "<p>Регистрация сохранена. Откройте эту же персональную ссылку ближе к началу мероприятия.</p>",
                status=200,
            )
        location = session.join_url

    try:
        await asyncio.to_thread(
            _mark_public_event_join_click,
            token=token,
            expected_registration_id=registration.id,
        )
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
    app.router.add_get("/e/{slug}/preview/{token}", public_event_landing_preview)
    app.router.add_post("/e/{slug}/register", public_event_register)
    app.router.add_get("/e/manage/{token}", public_event_manage)
    app.router.add_post("/e/manage/{token}/cancel", public_event_cancel_registration)
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
    "public_event_cancel_registration",
    "public_event_join",
    "public_event_manage",
    "public_event_landing",
    "public_event_landing_preview",
    "public_event_marketing_unsubscribe",
    "public_event_marketing_unsubscribe_confirm",
    "public_event_offer",
    "public_event_register",
    "register_public_event_routes",
]
