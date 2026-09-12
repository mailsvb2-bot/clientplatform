from __future__ import annotations

"""Auditable, channel-scoped commercial consent for online-event follow-ups.

Event registration consent remains separate and mandatory. Commercial consent is
optional, versioned and revocable. The append-only evidence table records exactly
what was accepted; current channel state is re-read at the provider boundary.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any, Iterable
from uuid import uuid4

from clientplatform.domain.events import normalize_utc


CURRENT_EVENT_MARKETING_CONSENT_VERSION = "2026-09-event-commercial-v1"
_ALLOWED_CHANNELS = frozenset({"email", "vk", "max", "telegram"})
_PUBLIC_CHANNELS = frozenset({"email", "vk", "max"})
_COMMERCIAL_KEY_FRAGMENT = ":message:post:v4:stage:"
_COMMERCIAL_PROVIDER_BOUNDARY_MARKER = "event_commercial_provider_call_started_non_idempotent"


def normalize_marketing_channels(
    values: Iterable[object],
    *,
    public_form: bool = False,
) -> tuple[str, ...]:
    allowed = _PUBLIC_CHANNELS if public_form else _ALLOWED_CHANNELS
    normalized: list[str] = []
    unknown: list[str] = []
    for raw in values:
        value = str(raw or "").strip().lower()
        if not value:
            continue
        if value not in allowed:
            unknown.append(value)
            continue
        if value not in normalized:
            normalized.append(value)
    if unknown:
        raise ValueError("unsupported marketing channel")
    if not normalized:
        raise ValueError("at least one marketing channel is required")
    return tuple(normalized)


def build_event_commercial_consent_text(advertiser_label: object) -> str:
    label = " ".join(str(advertiser_label or "").split()).strip()
    if not label or len(label) > 240:
        raise ValueError("advertiser label is unavailable")
    return (
        f"Я согласен получать от «{label}» информационные и рекламные сообщения "
        "о продуктах, услугах и мероприятиях по выбранным каналам. "
        "Согласие можно отозвать в любой момент по ссылке в сообщении."
    )


def commercial_consent_text_sha256(advertiser_label: object) -> str:
    return hashlib.sha256(
        build_event_commercial_consent_text(advertiser_label).encode("utf-8")
    ).hexdigest()


def public_event_advertiser_label_in_transaction(
    conn: Any,
    *,
    public_slug: str,
) -> str | None:
    slug = str(public_slug or "").strip()
    row = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(bp.brand_display_name),''), b.name) AS advertiser_label
        FROM clientplatform_events e
        JOIN businesses b ON b.id=e.business_id AND b.status='active'
        LEFT JOIN business_profiles bp ON bp.business_id=b.id
        WHERE e.public_slug=? AND e.status='published'
        LIMIT 1
        """,
        (slug,),
    ).fetchone()
    if row is None:
        return None
    value = row["advertiser_label"] if hasattr(row, "keys") else row[0]
    label = " ".join(str(value or "").split()).strip()
    return label[:240] if label else None


@dataclass(frozen=True, slots=True)
class CommercialConsentEvidence:
    event_id: str
    registration_id: str
    advertiser_label: str
    channels: tuple[str, ...]
    consent_text_version: str
    consent_text_sha256: str
    occurred_at: str


