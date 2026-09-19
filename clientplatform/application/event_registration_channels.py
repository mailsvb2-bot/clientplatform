from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Any, Iterable
from uuid import uuid4

from clientplatform.application.event_commercial_consent import (
    commercial_consent_text_sha256,
    grant_event_commercial_consent_in_transaction,
    public_event_advertiser_label_in_transaction,
)
from clientplatform.domain.customers import CustomerPlatform, normalize_identity_subject
from clientplatform.domain.events import EventRegistration, normalize_utc
from clientplatform.infrastructure.event_repository import EventRepository


LOGGER = logging.getLogger(__name__)

_EVENT_MESSENGER_PLATFORMS = (
    CustomerPlatform.TELEGRAM,
    CustomerPlatform.VK,
    CustomerPlatform.MAX,
)
_ALLOWED_CONNECTION_TYPES = {
    "telegram": frozenset(
        {"telegram_shared_bot", "telegram_managed_bot", "telegram_business"}
    ),
    "vk": frozenset({"vk_community"}),
    "max": frozenset({"max_shared_bot", "max_personal_bot"}),
}


class EventRegistrationChannelLinkRejected(ValueError):
    """An event-scoped messenger verification token cannot be consumed safely."""


@dataclass(frozen=True, slots=True)
class IssuedEventRegistrationChannelLink:
    platform: str
    token: str
    expires_at: str
    marketing_requested: bool


@dataclass(frozen=True, slots=True)
class ConsumedEventRegistrationChannelLink:
    business_id: str
    event_id: str
    registration_id: str
    platform: str
    external_subject: str
    marketing_consent_recorded: bool
    notifications_queued: int


def extract_event_registration_channel_link_token(value: object) -> str | None:
    raw = " ".join(str(value or "").strip().split())
    payload = raw
    lowered = raw.casefold()
    if lowered.startswith("/start ") or lowered.startswith("start "):
        payload = raw.split(maxsplit=1)[1].strip()
    if not payload.casefold().startswith("ecv_"):
        return None
    token = payload[4:].strip()
    return token or None


def _marketing_platforms(values: Iterable[object]) -> frozenset[str]:
    normalized = {
        str(value or "").strip().lower()
        for value in values
        if str(value or "").strip()
    }
    unsupported = normalized.difference({"telegram", "vk", "max"})
    if unsupported:
        raise ValueError("unsupported event messenger marketing channel")
    return frozenset(normalized)


def issue_event_registration_channel_links_in_transaction(
    conn: Any,
    *,
    registration: EventRegistration,
    marketing_platforms: Iterable[object] = (),
    expected_marketing_text_sha256: str | None = None,
    ttl_seconds: int = 3600,
    now: datetime | str | None = None,
) -> tuple[IssuedEventRegistrationChannelLink, ...]:
    """Issue registration-scoped messenger verification capabilities.

    These tokens never attach a public-form e-mail registration to a global CRM
    messenger identity. Consumption proves control of one exact messenger
    account for this exact event registration.
    """

    requested_marketing = _marketing_platforms(marketing_platforms)
    row = conn.execute(
        """
        SELECT r.id,r.business_id,r.event_id,r.status AS registration_status,
               e.public_slug,e.status AS event_status
        FROM clientplatform_event_registrations r
        JOIN clientplatform_events e
          ON e.id=r.event_id AND e.business_id=r.business_id
        WHERE r.id=? AND r.business_id=? AND r.event_id=?
        LIMIT 1
        """,
        (registration.id, registration.business_id, registration.event_id),
    ).fetchone()
    if row is None:
        raise EventRegistrationChannelLinkRejected("event registration was not found")
    value = lambda key, index: row[key] if hasattr(row, "keys") else row[index]
    if (
        str(value("registration_status", 3)) != "registered"
        or str(value("event_status", 5)) != "published"
    ):
        raise EventRegistrationChannelLinkRejected(
            "event registration is not available for messenger verification"
        )

    consent_hash: str | None = None
    if requested_marketing:
        public_slug = str(value("public_slug", 4))
        advertiser = public_event_advertiser_label_in_transaction(
            conn, public_slug=public_slug
        )
        if advertiser is None:
            raise EventRegistrationChannelLinkRejected(
                "advertiser label is unavailable for messenger marketing consent"
            )
        consent_hash = commercial_consent_text_sha256(advertiser)
        expected = str(expected_marketing_text_sha256 or "").strip().lower()
        if expected != consent_hash:
            raise EventRegistrationChannelLinkRejected(
                "commercial consent text changed; reload the event page"
            )

    current = normalize_utc(
        now or datetime.now(timezone.utc), field_name="now"
    ).replace(microsecond=0)
    ttl = min(max(int(ttl_seconds), 60), 3600)
    expires = current + timedelta(seconds=ttl)
    issued: list[IssuedEventRegistrationChannelLink] = []
    for platform in _EVENT_MESSENGER_PLATFORMS:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        marketing_requested = platform.value in requested_marketing
        conn.execute(
            """
            INSERT INTO clientplatform_event_channel_link_tokens(
                id,business_id,event_id,registration_id,token_digest,target_platform,
                marketing_requested,consent_text_sha256,created_at,expires_at,
                consumed_at,consumed_external_subject
            ) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)
            """,
            (
                str(uuid4()),
                registration.business_id,
                registration.event_id,
                registration.id,
                digest,
                platform.value,
                1 if marketing_requested else 0,
                consent_hash if marketing_requested else None,
                current.isoformat(),
                expires.isoformat(),
            ),
        )
        issued.append(
            IssuedEventRegistrationChannelLink(
                platform=platform.value,
                token=token,
                expires_at=expires.isoformat(),
                marketing_requested=marketing_requested,
            )
        )
    return tuple(issued)


