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
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


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


@dataclass(frozen=True, slots=True)
class EventLandingAIClaim:
    created: bool
    status: str
    base_revision: int
    claim_digest: str


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
        self._tenancy = TenancyRepository(conn)

    def _event(
        self,
        *,
        actor: TenantContext,
        event_id: str,
    ) -> tuple[TenantContext, str]:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        normalized = normalize_uuid(event_id, field_name="event_id")
        row = self._conn.execute(
            """
            SELECT id FROM clientplatform_events
            WHERE id=? AND business_id=? LIMIT 1
            """,
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise ValueError("event was not found in the active business")
        return current, normalized

    def get(
        self,
        *,
        actor: TenantContext,
        event_id: str,
    ) -> EventLandingProfile | None:
        current, normalized = self._event(actor=actor, event_id=event_id)
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM clientplatform_event_landing_profiles "  # nosec B608
            "WHERE business_id=? AND event_id=? LIMIT 1",
            (current.business_id, normalized),
        ).fetchone()
        return None if row is None else _profile_from_row(row)

    def save_draft(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        content: EventLandingContent,
        source: str,
        expected_revision: int | None = None,
        now: str | None = None,
    ) -> EventLandingProfile:
        current, normalized = self._event(actor=actor, event_id=event_id)
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
            (current.business_id, normalized),
        ).fetchone()
        if existing is None:
            if expected_revision not in {None, 0}:
                raise RuntimeError("event landing changed concurrently; refresh and retry")
            self._conn.execute(
                """
                INSERT INTO clientplatform_event_landing_profiles(
                    business_id,event_id,draft_json,published_json,draft_source,
                    revision,published_revision,preview_token_digest,preview_revision,
                    preview_expires_at,updated_by_member_id,created_at,updated_at,published_at
                ) VALUES(?,?,?,NULL,?,1,NULL,NULL,NULL,NULL,?,?,?,NULL)
                """,
                (
                    current.business_id,
                    normalized,
                    body,
                    source_value,
                    current.membership_id,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            revision = int(_value(existing, "revision", 0))
            if expected_revision is not None and revision != int(expected_revision):
                raise RuntimeError("event landing changed concurrently; refresh and retry")
            cursor = self._conn.execute(
                """
                UPDATE clientplatform_event_landing_profiles
                SET draft_json=?,draft_source=?,revision=revision+1,
                    preview_token_digest=NULL,preview_revision=NULL,preview_expires_at=NULL,
                    ai_status=CASE WHEN ai_status='planning' THEN 'ambiguous' ELSE NULL END,
                    ai_base_revision=CASE WHEN ai_status='planning' THEN ai_base_revision ELSE NULL END,
                    ai_claim_digest=CASE WHEN ai_status='planning' THEN ai_claim_digest ELSE NULL END,
                    ai_updated_at=CASE WHEN ai_status='planning' THEN ? ELSE NULL END,
                    updated_by_member_id=?,updated_at=?
                WHERE business_id=? AND event_id=? AND revision=?
                """,
                (
                    body,
                    source_value,
                    timestamp,
                    current.membership_id,
                    timestamp,
                    current.business_id,
                    normalized,
                    revision,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise RuntimeError("event landing changed concurrently; refresh and retry")
        stored = self.get(actor=current, event_id=normalized)
        if stored is None:
            raise RuntimeError("event landing draft was not persisted")
        return stored

    def ensure_draft(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        content: EventLandingContent,
        source: str = "template",
        now: str | None = None,
    ) -> EventLandingProfile:
        current, normalized = self._event(actor=actor, event_id=event_id)
        source_value = str(source or "").strip().lower()
        if source_value not in {"template", "ai", "owner"}:
            raise ValueError("event landing source is invalid")
        body = event_landing_content_to_json(content)
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO clientplatform_event_landing_profiles(
                business_id,event_id,draft_json,published_json,draft_source,
                revision,published_revision,preview_token_digest,preview_revision,
                preview_expires_at,updated_by_member_id,created_at,updated_at,published_at
            ) VALUES(?,?,?,NULL,?,1,NULL,NULL,NULL,NULL,?,?,?,NULL)
            ON CONFLICT(business_id,event_id) DO NOTHING
            """,
            (
                current.business_id,
                normalized,
                body,
                source_value,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        stored = self.get(actor=current, event_id=normalized)
        if stored is None:
            raise RuntimeError("event landing draft was not persisted")
        return stored


    def claim_ai_generation(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        expected_revision: int,
        claim_digest: str,
        now: str | None = None,
    ) -> EventLandingAIClaim:
        current, normalized = self._event(actor=actor, event_id=event_id)
        digest = str(claim_digest or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("event landing AI claim digest is invalid")
        revision = int(expected_revision)
        if revision < 1:
            raise ValueError("event landing AI base revision is invalid")
        timestamp = str(now or _utc_now())
        row = self._conn.execute(
            """
            SELECT revision,ai_status,ai_base_revision,ai_claim_digest
            FROM clientplatform_event_landing_profiles
            WHERE business_id=? AND event_id=? LIMIT 1
            """,
            (current.business_id, normalized),
        ).fetchone()
        if row is None:
            raise ValueError("event landing draft is missing")
        actual_revision = int(_value(row, "revision", 0))
        if actual_revision != revision:
            raise RuntimeError("event landing changed; refresh before AI generation")
        prior_status = _value(row, "ai_status", 1)
        prior_base = _value(row, "ai_base_revision", 2)
        prior_digest = _value(row, "ai_claim_digest", 3)
        if (
            prior_status in {"planning", "ambiguous"}
            and prior_base is not None
            and int(prior_base) == revision
            and str(prior_digest or "") == digest
        ):
            return EventLandingAIClaim(
                created=False,
                status=str(prior_status),
                base_revision=revision,
                claim_digest=digest,
            )
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_event_landing_profiles
            SET ai_status='planning',ai_base_revision=?,ai_claim_digest=?,
                ai_updated_at=?,updated_by_member_id=?,updated_at=?
            WHERE business_id=? AND event_id=? AND revision=?
              AND NOT (
                ai_status IN ('planning','ambiguous')
                AND ai_base_revision=?
                AND ai_claim_digest=?
              )
            """,
            (
                revision,
                digest,
                timestamp,
                current.membership_id,
                timestamp,
                current.business_id,
                normalized,
                revision,
                revision,
                digest,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            row = self._conn.execute(
                """
                SELECT ai_status,ai_base_revision,ai_claim_digest
                FROM clientplatform_event_landing_profiles
                WHERE business_id=? AND event_id=? AND revision=? LIMIT 1
                """,
                (current.business_id, normalized, revision),
            ).fetchone()
            if row is None:
                raise RuntimeError("event landing changed; refresh before AI generation")
            status = str(_value(row, "ai_status", 0) or "")
            base = _value(row, "ai_base_revision", 1)
            stored_digest = str(_value(row, "ai_claim_digest", 2) or "")
            if (
                status in {"planning", "ambiguous"}
                and base is not None
                and int(base) == revision
                and stored_digest == digest
            ):
                return EventLandingAIClaim(
                    created=False,
                    status=status,
                    base_revision=revision,
                    claim_digest=digest,
                )
            raise RuntimeError("event landing AI claim changed concurrently")
        return EventLandingAIClaim(
            created=True,
            status="planning",
            base_revision=revision,
            claim_digest=digest,
        )

    def mark_ai_generation_ambiguous(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        base_revision: int,
        claim_digest: str,
        now: str | None = None,
    ) -> None:
        current, normalized = self._event(actor=actor, event_id=event_id)
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            UPDATE clientplatform_event_landing_profiles
            SET ai_status='ambiguous',ai_updated_at=?,
                updated_by_member_id=?,updated_at=?
            WHERE business_id=? AND event_id=? AND ai_status='planning'
              AND ai_base_revision=? AND ai_claim_digest=?
            """,
            (
                timestamp,
                current.membership_id,
                timestamp,
                current.business_id,
                normalized,
                int(base_revision),
                str(claim_digest or "").strip().lower(),
            ),
        )

    def complete_ai_generation(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        base_revision: int,
        claim_digest: str,
        content: EventLandingContent,
        now: str | None = None,
    ) -> EventLandingProfile:
        current, normalized = self._event(actor=actor, event_id=event_id)
        digest = str(claim_digest or "").strip().lower()
        body = event_landing_content_to_json(content)
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_event_landing_profiles
            SET draft_json=?,draft_source='ai',revision=revision+1,
                preview_token_digest=NULL,preview_revision=NULL,preview_expires_at=NULL,
                ai_status='ready',ai_updated_at=?,
                updated_by_member_id=?,updated_at=?
            WHERE business_id=? AND event_id=? AND revision=?
              AND ai_status='planning' AND ai_base_revision=? AND ai_claim_digest=?
            """,
            (
                body,
                timestamp,
                current.membership_id,
                timestamp,
                current.business_id,
                normalized,
                int(base_revision),
                int(base_revision),
                digest,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            self.mark_ai_generation_ambiguous(
                actor=current,
                event_id=normalized,
                base_revision=base_revision,
                claim_digest=digest,
                now=timestamp,
            )
            raise RuntimeError(
                "event landing changed while AI generation was in progress"
            )
        stored = self.get(actor=current, event_id=normalized)
        if stored is None:
            raise RuntimeError("event landing AI draft was not persisted")
        return stored


    def publish(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        expected_revision: int | None = None,
        now: str | None = None,
    ) -> EventLandingProfile:
        current, normalized = self._event(actor=actor, event_id=event_id)
        profile = self.get(actor=current, event_id=normalized)
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
                current.membership_id,
                timestamp,
                current.business_id,
                profile.event_id,
                profile.revision,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise RuntimeError("event landing changed concurrently; refresh and retry")
        stored = self.get(actor=current, event_id=profile.event_id)
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
        current, normalized = self._event(actor=actor, event_id=event_id)
        profile = self.get(actor=current, event_id=normalized)
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
                current.membership_id,
                timestamp,
                current.business_id,
                profile.event_id,
                profile.revision,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise RuntimeError("event landing changed concurrently; refresh and retry")
        stored = self.get(actor=current, event_id=profile.event_id)
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
        current_actor, normalized = self._event(actor=actor, event_id=event_id)
        profile = self.get(actor=current_actor, event_id=normalized)
        if profile is None:
            raise ValueError("event landing draft is missing")
        ttl = max(60, min(int(ttl_seconds), 3600))
        current_time = _parse_utc(now or _utc_now()).replace(microsecond=0)
        expires = current_time + timedelta(seconds=ttl)
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
                current_actor.membership_id,
                current_time.isoformat(),
                current_actor.business_id,
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
    try:
        return event_landing_content_from_json(_value(row, "published_json", 0))
    except ValueError:
        # Landing copy is an optional presentation layer. Corruption must not
        # take the canonical registration endpoint down; the caller falls back
        # to the established simple event landing.
        return None


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
    try:
        return event_landing_content_from_json(_value(row, "draft_json", 0))
    except ValueError:
        return None


__all__ = [
    "EventLandingAIClaim",
    "EventLandingProfile",
    "EventLandingRepository",
    "IssuedEventLandingPreview",
    "get_preview_event_landing",
    "get_published_event_landing",
]