def grant_event_commercial_consent_in_transaction(
    conn: Any,
    *,
    business_id: str,
    event_id: str,
    registration_id: str,
    channels: Iterable[object],
    expected_text_sha256: str | None = None,
    now: datetime | str | None = None,
) -> CommercialConsentEvidence:
    normalized_channels = normalize_marketing_channels(channels)
    row = conn.execute(
        """
        SELECT r.identity_hash,r.source,r.campaign_ref,
               COALESCE(NULLIF(TRIM(bp.brand_display_name),''), b.name) AS advertiser_label
        FROM clientplatform_event_registrations r
        JOIN clientplatform_events e
          ON e.id=r.event_id AND e.business_id=r.business_id
        JOIN businesses b ON b.id=r.business_id AND b.status='active'
        LEFT JOIN business_profiles bp ON bp.business_id=b.id
        WHERE r.id=? AND r.event_id=? AND r.business_id=? AND r.status='registered'
        LIMIT 1
        """,
        (registration_id, event_id, business_id),
    ).fetchone()
    if row is None:
        raise ValueError("event registration is unavailable for commercial consent")
    identity_hash = str(row["identity_hash"] if hasattr(row, "keys") else row[0])
    source = row["source"] if hasattr(row, "keys") else row[1]
    campaign_ref = row["campaign_ref"] if hasattr(row, "keys") else row[2]
    advertiser_raw = row["advertiser_label"] if hasattr(row, "keys") else row[3]
    advertiser = " ".join(str(advertiser_raw or "").split()).strip()
    text_hash = commercial_consent_text_sha256(advertiser)
    if expected_text_sha256 is not None:
        expected = str(expected_text_sha256 or "").strip().lower()
        if expected != text_hash:
            raise ValueError("commercial consent text changed; reload the event page")
    occurred = normalize_utc(
        now or datetime.now(timezone.utc), field_name="now"
    ).replace(microsecond=0).isoformat()
    evidence_id = str(uuid4())
    channels_csv = ",".join(sorted(normalized_channels))
    conn.execute(
        """
        INSERT INTO clientplatform_event_commercial_consent_events(
            id,business_id,event_id,registration_id,action,advertiser_label,
            consent_text_version,consent_text_sha256,channels_csv,
            registration_identity_hash,source,campaign_ref,occurred_at
        ) VALUES(?,?,?,?, 'grant', ?,?,?,?,?,?,?,?)
        """,
        (
            evidence_id,
            business_id,
            event_id,
            registration_id,
            advertiser,
            CURRENT_EVENT_MARKETING_CONSENT_VERSION,
            text_hash,
            channels_csv,
            identity_hash,
            None if source is None else str(source)[:200],
            None if campaign_ref is None else str(campaign_ref)[:200],
            occurred,
        ),
    )
    for platform in normalized_channels:
        conn.execute(
            """
            INSERT INTO clientplatform_event_commercial_channel_state(
                business_id,event_id,registration_id,platform,status,
                grant_event_id,granted_at,updated_at,revoked_at
            ) VALUES(?,?,?,?, 'active', ?,?,?,NULL)
            ON CONFLICT(business_id,event_id,registration_id,platform) DO UPDATE SET
                status='active',
                grant_event_id=excluded.grant_event_id,
                granted_at=excluded.granted_at,
                updated_at=excluded.updated_at,
                revoked_at=NULL
            """,
            (
                business_id,
                event_id,
                registration_id,
                platform,
                evidence_id,
                occurred,
                occurred,
            ),
        )
    return CommercialConsentEvidence(
        event_id=event_id,
        registration_id=registration_id,
        advertiser_label=advertiser,
        channels=normalized_channels,
        consent_text_version=CURRENT_EVENT_MARKETING_CONSENT_VERSION,
        consent_text_sha256=text_hash,
        occurred_at=occurred,
    )


def active_event_commercial_channels(
    conn: Any,
    *,
    business_id: str,
    event_id: str,
    registration_id: str,
) -> tuple[str, ...]:
    rows = conn.execute(
        """
        SELECT platform
        FROM clientplatform_event_commercial_channel_state
        WHERE business_id=? AND event_id=? AND registration_id=? AND status='active'
        ORDER BY platform
        """,
        (business_id, event_id, registration_id),
    ).fetchall()
    return tuple(
        str(row["platform"] if hasattr(row, "keys") else row[0]) for row in rows
    )