def _validated_connection_id(
    conn: Any,
    *,
    business_id: str,
    platform: str,
    connection_id: str | None,
) -> str | None:
    if connection_id is None:
        return None
    normalized = str(connection_id or "").strip()
    if not normalized:
        return None
    row = conn.execute(
        """
        SELECT connection_type
        FROM connections
        WHERE id=? AND business_id=? AND platform=? AND status='active'
        LIMIT 1
        """,
        (normalized, business_id, platform),
    ).fetchone()
    if row is None:
        raise EventRegistrationChannelLinkRejected(
            "messenger verification arrived through an inactive connection"
        )
    connection_type = str(
        row["connection_type"] if hasattr(row, "keys") else row[0]
    )
    if connection_type not in _ALLOWED_CONNECTION_TYPES[platform]:
        raise EventRegistrationChannelLinkRejected(
            "messenger verification connection cannot address a customer"
        )
    return normalized


def consume_event_registration_channel_link_in_transaction(
    conn: Any,
    *,
    token: str,
    platform: CustomerPlatform | str,
    external_subject: str,
    expected_business_id: str | None = None,
    connection_id: str | None = None,
    now: datetime | str | None = None,
) -> ConsumedEventRegistrationChannelLink:
    raw_token = str(token or "").strip()
    if len(raw_token) < 20 or len(raw_token) > 160:
        raise EventRegistrationChannelLinkRejected(
            "event messenger verification token is invalid"
        )
    normalized_platform, normalized_subject = normalize_identity_subject(
        platform, external_subject
    )
    if normalized_platform not in _EVENT_MESSENGER_PLATFORMS:
        raise EventRegistrationChannelLinkRejected(
            "event messenger verification requires Telegram, VK or MAX"
        )
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    row = conn.execute(
        """
        SELECT l.id,l.business_id,l.event_id,l.registration_id,l.target_platform,
               l.marketing_requested,l.consent_text_sha256,l.expires_at,l.consumed_at,
               r.token AS registration_token,e.public_slug
        FROM clientplatform_event_channel_link_tokens l
        JOIN clientplatform_event_registrations r
          ON r.id=l.registration_id AND r.business_id=l.business_id
         AND r.event_id=l.event_id AND r.status='registered'
        JOIN clientplatform_events e
          ON e.id=l.event_id AND e.business_id=l.business_id
         AND e.status='published'
        WHERE l.token_digest=?
        LIMIT 1
        """,
        (digest,),
    ).fetchone()
    if row is None:
        raise EventRegistrationChannelLinkRejected(
            "event messenger verification token was not found"
        )
    value = lambda key, index: row[key] if hasattr(row, "keys") else row[index]
    business_id = str(value("business_id", 1))
    if expected_business_id is not None and business_id != str(expected_business_id):
        raise EventRegistrationChannelLinkRejected(
            "event messenger verification belongs to another business"
        )
    if str(value("target_platform", 4)) != normalized_platform.value:
        raise EventRegistrationChannelLinkRejected(
            "event messenger verification token belongs to another platform"
        )
    if value("consumed_at", 8) is not None:
        raise EventRegistrationChannelLinkRejected(
            "event messenger verification token was already consumed"
        )
    current = normalize_utc(
        now or datetime.now(timezone.utc), field_name="now"
    ).replace(microsecond=0)
    try:
        expires = normalize_utc(str(value("expires_at", 7)), field_name="expires_at")
    except ValueError as exc:
        raise EventRegistrationChannelLinkRejected(
            "event messenger verification token expiry is invalid"
        ) from exc
    if expires <= current:
        raise EventRegistrationChannelLinkRejected(
            "event messenger verification token expired"
        )

    registration_id = str(value("registration_id", 3))
    event_id = str(value("event_id", 2))
    bound_connection_id = _validated_connection_id(
        conn,
        business_id=business_id,
        platform=normalized_platform.value,
        connection_id=connection_id,
    )
    existing = conn.execute(
        """
        SELECT external_subject,connection_id
        FROM clientplatform_event_registration_channels
        WHERE business_id=? AND event_id=? AND registration_id=? AND platform=?
        LIMIT 1
        """,
        (business_id, event_id, registration_id, normalized_platform.value),
    ).fetchone()
    if existing is not None:
        existing_subject = str(
            existing["external_subject"] if hasattr(existing, "keys") else existing[0]
        )
        existing_connection = (
            existing["connection_id"] if hasattr(existing, "keys") else existing[1]
        )
        if existing_subject != normalized_subject:
            raise EventRegistrationChannelLinkRejected(
                "this event registration is already verified to another messenger account"
            )
        if (
            existing_connection is not None
            and bound_connection_id is not None
            and str(existing_connection) != bound_connection_id
        ):
            raise EventRegistrationChannelLinkRejected(
                "this event registration is already verified through another connection"
            )

    cursor = conn.execute(
        """
        UPDATE clientplatform_event_channel_link_tokens
        SET consumed_at=?,consumed_external_subject=?
        WHERE id=? AND consumed_at IS NULL
        """,
        (current.isoformat(), normalized_subject, str(value("id", 0))),
    )
    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
        raise EventRegistrationChannelLinkRejected(
            "event messenger verification lost a concurrent consume race"
        )

    conn.execute(
        """
        INSERT INTO clientplatform_event_registration_channels(
            business_id,event_id,registration_id,platform,external_subject,
            connection_id,verified_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(business_id,event_id,registration_id,platform) DO UPDATE SET
            connection_id=COALESCE(
                clientplatform_event_registration_channels.connection_id,
                excluded.connection_id
            ),
            updated_at=excluded.updated_at
        """,
        (
            business_id,
            event_id,
            registration_id,
            normalized_platform.value,
            normalized_subject,
            bound_connection_id,
            current.isoformat(),
            current.isoformat(),
        ),
    )

    marketing_recorded = False
    if int(value("marketing_requested", 5) or 0) == 1:
        expected_hash = str(value("consent_text_sha256", 6) or "").strip().lower()
        grant_event_commercial_consent_in_transaction(
            conn,
            business_id=business_id,
            event_id=event_id,
            registration_id=registration_id,
            channels=(normalized_platform.value,),
            expected_text_sha256=expected_hash,
            now=current,
        )
        marketing_recorded = True

    notifications_queued = 0
    registration_token = str(value("registration_token", 9))
    public_slug = str(value("public_slug", 10))
    try:
        repository = EventRepository(conn)
        registration = repository.get_registration_by_token(token=registration_token)
        event = repository.get_public_owner_event(public_slug=public_slug)
        from clientplatform.application.event_notifications import enqueue_event_notifications
        from services.db.core import ambient_savepoint

        with ambient_savepoint(conn):
            notifications_queued = enqueue_event_notifications(
                conn,
                event=event,
                registration=registration,
                now=current,
            ).queued
    except Exception:  # validator: allow-wide-except - verification and consent stay durable
        LOGGER.exception(
            "event messenger verification notification reconciliation failed",
            extra={
                "business_id": business_id,
                "event_id": event_id,
                "registration_id": registration_id,
                "platform": normalized_platform.value,
            },
        )
    return ConsumedEventRegistrationChannelLink(
        business_id=business_id,
        event_id=event_id,
        registration_id=registration_id,
        platform=normalized_platform.value,
        external_subject=normalized_subject,
        marketing_consent_recorded=marketing_recorded,
        notifications_queued=notifications_queued,
    )


def consume_event_registration_channel_link(
    *,
    token: str,
    platform: CustomerPlatform | str,
    external_subject: str,
    expected_business_id: str | None = None,
    connection_id: str | None = None,
) -> ConsumedEventRegistrationChannelLink:
    from services.db import get_db

    with get_db() as conn:
        return consume_event_registration_channel_link_in_transaction(
            conn,
            token=token,
            platform=platform,
            external_subject=external_subject,
            expected_business_id=expected_business_id,
            connection_id=connection_id,
        )


__all__ = [
    "ConsumedEventRegistrationChannelLink",
    "EventRegistrationChannelLinkRejected",
    "IssuedEventRegistrationChannelLink",
    "consume_event_registration_channel_link",
    "consume_event_registration_channel_link_in_transaction",
    "extract_event_registration_channel_link_token",
    "issue_event_registration_channel_links_in_transaction",
]
