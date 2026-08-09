from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from clientplatform.domain.partners import (
    ContactBasis,
    PartnerAutomationMode,
    PartnerCampaign,
    PartnerCampaignGoal,
    PartnerCampaignStats,
    PartnerCampaignStatus,
    PartnerCandidate,
    PartnerCandidateStatus,
    PartnerChannel,
    PartnerContentPack,
    PartnerFitScore,
    PartnerNotFound,
    PlacementKind,
    normalize_public_contact,
    partner_source_fingerprint,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _goal_from_json(value: str) -> PartnerCampaignGoal:
    raw = json.loads(value or "{}")
    if not isinstance(raw, dict):
        raise ValueError("partner campaign goal must be a JSON object")
    return PartnerCampaignGoal(
        target_count=int(raw.get("target_count") or 1),
        deadline=str(raw.get("deadline") or ""),
        budget_minor=int(raw.get("budget_minor") or 0),
        objective=str(raw.get("objective") or "new_customers"),
        event_title=str(raw.get("event_title") or ""),
        target_url=str(raw.get("target_url") or ""),
        audience_terms=tuple(raw.get("audience_terms") or ()),
        offer_summary=str(raw.get("offer_summary") or ""),
        constraints=tuple(raw.get("constraints") or ()),
    )


def _campaign_from_row(row: Any) -> PartnerCampaign:
    return PartnerCampaign(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        name=str(_value(row, "name", 2)),
        goal=_goal_from_json(str(_value(row, "goal_json", 3))),
        automation_mode=PartnerAutomationMode(str(_value(row, "automation_mode", 4))),
        status=PartnerCampaignStatus(str(_value(row, "status", 5))),
        created_by_member_id=str(_value(row, "created_by_member_id", 6)),
        created_at=str(_value(row, "created_at", 7)),
        updated_at=str(_value(row, "updated_at", 8)),
    )


def _candidate_from_row(row: Any) -> PartnerCandidate:
    follower_count = _value(row, "follower_count", 11)
    raw_tags = json.loads(str(_value(row, "tags_json", 12) or "[]"))
    if not isinstance(raw_tags, list):
        raise ValueError("partner candidate tags must be a JSON list")
    return PartnerCandidate(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        campaign_id=str(_value(row, "campaign_id", 2)),
        name=str(_value(row, "name", 4)),
        source_url=str(_value(row, "source_url", 5) or ""),
        audience_summary=str(_value(row, "audience_summary", 6) or ""),
        recent_topic=str(_value(row, "recent_topic", 7) or ""),
        channel=PartnerChannel(str(_value(row, "channel", 8))),
        contact_value=str(_value(row, "contact_value", 9) or ""),
        contact_basis=ContactBasis(str(_value(row, "contact_basis", 10))),
        follower_count=None if follower_count is None else int(follower_count),
        tags=tuple(str(item) for item in raw_tags),
        competitor=bool(int(_value(row, "competitor", 13) or 0)),
        status=PartnerCandidateStatus(str(_value(row, "status", 14))),
        referral_token=str(_value(row, "referral_token", 17) or ""),
        discovered_at=str(_value(row, "discovered_at", 18)),
        updated_at=str(_value(row, "updated_at", 19)),
    )


_CAMPAIGN_SELECT = """
SELECT id, business_id, name, goal_json, automation_mode, status,
       created_by_member_id, created_at, updated_at
FROM partner_campaigns
"""

_CANDIDATE_SELECT = """
SELECT id, business_id, campaign_id, source_fingerprint, name, source_url,
       audience_summary, recent_topic, channel, contact_value, contact_basis,
       follower_count, tags_json, competitor, status, fit_total, fit_json,
       referral_token, discovered_at, updated_at
FROM partner_candidates
"""

_EVENT_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class PartnerRepository:
    """Tenant-safe persistence for partner preparation and referral evidence."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _current_actor(self, actor: TenantContext) -> TenantContext:
        return self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )

    def create_campaign(
        self,
        *,
        actor: TenantContext,
        name: str,
        goal: PartnerCampaignGoal,
        automation_mode: PartnerAutomationMode | str = PartnerAutomationMode.CAUTIOUS,
        now: str | None = None,
    ) -> PartnerCampaign:
        current = self._current_actor(actor)
        current.assert_can_manage_promotions()
        timestamp = str(now or _utc_now())
        mode = (
            automation_mode
            if isinstance(automation_mode, PartnerAutomationMode)
            else PartnerAutomationMode(str(automation_mode))
        )
        campaign = PartnerCampaign(
            id=str(uuid4()),
            business_id=current.business_id,
            name=name,
            goal=goal,
            automation_mode=mode,
            status=PartnerCampaignStatus.ACTIVE,
            created_by_member_id=current.membership_id,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._conn.execute(
            """
            INSERT INTO partner_campaigns(
                id, business_id, name, goal_json, automation_mode, status,
                created_by_member_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign.id,
                campaign.business_id,
                campaign.name,
                _json(asdict(campaign.goal)),
                campaign.automation_mode.value,
                campaign.status.value,
                campaign.created_by_member_id,
                campaign.created_at,
                campaign.updated_at,
            ),
        )
        return campaign

    def get_campaign(
        self,
        *,
        actor: TenantContext,
        campaign_id: str,
    ) -> PartnerCampaign:
        current = self._current_actor(actor)
        current.assert_can_view_promotion_analytics()
        normalized = normalize_uuid(campaign_id, field_name="partner_campaign_id")
        row = self._conn.execute(
            _CAMPAIGN_SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise PartnerNotFound("Партнёрская кампания не найдена")
        return _campaign_from_row(row)

    def list_campaigns(self, *, actor: TenantContext) -> list[PartnerCampaign]:
        current = self._current_actor(actor)
        current.assert_can_view_promotion_analytics()
        rows = self._conn.execute(
            _CAMPAIGN_SELECT
            + " WHERE business_id=? ORDER BY created_at DESC, id DESC",
            (current.business_id,),
        ).fetchall()
        return [_campaign_from_row(row) for row in rows]

    def upsert_candidate(
        self,
        *,
        actor: TenantContext,
        campaign: PartnerCampaign,
        name: str,
        source_url: str,
        audience_summary: str,
        recent_topic: str,
        channel: PartnerChannel | str,
        contact_value: str,
        contact_basis: ContactBasis | str,
        follower_count: int | None,
        tags: tuple[str, ...],
        competitor: bool,
        score: PartnerFitScore,
        now: str | None = None,
    ) -> PartnerCandidate:
        current = self._current_actor(actor)
        current.assert_can_manage_promotions()
        current.assert_business(campaign.business_id)
        selected_channel = (
            channel if isinstance(channel, PartnerChannel) else PartnerChannel(str(channel))
        )
        selected_basis = (
            contact_basis
            if isinstance(contact_basis, ContactBasis)
            else ContactBasis(str(contact_basis))
        )
        timestamp = str(now or _utc_now())
        normalized_contact = normalize_public_contact(selected_channel, contact_value)
        candidate = PartnerCandidate(
            id=str(uuid4()),
            business_id=current.business_id,
            campaign_id=campaign.id,
            name=name,
            source_url=source_url,
            audience_summary=audience_summary,
            recent_topic=recent_topic,
            channel=selected_channel,
            contact_value=normalized_contact,
            contact_basis=selected_basis,
            follower_count=follower_count,
            tags=tags,
            competitor=competitor,
            referral_token=secrets.token_urlsafe(18),
            status=PartnerCandidateStatus.READY,
            discovered_at=timestamp,
            updated_at=timestamp,
        )
        fingerprint = partner_source_fingerprint(
            campaign_id=campaign.id,
            source_url=candidate.source_url,
            name=candidate.name,
        )
        self._conn.execute(
            """
            INSERT INTO partner_candidates(
                id, business_id, campaign_id, source_fingerprint, name, source_url,
                audience_summary, recent_topic, channel, contact_value, contact_basis,
                follower_count, tags_json, competitor, status, fit_total, fit_json,
                referral_token, discovered_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)
            ON CONFLICT(business_id, campaign_id, source_fingerprint) DO UPDATE SET
                name=excluded.name,
                source_url=excluded.source_url,
                audience_summary=excluded.audience_summary,
                recent_topic=excluded.recent_topic,
                channel=excluded.channel,
                contact_value=CASE
                    WHEN partner_candidates.contact_basis IN ('opted_in','existing_relationship')
                        THEN partner_candidates.contact_value
                    WHEN excluded.contact_basis IN ('opted_in','existing_relationship')
                        THEN excluded.contact_value
                    WHEN partner_candidates.contact_value<>''
                        THEN partner_candidates.contact_value
                    ELSE excluded.contact_value
                END,
                contact_basis=CASE
                    WHEN partner_candidates.contact_basis IN ('opted_in','existing_relationship')
                        THEN partner_candidates.contact_basis
                    WHEN excluded.contact_basis IN ('opted_in','existing_relationship')
                        THEN excluded.contact_basis
                    WHEN partner_candidates.contact_basis='public_business_contact'
                        THEN partner_candidates.contact_basis
                    WHEN excluded.contact_basis='public_business_contact'
                        THEN excluded.contact_basis
                    ELSE partner_candidates.contact_basis
                END,
                follower_count=COALESCE(excluded.follower_count, partner_candidates.follower_count),
                tags_json=excluded.tags_json,
                competitor=CASE
                    WHEN partner_candidates.competitor=1 OR excluded.competitor=1 THEN 1
                    ELSE 0
                END,
                fit_total=excluded.fit_total,
                updated_at=excluded.updated_at
            """,
            (
                candidate.id,
                candidate.business_id,
                candidate.campaign_id,
                fingerprint,
                candidate.name,
                candidate.source_url,
                candidate.audience_summary,
                candidate.recent_topic,
                candidate.channel.value,
                candidate.contact_value,
                candidate.contact_basis.value,
                candidate.follower_count,
                _json(list(candidate.tags)),
                1 if candidate.competitor else 0,
                candidate.status.value,
                float(score.total),
                candidate.referral_token,
                candidate.discovered_at,
                candidate.updated_at,
            ),
        )
        row = self._conn.execute(
            _CANDIDATE_SELECT
            + " WHERE business_id=? AND campaign_id=? AND source_fingerprint=? LIMIT 1",
            (current.business_id, campaign.id, fingerprint),
        ).fetchone()
        if row is None:
            raise PartnerNotFound("Кандидат партнёра не сохранился")
        durable = _candidate_from_row(row)
        durable_score = replace(score, candidate_id=durable.id)
        self._conn.execute(
            "UPDATE partner_candidates SET fit_json=? WHERE id=? AND business_id=?",
            (_json(asdict(durable_score)), durable.id, current.business_id),
        )
        return durable

    def list_candidates(
        self,
        *,
        actor: TenantContext,
        campaign_id: str,
        limit: int = 100,
    ) -> list[PartnerCandidate]:
        current = self._current_actor(actor)
        # Candidate rows contain contact details; analytics-only roles must not
        # receive them through a read path.
        current.assert_can_manage_promotions()
        normalized = normalize_uuid(campaign_id, field_name="partner_campaign_id")
        rows = self._conn.execute(
            _CANDIDATE_SELECT
            + " WHERE business_id=? AND campaign_id=? "
            "ORDER BY fit_total DESC, discovered_at ASC LIMIT ?",
            (current.business_id, normalized, max(1, min(500, int(limit)))),
        ).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def get_candidate(
        self,
        *,
        actor: TenantContext,
        candidate_id: str,
    ) -> PartnerCandidate:
        current = self._current_actor(actor)
        current.assert_can_manage_promotions()
        normalized = normalize_uuid(candidate_id, field_name="partner_candidate_id")
        row = self._conn.execute(
            _CANDIDATE_SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise PartnerNotFound("Кандидат партнёра не найден")
        return _candidate_from_row(row)

    def set_candidate_status(
        self,
        *,
        actor: TenantContext,
        candidate_id: str,
        status: PartnerCandidateStatus | str,
        now: str | None = None,
    ) -> None:
        current = self._current_actor(actor)
        current.assert_can_manage_promotions()
        selected = (
            status
            if isinstance(status, PartnerCandidateStatus)
            else PartnerCandidateStatus(str(status))
        )
        normalized = normalize_uuid(candidate_id, field_name="partner_candidate_id")
        cursor = self._conn.execute(
            "UPDATE partner_candidates SET status=?, updated_at=? "
            "WHERE id=? AND business_id=?",
            (selected.value, str(now or _utc_now()), normalized, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise PartnerNotFound("Кандидат партнёра не найден")

    def save_content_pack(
        self,
        *,
        actor: TenantContext,
        campaign_id: str,
        pack: PartnerContentPack,
        now: str | None = None,
    ) -> None:
        current = self._current_actor(actor)
        current.assert_can_manage_promotions()
        timestamp = str(now or _utc_now())
        campaign = normalize_uuid(campaign_id, field_name="partner_campaign_id")
        self._conn.execute(
            """
            INSERT INTO partner_content_packs(
                candidate_id, business_id, campaign_id, subject, outreach_message,
                ready_post, followup_message, collaboration_angle, cta,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id, business_id) DO UPDATE SET
                subject=excluded.subject,
                outreach_message=excluded.outreach_message,
                ready_post=excluded.ready_post,
                followup_message=excluded.followup_message,
                collaboration_angle=excluded.collaboration_angle,
                cta=excluded.cta,
                updated_at=excluded.updated_at
            """,
            (
                pack.candidate_id,
                current.business_id,
                campaign,
                pack.subject,
                pack.outreach_message,
                pack.ready_post,
                pack.followup_message,
                pack.collaboration_angle,
                pack.cta,
                timestamp,
                timestamp,
            ),
        )

    def get_content_pack(
        self,
        *,
        actor: TenantContext,
        candidate_id: str,
    ) -> PartnerContentPack:
        current = self._current_actor(actor)
        current.assert_can_manage_promotions()
        normalized = normalize_uuid(candidate_id, field_name="partner_candidate_id")
        row = self._conn.execute(
            """
            SELECT candidate_id, subject, outreach_message, ready_post,
                   followup_message, collaboration_angle, cta
            FROM partner_content_packs
            WHERE candidate_id=? AND business_id=? LIMIT 1
            """,
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise PartnerNotFound("Комплект для партнёра не найден")
        return PartnerContentPack(
            candidate_id=str(_value(row, "candidate_id", 0)),
            subject=str(_value(row, "subject", 1)),
            outreach_message=str(_value(row, "outreach_message", 2)),
            ready_post=str(_value(row, "ready_post", 3)),
            followup_message=str(_value(row, "followup_message", 4)),
            collaboration_angle=str(_value(row, "collaboration_angle", 5)),
            cta=str(_value(row, "cta", 6)),
        )

    def record_placement(
        self,
        *,
        actor: TenantContext,
        campaign_id: str,
        candidate_id: str,
        kind: PlacementKind | str,
        external_url: str = "",
        scheduled_at: str | None = None,
        published_at: str | None = None,
        now: str | None = None,
    ) -> str:
        current = self._current_actor(actor)
        current.assert_can_manage_promotions()
        campaign = normalize_uuid(campaign_id, field_name="partner_campaign_id")
        candidate = normalize_uuid(candidate_id, field_name="partner_candidate_id")
        selected_kind = (
            kind if isinstance(kind, PlacementKind) else PlacementKind(str(kind))
        )
        url = _normalize_optional_https_url(external_url)
        timestamp = str(now or _utc_now())
        placement_id = str(uuid4())
        self._conn.execute(
            """
            INSERT INTO partner_placements(
                id, business_id, campaign_id, candidate_id, kind, external_url,
                scheduled_at, published_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                placement_id,
                current.business_id,
                campaign,
                candidate,
                selected_kind.value,
                url,
                scheduled_at,
                published_at,
                timestamp,
                timestamp,
            ),
        )
        return placement_id

    def resolve_public_referral(
        self,
        *,
        referral_token: str,
    ) -> tuple[PartnerCandidate, PartnerCampaign]:
        candidate = self._candidate_by_referral_token(referral_token=referral_token)
        row = self._conn.execute(
            _CAMPAIGN_SELECT
            + " WHERE id=? AND business_id=? AND status='active' LIMIT 1",
            (candidate.campaign_id, candidate.business_id),
        ).fetchone()
        if row is None:
            raise PartnerNotFound("Партнёрская кампания больше не активна")
        return candidate, _campaign_from_row(row)

    def record_referral_event(
        self,
        *,
        referral_token: str,
        event_type: str,
        event_key: str | None = None,
        now: str | None = None,
    ) -> bool:
        candidate, _campaign = self.resolve_public_referral(
            referral_token=referral_token
        )
        selected_type = str(event_type or "").strip()
        if selected_type not in {"opened", "result"}:
            raise ValueError("unsupported partner referral event")
        key = str(event_key or secrets.token_urlsafe(18)).strip()
        if not _EVENT_KEY_RE.fullmatch(key):
            raise ValueError("partner referral event key must be an opaque token")
        dedupe = f"{selected_type}:{candidate.id}:{key}"
        cursor = self._conn.execute(
            """
            INSERT INTO partner_referral_events(
                id, business_id, campaign_id, candidate_id, referral_token,
                event_type, event_key, dedupe_key, occurred_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(business_id, dedupe_key) DO NOTHING
            """,
            (
                str(uuid4()),
                candidate.business_id,
                candidate.campaign_id,
                candidate.id,
                str(referral_token),
                selected_type,
                key,
                dedupe,
                str(now or _utc_now()),
            ),
        )
        return int(getattr(cursor, "rowcount", 1) or 0) == 1

    def stats(
        self,
        *,
        actor: TenantContext,
        campaign_id: str | None = None,
    ) -> PartnerCampaignStats:
        current = self._current_actor(actor)
        current.assert_can_view_promotion_analytics()
        campaign = (
            None
            if campaign_id is None
            else normalize_uuid(campaign_id, field_name="partner_campaign_id")
        )
        campaign_where = "business_id=?"
        campaign_params: tuple[Any, ...] = (current.business_id,)
        child_where = "business_id=?"
        child_params: tuple[Any, ...] = (current.business_id,)
        if campaign is not None:
            campaign_where += " AND id=?"
            campaign_params += (campaign,)
            child_where += " AND campaign_id=?"
            child_params += (campaign,)

        campaign_row = self._conn.execute(
            f"SELECT COUNT(*) FROM partner_campaigns WHERE {campaign_where}",  # nosec B608
            campaign_params,
        ).fetchone()
        candidate_row = self._conn.execute(
            f"""
            SELECT COUNT(*),
                   SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status IN ('contacted','replied','accepted','paid_only') THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status IN ('replied','accepted','paid_only') THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END)
            FROM partner_candidates WHERE {child_where}
            """,  # nosec B608
            child_params,
        ).fetchone()
        placement_row = self._conn.execute(
            f"SELECT COUNT(*) FROM partner_placements WHERE {child_where}",  # nosec B608
            child_params,
        ).fetchone()
        event_row = self._conn.execute(
            f"""
            SELECT SUM(CASE WHEN event_type='opened' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN event_type='result' THEN 1 ELSE 0 END)
            FROM partner_referral_events WHERE {child_where}
            """,  # nosec B608
            child_params,
        ).fetchone()
        return PartnerCampaignStats(
            campaigns=_count(campaign_row, 0),
            candidates=_count(candidate_row, 0),
            ready=_count(candidate_row, 1),
            contacted=_count(candidate_row, 2),
            replies=_count(candidate_row, 3),
            accepted=_count(candidate_row, 4),
            placements=_count(placement_row, 0),
            attributed_visits=_count(event_row, 0),
            attributed_results=_count(event_row, 1),
        )

    def _candidate_by_referral_token(self, *, referral_token: str) -> PartnerCandidate:
        token = str(referral_token or "").strip()
        if not token or len(token) > 128:
            raise PartnerNotFound("Партнёрская ссылка недействительна")
        row = self._conn.execute(
            _CANDIDATE_SELECT + " WHERE referral_token=? LIMIT 1",
            (token,),
        ).fetchone()
        if row is None:
            raise PartnerNotFound("Партнёрская ссылка не найдена")
        return _candidate_from_row(row)


def _count(row: Any, position: int) -> int:
    if row is None:
        return 0
    try:
        return int(row[position] or 0)
    except (IndexError, TypeError, ValueError):
        return 0


def _normalize_optional_https_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 2048:
        raise ValueError("partner placement URL is too long")
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("partner placement URL must be a public HTTPS URL")
    return text


__all__ = ["PartnerRepository"]