def commercial_channel_is_authorized(
    conn: Any,
    *,
    business_id: str,
    event_id: str,
    registration_id: str,
    platform: str,
) -> bool:
    channel = str(platform or "").strip().lower()
    if channel not in _ALLOWED_CHANNELS:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM clientplatform_event_commercial_channel_state
        WHERE business_id=? AND event_id=? AND registration_id=?
          AND platform=? AND status='active'
        LIMIT 1
        """,
        (business_id, event_id, registration_id, channel),
    ).fetchone()
    return row is not None


def _cancel_not_started_commercial_dispatches(
    conn: Any,
    *,
    business_id: str,
    registration_id: str,
    reason: str,
    now_iso: str,
) -> int:
    cursor = conn.execute(
        """
        UPDATE provider_dispatch_outbox
        SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,last_error=?
        WHERE business_id=? AND source_kind='event_message' AND source_id=?
          AND status IN ('pending','retry')
          AND idempotency_key LIKE 'event:%:message:post:v4:stage:%'
        """,
        (
            now_iso,
            reason[:240],
            business_id,
            registration_id,
        ),
    )
    return max(0, int(getattr(cursor, "rowcount", 0) or 0))


def revoke_event_commercial_consent_by_registration_token_in_transaction(
    conn: Any,
    *,
    token: str,
    now: datetime | str | None = None,
) -> int:
    capability = str(token or "").strip()
    row = conn.execute(
        """
        SELECT r.business_id,r.event_id,r.id,r.identity_hash,r.source,r.campaign_ref,
               COALESCE(NULLIF(TRIM(bp.brand_display_name),''), b.name) AS advertiser_label
        FROM clientplatform_event_registrations r
        JOIN businesses b ON b.id=r.business_id
        LEFT JOIN business_profiles bp ON bp.business_id=b.id
        WHERE r.token=?
        LIMIT 1
        """,
        (capability,),
    ).fetchone()
    if row is None:
        raise ValueError("event registration token is invalid")
    business_id = str(row["business_id"] if hasattr(row, "keys") else row[0])
    event_id = str(row["event_id"] if hasattr(row, "keys") else row[1])
    registration_id = str(row["id"] if hasattr(row, "keys") else row[2])
    identity_hash = str(row["identity_hash"] if hasattr(row, "keys") else row[3])
    source = row["source"] if hasattr(row, "keys") else row[4]
    campaign_ref = row["campaign_ref"] if hasattr(row, "keys") else row[5]
    advertiser_raw = row["advertiser_label"] if hasattr(row, "keys") else row[6]
    advertiser = " ".join(str(advertiser_raw or "").split()).strip()[:240]
    channels = active_event_commercial_channels(
        conn,
        business_id=business_id,
        event_id=event_id,
        registration_id=registration_id,
    )
    occurred = normalize_utc(
        now or datetime.now(timezone.utc), field_name="now"
    ).replace(microsecond=0).isoformat()
    if channels:
        conn.execute(
            """
            INSERT INTO clientplatform_event_commercial_consent_events(
                id,business_id,event_id,registration_id,action,advertiser_label,
                consent_text_version,consent_text_sha256,channels_csv,
                registration_identity_hash,source,campaign_ref,occurred_at
            ) VALUES(?,?,?,?, 'revoke', ?,NULL,NULL,?,?,?,?,?)
            """,
            (
                str(uuid4()),
                business_id,
                event_id,
                registration_id,
                advertiser or "Организатор",
                ",".join(sorted(channels)),
                identity_hash,
                None if source is None else str(source)[:200],
                None if campaign_ref is None else str(campaign_ref)[:200],
                occurred,
            ),
        )
        conn.execute(
            """
            UPDATE clientplatform_event_commercial_channel_state
            SET status='revoked',updated_at=?,revoked_at=?
            WHERE business_id=? AND event_id=? AND registration_id=? AND status='active'
            """,
            (occurred, occurred, business_id, event_id, registration_id),
        )
    _cancel_not_started_commercial_dispatches(
        conn,
        business_id=business_id,
        registration_id=registration_id,
        reason="event_commercial_consent_revoked",
        now_iso=occurred,
    )
    return len(channels)


__all__ = [
    "CURRENT_EVENT_MARKETING_CONSENT_VERSION",
    "CommercialConsentEvidence",
    "active_event_commercial_channels",
    "build_event_commercial_consent_text",
    "commercial_channel_is_authorized",
    "commercial_consent_text_sha256",
    "grant_event_commercial_consent_in_transaction",
    "normalize_marketing_channels",
    "public_event_advertiser_label_in_transaction",
    "revoke_event_commercial_consent_by_registration_token_in_transaction",
]
