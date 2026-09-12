from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from uuid import UUID

from clientplatform.application.activity import get_business_profile
from clientplatform.application.cockpit import resolve_cockpit_context
from clientplatform.application.event_analytics import get_event_funnel_in_transaction
from clientplatform.application.event_followups import commercial_event_followups_enabled
from clientplatform.application.event_growth import get_event_acquisition_breakdown_in_transaction
from clientplatform.application.event_owner_flow import (
    OnlineEventCreateRequest,
    create_and_publish_online_event_in_transaction,
)
from clientplatform.application.events import cancel_event
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.bookings import parse_local_booking_start
from clientplatform.domain.events import validate_external_https_url
from clientplatform.domain.money import settlement_currency_minor_unit_exponent
from clientplatform.domain.tenancy import TenantAccessDenied, TenantContext, TenantPermissionDenied
from clientplatform.infrastructure.event_repository import EventRepository
from config.settings import settings
from services.db import get_db_ro
from services.db.core import atomic_db

_SCHEMA_VERSION = "2026-09-12.events.v1"


@dataclass(frozen=True, slots=True)
class CockpitEventRevenue:
    currency: str
    amount_minor: int
    display: str


@dataclass(frozen=True, slots=True)
class CockpitEventAcquisition:
    source: str
    campaign_ref: str | None
    registered: int
    paid: int


@dataclass(frozen=True, slots=True)
class CockpitEventItem:
    id: str
    title: str
    status: str
    starts_at: str
    local_start: str
    provider_key: str
    registration_url: str | None
    has_offer: bool
    email_notifications_enabled: bool
    registered: int
    join_clicked: int
    attendance_confirmed: int
    offer_clicked: int
    paid: int
    commercial_consents: int
    revenue: tuple[CockpitEventRevenue, ...]
    acquisition: tuple[CockpitEventAcquisition, ...]


@dataclass(frozen=True, slots=True)
class CockpitEventsSnapshot:
    schema_version: str
    business_id: str
    business_name: str
    timezone_name: str
    can_manage: bool
    commercial_followups_enabled: bool
    items: tuple[CockpitEventItem, ...]
    limitations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _public_base_url(*, required: bool) -> str | None:
    value = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if value.startswith("https://"):
        return value
    if required:
        raise ValueError("публичный HTTPS-адрес ClientPlatform пока не настроен")
    return None


def _money_display(amount_minor: int, currency: str) -> str:
    exponent = settlement_currency_minor_unit_exponent(currency)
    amount = Decimal(int(amount_minor)) / (Decimal(10) ** exponent)
    rendered = f"{amount:,.{exponent}f}" if exponent else f"{amount:,.0f}"
    return f"{rendered.replace(',', ' ')} {currency}"


def _resolve_actor(
    *, telegram_user_id: int, requested_business_id: str | None
) -> tuple[TenantContext, str]:
    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if context.onboarding_required or context.business_id is None or context.business_name is None:
        raise TenantAccessDenied("active business membership was not found")
    actor = resolve_tenant_context(user_id=context.user_id, business_id=context.business_id)
    actor.assert_can_view_customer_records()
    actor.assert_can_view_outcome_ledger()
    actor.assert_can_view_attribution_spine()
    return actor, context.business_name


def _can_manage(actor: TenantContext) -> bool:
    try:
        actor.assert_can_manage_business()
    except TenantPermissionDenied:
        return False
    return True


def _active_commercial_consents(conn: object, *, business_id: str, event_id: str) -> int:
    row = conn.execute(  # type: ignore[attr-defined]
        """
        SELECT COUNT(DISTINCT registration_id)
        FROM clientplatform_event_commercial_channel_state
        WHERE business_id=? AND event_id=? AND status='active'
        """,
        (business_id, event_id),
    ).fetchone()
    return int((row[0] if row is not None else 0) or 0)


