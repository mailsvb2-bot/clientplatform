from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Any

from clientplatform.domain.event_landing import (
    EventLandingContent,
    event_landing_content_from_json,
    event_landing_content_to_json,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


@dataclass(frozen=True, slots=True)
class EventLandingProfile:
    business_id: str
    event_id: str
    draft: EventLandingContent
    published: EventLandingContent | None
    draft_source: str
    revision: int
    published_revision: int | None
    updated_by_member_id: str
    created_at: str
    updated_at: str
    published_at: str | None

    @property
    def is_published(self) -> bool:
        return self.published is not None and self.published_revision is not None

    @property
    def has_unpublished_changes(self) -> bool:
        return self.is_published and self.published_revision != self.revision


@dataclass(frozen=True, slots=True)
class IssuedEventLandingPreview:
    token: str
    revision: int
    expires_at: str


_COLUMNS = (
    "business_id,event_id,draft_json,published_json,draft_source,revision,"
    "published_revision,updated_by_member_id,created_at,updated_at,published_at"
)


def _profile_from_row(row: Any) -> EventLandingProfile:
    published_raw = _value(row, "published_json", 3)
    published_revision = _value(row, "published_revision", 6)
    published_at = _value(row, "published_at", 10)
    return EventLandingProfile(
        business_id=str(_value(row, "business_id", 0)),
        event_id=str(_value(row, "event_id", 1)),
        draft=event_landing_content_from_json(_value(row, "draft_json", 2)),
        published=(
            None
            if published_raw is None
            else event_landing_content_from_json(published_raw)
        ),
        draft_source=str(_value(row, "draft_source", 4)),
        revision=int(_value(row, "revision", 5)),
        published_revision=(
            None if published_revision is None else int(published_revision)
        ),
        updated_by_member_id=str(_value(row, "updated_by_member_id", 7)),
        created_at=str(_value(row, "created_at", 8)),
        updated_at=str(_value(row, "updated_at", 9)),
        published_at=None if published_at is None else str(published_at),
    )


class EventLandingRepository:
    def __init__(self, conn: Any):
        self._conn = conn

    def _event(self, *, actor: TenantContext, event_id: str) -> str:
        actor.assert_can_manage_business()
        normalized = normalize_uuid(event_id, field_name="event_id")
        row = self._conn.execute(
            """
            SELECT id FROM clientplatform_events
            WHERE id=? AND business_id=? LIMIT 1
            """,
            (normalized, actor.business_id),
        ).fetchone()
        if row is None:
            raise ValueError("event was not found in the active business")
        return normalized

    def get(
        self,
        *,
        actor: TenantContext,
        event_id: str,
    ) -> EventLandingProfile | None:
        normalized = self._event(actor=actor, event_id=event_id)
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM clientplatform_event_landing_profiles "  # nosec B608
            "WHERE business_id=? AND event_id=? LIMIT 1",
            (actor.business_id, normalized),
        ).fetchone()
        return None if row is None else _profile_from_row(row)

    def save_draft(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        content: EventLandingContent,
        source: str,
        now: str | None = None,
    ) -> EventLandingProfile:
        normalized = self._event(actor=actor, event_id=event_id)
        source_value = str(source or "").strip().lower()
        if source_value not in {"template", "ai", "owner"}:
            raise ValueError("event landing source is invalid")
        body = event_landing_content_to_json(content)
        timestamp = str(now or _utc_now())
        existing = self._conn.execute(
            """
            SELECT revision FROM clientplatform_event_landing_profiles
            WHERE business_id=? AND event_id=? LIMIT 1
            """,
            (actor.business_id, normalized),
        ).fetchone()
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO clientplatform_event_landing_profiles(
                    business_id,event_id,draft_json,published_json,draft_source,
                    revision,published_revision,preview_token_digest,preview_revision,
                    preview_expires_at,updated_by_member_id,created_at,updated_at,published_at
                ) VALUES(?,?,?,NULL,?,1,NULL,NULL,NULL,NULL,?,?,?,NULL)
                """,
                (
                    actor.business_id,
                    normalized,
                    body,
                    source_value,
                    actor.membership_id,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            revision = int(_value(existing, "revision", 0))
            cursor = self._conn.execute(
                """
                UPDATE clientplatform_event_landing_profiles
                SET draft_json=?,draft_source=?,revision=revision+1,
                    preview_token_digest=NULL,preview_revision=NULL,preview_expires_at=NULL,
                    updated_by_member_id=?,updated_at=?
                WHERE business_id=? AND event_id=? AND revision=?
                """,
                (
                    body,
                    source_value,
                    actor.membership_id,
                    timestamp,
                    actor.business_id,
                    normalized,
                    revision,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise RuntimeError("event landing changed concurrently; refresh and retry")
        stored = self.get(actor=actor, event_id=normalized)
        if stored is None:
            raise RuntimeError("event landing draft was not persisted")
        return stored

    def publish(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        expected_revision: int | None = None,
        now: str | None = None,
    ) -> EventLandingProfile:
        profile = self.get(actor=actor, event_id=event_id)
        if profile is None:
            raise ValueError("event landing draft is missing")
        if expected_revision is not None and profile.revision != int(expected_revision):
            raise RuntimeError("event landing changed; refresh before publishing")
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_event_landing_profiles
            SET published_json=draft_json,published_revision=revision,published_at=?,
                preview_token_digest=NULL,preview_revision=NULL,preview_expires_at=NULL,
                updated_by_member_id=?,updated_at=?
            WHERE business_id=? AND event_id=? AND revision=?
            """,
            (
                timestamp,
                actor.membership_id,
                timestamp,
                actor.business_id,
                profile.event_id,
                profile.revision,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise RuntimeError("event landing changed concurrently; refresh and retry")
        stored = self.get(actor=actor, event_id=profile.event_id)
        if stored is None:
            raise RuntimeError("published event landing was not persisted")
        return stored

    def unpublish(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        now: str | None = None,
    ) -> EventLandingProfile:
        profile = self.get(actor=actor, event_id=event_id)
        if profile is None:
            raise ValueError("event landing draft is missing")
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_event_landing_profiles
            SET published_json=NULL,published_revision=NULL,published_at=NULL,
                preview_token_digest=NULL,preview_revision=NULL,preview_expires_at=NULL,
                updated_by_member_id=?,updated_at=?
            WHERE business_id=? AND event_id=? AND revision=?
            """,
            (
                actor.membership_id,
                timestamp,
                actor.business_id,
                profile.event_id,
                profile.revision,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise RuntimeError("event landing changed concurrently; refresh and retry")
        stored = self.get(actor=actor, event_id=profile.event_id)
        if stored is None:
            raise RuntimeError("event landing draft disappeared")
        return stored

    def issue_preview(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        ttl_seconds: int = 1800,
        now: str | None = None,
    ) -> IssuedEventLandingPreview:
        profile = self.get(actor=actor, event_id=event_id)
        if profile is None:
            raise ValueError("event landing draft is missing")
        ttl = max(60, min(int(ttl_seconds), 3600))
        current = _parse_utc(now or _utc_now()).replace(microsecond=0)
        expires = current + timedelta(seconds=ttl)
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_event_landing_profiles
            SET preview_token_digest=?,preview_revision=?,preview_expires_at=?,
                updated_by_member_id=?,updated_at=?
            WHERE business_id=? AND event_id=? AND revision=?
            """,
            (
                digest,
                profile.revision,
                expires.isoformat(),
                actor.membership_id,
                current.isoformat(),
                actor.business_id,
                profile.event_id,
                profile.revision,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise RuntimeError("event landing changed concurrently; refresh and retry")
        return IssuedEventLandingPreview(
            token=token,
            revision=profile.revision,
            expires_at=expires.isoformat(),
        )


def get_published_event_landing(
    conn: Any,
    *,
    public_slug: str,
) -> EventLandingContent | None:
    slug = str(public_slug or "").strip()
    row = conn.execute(
        """
        SELECT l.published_json
        FROM clientplatform_event_landing_profiles l
        JOIN clientplatform_events e
          ON e.id=l.event_id AND e.business_id=l.business_id
        WHERE e.public_slug=? AND e.status='published'
          AND l.published_json IS NOT NULL
        LIMIT 1
        """,
        (slug,),
    ).fetchone()
    if row is None:
        return None
    return event_landing_content_from_json(_value(row, "published_json", 0))


def get_preview_event_landing(
    conn: Any,
    *,
    public_slug: str,
    token: str,
    now: str | None = None,
) -> EventLandingContent | None:
    raw_token = str(token or "").strip()
    if len(raw_token) < 20 or len(raw_token) > 160:
        return None
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    row = conn.execute(
        """
        SELECT l.draft_json,l.revision,l.preview_revision,l.preview_expires_at
        FROM clientplatform_event_landing_profiles l
        JOIN clientplatform_events e
          ON e.id=l.event_id AND e.business_id=l.business_id
        WHERE e.public_slug=? AND e.status='published'
          AND l.preview_token_digest=?
        LIMIT 1
        """,
        (str(public_slug or "").strip(), digest),
    ).fetchone()
    if row is None:
        return None
    revision = int(_value(row, "revision", 1))
    preview_revision = int(_value(row, "preview_revision", 2) or 0)
    expires_at = _value(row, "preview_expires_at", 3)
    current = _parse_utc(now or _utc_now())
    if preview_revision != revision or expires_at is None or _parse_utc(expires_at) <= current:
        return None
    return event_landing_content_from_json(_value(row, "draft_json", 0))


__all__ = [
    "EventLandingProfile",
    "EventLandingRepository",
    "IssuedEventLandingPreview",
    "get_preview_event_landing",
    "get_published_event_landing",
]
