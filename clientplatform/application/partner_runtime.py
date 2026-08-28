from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.application.partner_growth import (
    PartnerGrowthService,
    PartnerPreparationRun,
)
from clientplatform.domain.partners import (
    ContactBasis,
    PartnerAutomationMode,
    PartnerCampaign,
    PartnerCampaignGoal,
    PartnerCampaignStats,
    PartnerCandidate,
    PartnerCandidateStatus,
    PartnerContentPack,
    PartnerInvariantViolation,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure import DispatchOutboxRepository
from clientplatform.infrastructure.partner_repository import PartnerRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.integrations.partner_discovery_runtime import (
    build_connected_partner_discovery,
)
from services.db import get_db, get_db_ro


_TELEGRAM_CHAT_ID_RE = re.compile(r"-?[1-9][0-9]{0,19}")
_CONTACT_REVOKING_STATUSES = {
    PartnerCandidateStatus.DECLINED,
    PartnerCandidateStatus.DO_NOT_CONTACT,
    PartnerCandidateStatus.INVALID,
}


@dataclass(frozen=True, slots=True)
class PartnerSendConnection:
    id: str
    label: str


@dataclass(frozen=True, slots=True)
class PartnerCandidateView:
    candidate: PartnerCandidate
    fit_total: float
    content: PartnerContentPack
    reply_count: int
    latest_reply: str


@dataclass(frozen=True, slots=True)
class PartnerReplyView:
    occurred_at: str
    reply_text: str


def start_connected_partner_campaign(
    *,
    actor: TenantContext,
    name: str = "",
    target_count: int = 50,
) -> PartnerPreparationRun:
    discovery = build_connected_partner_discovery(actor=actor)
    stamp = datetime.now(timezone.utc).date().isoformat()
    campaign_name = " ".join(str(name or "").split()).strip() or f"Партнёрства {stamp}"
    service = PartnerGrowthService(discovery=discovery)
    return service.start(
        actor=actor,
        name=campaign_name[:200],
        goal=PartnerCampaignGoal(
            target_count=max(1, min(int(target_count), 500)),
            objective="new_customers",
        ),
        automation_mode=PartnerAutomationMode.CAUTIOUS,
    )


def rerun_connected_partner_campaign(
    *,
    actor: TenantContext,
    campaign_id: str,
) -> PartnerPreparationRun:
    return PartnerGrowthService(
        discovery=build_connected_partner_discovery(actor=actor)
    ).run(actor=actor, campaign_id=campaign_id)


def list_partner_campaigns(*, actor: TenantContext) -> list[PartnerCampaign]:
    with get_db_ro() as conn:
        return PartnerRepository(conn).list_campaigns(actor=actor)


def partner_stats(
    *,
    actor: TenantContext,
    campaign_id: str | None = None,
) -> PartnerCampaignStats:
    with get_db_ro() as conn:
        return PartnerRepository(conn).stats(actor=actor, campaign_id=campaign_id)


def list_partner_candidates(
    *,
    actor: TenantContext,
    campaign_id: str,
    limit: int = 50,
) -> list[PartnerCandidate]:
    with get_db_ro() as conn:
        return PartnerRepository(conn).list_candidates(
            actor=actor,
            campaign_id=campaign_id,
            limit=limit,
        )


def get_partner_candidate_view(
    *,
    actor: TenantContext,
    candidate_id: str,
) -> PartnerCandidateView:
    normalized = normalize_uuid(candidate_id, field_name="partner_candidate_id")
    with get_db_ro() as conn:
        repo = PartnerRepository(conn)
        candidate = repo.get_candidate(actor=actor, candidate_id=normalized)
        content = repo.get_content_pack(actor=actor, candidate_id=normalized)
        row = conn.execute(
            """
            SELECT fit_total
            FROM partner_candidates
            WHERE id=? AND business_id=? LIMIT 1
            """,
            (candidate.id, candidate.business_id),
        ).fetchone()
        reply = conn.execute(
            """
            SELECT COUNT(*) AS reply_count
            FROM partner_reply_events
            WHERE business_id=? AND campaign_id=? AND candidate_id=?
            """,
            (candidate.business_id, candidate.campaign_id, candidate.id),
        ).fetchone()
        latest = conn.execute(
            """
            SELECT reply_text
            FROM partner_reply_events
            WHERE business_id=? AND campaign_id=? AND candidate_id=?
            ORDER BY occurred_at DESC,id DESC LIMIT 1
            """,
            (candidate.business_id, candidate.campaign_id, candidate.id),
        ).fetchone()
        fit_total = float(_value(row, "fit_total", 0) or 0) if row is not None else 0.0
        reply_count = int(_value(reply, "reply_count", 0) or 0) if reply is not None else 0
        latest_reply = "" if latest is None else str(_value(latest, "reply_text", 0) or "")
        return PartnerCandidateView(
            candidate=candidate,
            fit_total=round(fit_total, 2),
            content=content,
            reply_count=reply_count,
            latest_reply=latest_reply,
        )


def list_partner_send_connections(
    *,
    actor: TenantContext,
    platform: str = "telegram",
) -> list[PartnerSendConnection]:
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        selected = str(platform or "").strip().lower()
        if selected == "telegram":
            rows = conn.execute(
                """
                SELECT id, external_account_id, connection_type
                FROM connections
                WHERE business_id=? AND platform='telegram' AND status='active'
                  AND connection_type IN (
                      'telegram_shared_bot','telegram_managed_bot'
                  )
                ORDER BY created_at,id
                """,
                (current.business_id,),
            ).fetchall()
        elif selected == "email":
            rows = conn.execute(
                """
                SELECT id, external_account_id, connection_type
                FROM connections
                WHERE business_id=? AND platform='email' AND status='active'
                  AND connection_type='email_smtp'
                ORDER BY created_at,id
                """,
                (current.business_id,),
            ).fetchall()
        else:
            raise ValueError("partner send platform must be telegram or email")
        prefix = "Telegram" if selected == "telegram" else "Email"
        fallback = "бот" if selected == "telegram" else "отправитель"
        return [
            PartnerSendConnection(
                id=str(_value(row, "id", 0)),
                label=(
                    prefix
                    + " · "
                    + str(_value(row, "external_account_id", 1) or fallback)[:80]
                ),
            )
            for row in rows
        ]


def authorize_partner_telegram_contact(
    *,
    actor: TenantContext,
    candidate_id: str,
    chat_id: str,
    basis: ContactBasis | str,
) -> PartnerCandidate:
    selected = basis if isinstance(basis, ContactBasis) else ContactBasis(str(basis))
    if selected not in {ContactBasis.OPTED_IN, ContactBasis.EXISTING_RELATIONSHIP}:
        raise PartnerInvariantViolation(
            "automatic partner contact requires opt-in or an existing relationship"
        )
    subject = str(chat_id or "").strip()
    if not _TELEGRAM_CHAT_ID_RE.fullmatch(subject):
        raise PartnerInvariantViolation("Telegram chat id must be numeric")
    normalized = normalize_uuid(candidate_id, field_name="partner_candidate_id")
    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        candidate = PartnerRepository(conn).get_candidate(
            actor=current,
            candidate_id=normalized,
        )
        if candidate.competitor or candidate.status in _CONTACT_REVOKING_STATUSES:
            raise PartnerInvariantViolation("candidate cannot be authorized for outreach")
        conn.execute(
            """
            UPDATE partner_candidates
            SET channel='telegram',contact_value=?,contact_basis=?,updated_at=?
            WHERE id=? AND business_id=?
            """,
            (
                subject,
                selected.value,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                candidate.id,
                current.business_id,
            ),
        )
        return PartnerRepository(conn).get_candidate(
            actor=current,
            candidate_id=candidate.id,
        )


def queue_partner_outreach(
    *,
    actor: TenantContext,
    candidate_id: str,
    connection_id: str,
) -> Any:
    with get_db() as conn:
        return DispatchOutboxRepository(conn).materialize_partner_outreach(
            actor=actor,
            candidate_id=candidate_id,
            connection_id=connection_id,
        )


def approve_and_queue_partner_email_outreach(
    *,
    actor: TenantContext,
    candidate_id: str,
    connection_id: str,
) -> Any:
    """Explicit owner gate for one public-business-email first contact."""

    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        candidate = PartnerRepository(conn).get_candidate(
            actor=current,
            candidate_id=candidate_id,
        )
        if (
            candidate.channel.value != "email"
            or candidate.contact_basis != ContactBasis.PUBLIC_BUSINESS_CONTACT
        ):
            raise PartnerInvariantViolation(
                "explicit email approval is only for a public business contact"
            )
        return DispatchOutboxRepository(conn).materialize_partner_outreach(
            actor=current,
            candidate_id=candidate.id,
            connection_id=connection_id,
            explicit_owner_approval=True,
        )


def set_partner_candidate_status(
    *,
    actor: TenantContext,
    candidate_id: str,
    status: PartnerCandidateStatus | str,
) -> None:
    selected = (
        status
        if isinstance(status, PartnerCandidateStatus)
        else PartnerCandidateStatus(str(status))
    )
    with get_db() as conn:
        PartnerRepository(conn).set_candidate_status(
            actor=actor,
            candidate_id=candidate_id,
            status=selected,
        )
        if selected in _CONTACT_REVOKING_STATUSES:
            DispatchOutboxRepository(conn).cancel_not_started_partner_outreach(
                actor=actor,
                candidate_id=candidate_id,
            )


def record_partner_reply_if_expected(
    *,
    business_id: str,
    connection_id: str,
    external_subject: str,
    provider_event_key: str,
    reply_text: str,
) -> str | None:
    """Persist authenticated provider ingress as a partner reply when matched.

    This is a system path: tenant identity comes from the authenticated managed
    bot route, not from user-controlled payload fields.
    """

    business = normalize_uuid(business_id, field_name="business_id")
    connection = normalize_uuid(connection_id, field_name="connection_id")
    subject = str(external_subject or "").strip()
    event_key = str(provider_event_key or "").strip()
    if not subject or not event_key or len(event_key) > 128:
        return None
    text = str(reply_text or "").replace("\x00", " ").strip()[:4000]
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT d.partner_campaign_id,d.partner_candidate_id
            FROM provider_dispatch_outbox d
            JOIN partner_candidates p
              ON p.id=d.partner_candidate_id AND p.business_id=d.business_id
             AND p.campaign_id=d.partner_campaign_id
            WHERE d.business_id=? AND d.connection_id=?
              AND d.platform='telegram' AND d.external_subject=?
              AND d.source_kind='partner_outreach' AND d.status='sent'
              AND p.status IN ('contacted','replied','accepted')
            ORDER BY d.sent_at DESC,d.id DESC LIMIT 1
            """,
            (business, connection, subject),
        ).fetchone()
        if row is None:
            return None
        campaign_id = str(_value(row, "partner_campaign_id", 0))
        candidate_id = str(_value(row, "partner_candidate_id", 1))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO partner_reply_events(
                id,business_id,campaign_id,candidate_id,connection_id,platform,
                external_subject,provider_event_key,reply_text,occurred_at
            ) VALUES(?,?,?,?,?,'telegram',?,?,?,?)
            ON CONFLICT(business_id,connection_id,provider_event_key) DO NOTHING
            """,
            (
                str(uuid4()),
                business,
                campaign_id,
                candidate_id,
                connection,
                subject,
                event_key,
                text,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE partner_candidates
            SET status='replied',updated_at=?
            WHERE id=? AND business_id=? AND status='contacted'
            """,
            (now, candidate_id, business),
        )
        return candidate_id


def list_partner_replies(
    *,
    actor: TenantContext,
    candidate_id: str,
    limit: int = 20,
) -> list[PartnerReplyView]:
    normalized = normalize_uuid(candidate_id, field_name="partner_candidate_id")
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        candidate = PartnerRepository(conn).get_candidate(
            actor=current,
            candidate_id=normalized,
        )
        rows = conn.execute(
            """
            SELECT occurred_at,reply_text
            FROM partner_reply_events
            WHERE business_id=? AND candidate_id=?
            ORDER BY occurred_at DESC,id DESC LIMIT ?
            """,
            (
                current.business_id,
                candidate.id,
                max(1, min(int(limit), 100)),
            ),
        ).fetchall()
        return [
            PartnerReplyView(
                occurred_at=str(_value(row, "occurred_at", 0)),
                reply_text=str(_value(row, "reply_text", 1) or ""),
            )
            for row in rows
        ]


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


__all__ = [
    "PartnerCandidateView",
    "PartnerReplyView",
    "PartnerSendConnection",
    "approve_and_queue_partner_email_outreach",
    "authorize_partner_telegram_contact",
    "get_partner_candidate_view",
    "list_partner_campaigns",
    "list_partner_candidates",
    "list_partner_replies",
    "list_partner_send_connections",
    "partner_stats",
    "queue_partner_outreach",
    "record_partner_reply_if_expected",
    "rerun_connected_partner_campaign",
    "set_partner_candidate_status",
    "start_connected_partner_campaign",
]