def resolve_cockpit_events(
    *, telegram_user_id: int, requested_business_id: str | None = None, limit: int = 30
) -> CockpitEventsSnapshot:
    actor, business_name = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    profile = get_business_profile(actor=actor)
    bounded_limit = max(1, min(int(limit), 100))
    public_base = _public_base_url(required=False)
    limitations: list[str] = []
    if public_base is None:
        limitations.append("Публичный HTTPS-адрес не настроен; ссылки регистрации временно скрыты.")

    with get_db_ro() as conn:
        events = EventRepository(conn).list(actor=actor, limit=bounded_limit)
        items: list[CockpitEventItem] = []
        for event in events:
            funnel = get_event_funnel_in_transaction(conn, actor=actor, event_id=event.id)
            acquisition = get_event_acquisition_breakdown_in_transaction(
                conn, actor=actor, event_id=event.id
            )
            items.append(
                CockpitEventItem(
                    id=event.id,
                    title=event.title,
                    status=event.status,
                    starts_at=event.starts_at.isoformat(),
                    local_start=event.local_start_label(),
                    provider_key=event.provider_key,
                    registration_url=(
                        None if public_base is None else f"{public_base}/e/{event.public_slug}"
                    ),
                    has_offer=bool(event.offer_url),
                    email_notifications_enabled=event.notification_connection_id is not None,
                    registered=funnel.registered,
                    join_clicked=funnel.join_clicked,
                    attendance_confirmed=funnel.attendance_confirmed,
                    offer_clicked=funnel.offer_clicked,
                    paid=funnel.paid,
                    commercial_consents=_active_commercial_consents(
                        conn, business_id=event.business_id, event_id=event.id
                    ),
                    revenue=tuple(
                        CockpitEventRevenue(
                            currency=row.currency,
                            amount_minor=row.amount_minor,
                            display=_money_display(row.amount_minor, row.currency),
                        )
                        for row in funnel.revenue
                    ),
                    acquisition=tuple(
                        CockpitEventAcquisition(
                            source=row.source,
                            campaign_ref=row.campaign_ref,
                            registered=row.registered,
                            paid=row.paid,
                        )
                        for row in acquisition[:8]
                    ),
                )
            )
    return CockpitEventsSnapshot(
        schema_version=_SCHEMA_VERSION,
        business_id=actor.business_id,
        business_name=business_name,
        timezone_name=profile.timezone,
        can_manage=_can_manage(actor),
        commercial_followups_enabled=commercial_event_followups_enabled(),
        items=tuple(items),
        limitations=tuple(limitations),
    )


def _request_hash(*, request: OnlineEventCreateRequest) -> str:
    canonical = {
        "description": request.description,
        "enable_email_notifications": bool(request.enable_email_notifications),
        "ends_at": None if request.ends_at is None else request.ends_at.isoformat(),
        "join_url": request.join_url,
        "kind": request.kind,
        "offer_url": request.offer_url,
        "provider_key": request.provider_key,
        "provider_label": request.provider_label,
        "starts_at": request.starts_at.isoformat(),
        "timezone_name": request.timezone_name,
        "title": request.title,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def create_cockpit_event(
    *,
    telegram_user_id: int,
    requested_business_id: str | None,
    request_id: str,
    title: str,
    starts_at_local: str,
    join_url: str,
    offer_url: str | None,
    description: str,
    enable_email_notifications: bool,
) -> str:
    actor, _ = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    actor.assert_can_manage_business()
    _public_base_url(required=True)
    profile = get_business_profile(actor=actor)
    normalized_request_id = str(UUID(str(request_id)))
    starts_at = datetime.fromisoformat(
        parse_local_booking_start(starts_at_local, timezone_name=profile.timezone)
    )
    normalized_offer = None if not str(offer_url or "").strip() else validate_external_https_url(
        offer_url, field_name="offer_url"
    )
    request = OnlineEventCreateRequest(
        title=str(title or "").strip(),
        starts_at=starts_at,
        timezone_name=profile.timezone,
        join_url=validate_external_https_url(join_url, field_name="join_url"),
        offer_url=normalized_offer,
        description=str(description or "").strip(),
        enable_email_notifications=bool(enable_email_notifications),
    )
    fingerprint = _request_hash(request=request)
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with atomic_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO clientplatform_event_owner_requests(
                business_id,request_id,request_hash,event_id,created_at
            ) VALUES(?,?,?,NULL,?)
            ON CONFLICT(business_id,request_id) DO NOTHING
            """,
            (actor.business_id, normalized_request_id, fingerprint, now_iso),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            row = conn.execute(
                """
                SELECT request_hash,event_id
                FROM clientplatform_event_owner_requests
                WHERE business_id=? AND request_id=? LIMIT 1
                """,
                (actor.business_id, normalized_request_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("event creation replay state is unavailable")
            existing_hash = str(row["request_hash"] if hasattr(row, "keys") else row[0])
            existing_event = row["event_id"] if hasattr(row, "keys") else row[1]
            if existing_hash != fingerprint:
                raise ValueError("request_id already belongs to another event request")
            if existing_event is None:
                raise RuntimeError("event creation is still in progress")
            return str(existing_event)

        created = create_and_publish_online_event_in_transaction(
            conn, actor=actor, request=request
        )
        conn.execute(
            """
            UPDATE clientplatform_event_owner_requests
            SET event_id=?
            WHERE business_id=? AND request_id=? AND event_id IS NULL
            """,
            (created.event_id, actor.business_id, normalized_request_id),
        )
        return created.event_id


def cancel_cockpit_event(
    *, telegram_user_id: int, requested_business_id: str | None, event_id: str
) -> str:
    actor, _ = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    actor.assert_can_manage_business()
    return cancel_event(actor=actor, event_id=event_id).status


__all__ = [
    "CockpitEventAcquisition",
    "CockpitEventItem",
    "CockpitEventRevenue",
    "CockpitEventsSnapshot",
    "cancel_cockpit_event",
    "create_cockpit_event",
    "resolve_cockpit_events",
]
