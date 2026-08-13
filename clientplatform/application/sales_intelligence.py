from __future__ import annotations

import re
from typing import Any, Mapping

from clientplatform.application.sales_ai_settings import business_sales_ai_enabled_in_conn
from clientplatform.application.sales_orchestration import orchestrate_sales_signal_in_transaction
from clientplatform.domain.bookings import CustomerBusinessLink
from clientplatform.domain.bot_gateway import ManagedBotRoute
from clientplatform.domain.sales import ContactBasis
from clientplatform.domain.sales_state_machine import SalesConversationEvent
from clientplatform.infrastructure.sales_ai_job_repository import SalesAIJobRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db


_MAX_CAPTURED_MESSAGE_CHARS = 12_000


def extract_customer_message_text(payload: Mapping[str, Any]) -> str | None:
    """Extract bounded human text from one Telegram update.

    Commands are routing instructions, not sales language, so they deliberately do
    not enter the advisory model queue. Text/captions remain customer evidence only
    when the business has current consent for the configured AI target.
    """

    message: Mapping[str, Any] | None = None
    for key in ("message", "edited_message"):
        candidate = payload.get(key)
        if isinstance(candidate, Mapping):
            message = candidate
            break
    if message is None:
        return None
    raw = message.get("text")
    if raw is None:
        raw = message.get("caption")
    normalized = re.sub(r"\s+", " ", str(raw or "").replace("\x00", " ")).strip()
    if not normalized or normalized.startswith("/"):
        return None
    if len(normalized) > _MAX_CAPTURED_MESSAGE_CHARS:
        normalized = normalized[:_MAX_CAPTURED_MESSAGE_CHARS].rstrip()
    return normalized or None


def _owner_actor(conn: Any, *, business_id: str):
    row = conn.execute(
        """
        SELECT user_id
        FROM business_members
        WHERE business_id=? AND role='owner' AND status='active'
        ORDER BY created_at, id
        LIMIT 1
        """,
        (business_id,),
    ).fetchone()
    if row is None:
        raise ValueError("active business owner is required for sales evidence")
    user_id = row["user_id"] if hasattr(row, "keys") else row[0]
    return TenancyRepository(conn).resolve_context(
        user_id=int(user_id),
        business_id=business_id,
    )


def record_managed_bot_customer_message(
    *,
    route: ManagedBotRoute,
    customer_link: CustomerBusinessLink,
    telegram_user_id: int,
    provider_update_id: str,
    message_text: str,
    runtime_ai_enabled: bool,
    runtime_ai_consent_target: str,
) -> str:
    """Persist deterministic inbound sales evidence and optionally queue AI.

    The sales funnel must work even with AI disabled. Raw message text is persisted
    into the longer-lived sales event store only when both the deployment and the
    tenant have current consent for the configured provider target. No model/network
    call happens in this critical Managed Bot Gateway transaction.
    """

    if route.business_id != customer_link.business_id:
        raise ValueError("managed bot route and customer link belong to different businesses")
    if isinstance(telegram_user_id, bool) or not isinstance(telegram_user_id, int) or telegram_user_id <= 0:
        raise ValueError("telegram_user_id must be a positive integer")
    update_id = str(provider_update_id or "").strip()
    if not update_id.isdigit() or len(update_id) > 32:
        raise ValueError("provider_update_id must be a positive decimal identifier")
    text = re.sub(r"\s+", " ", str(message_text or "").replace("\x00", " ")).strip()
    if not text or len(text) > _MAX_CAPTURED_MESSAGE_CHARS:
        raise ValueError("message_text must be 1..12000 characters")
    if not isinstance(runtime_ai_enabled, bool):
        raise ValueError("runtime_ai_enabled must be a boolean")
    consent_target = str(runtime_ai_consent_target or "").strip()

    opportunity_key = f"managed-bot:{route.managed_bot_id}:telegram:{telegram_user_id}"
    source_event_key = f"managed-bot-message:{route.managed_bot_id}:{update_id}"
    transition_key = f"managed-bot-inbound:{route.managed_bot_id}:{update_id}"

    with get_db() as conn:
        actor = _owner_actor(conn, business_id=route.business_id)
        sales = SalesRepository(conn)
        lead = sales.create_or_refresh_lead(
            actor=actor,
            opportunity_key=opportunity_key,
            customer_id=customer_link.customer_id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
            source_ref=f"managed_bot:{route.managed_bot_id}",
        )

        ai_allowed = runtime_ai_enabled and business_sales_ai_enabled_in_conn(
            conn,
            business_id=route.business_id,
            consent_target=consent_target,
        )
        if ai_allowed:
            inserted = sales.record_event(
                actor=actor,
                lead_id=lead.id,
                event_type="customer_message",
                dedupe_key=source_event_key,
                payload={"text": text, "channel": "telegram", "surface": "managed_bot"},
            )
            if inserted:
                # Advance/lock freshness before canonical planning. If an older AI
                # result is committing, enqueue waits; once it owns the head, the
                # deterministic plan below supersedes whatever the old worker wrote.
                SalesAIJobRepository(conn).enqueue(
                    business_id=route.business_id,
                    lead_id=lead.id,
                    source_event_dedupe_key=source_event_key,
                    source_order=update_id,
                )

        # Always keep deterministic sales behaviour alive, irrespective of AI.
        # This creates the immediate safe RESPOND plan for a real inbound message;
        # a later fresh AI analysis may refine it through the same canonical #120
        # orchestrator and therefore atomically supersede this plan.
        orchestrate_sales_signal_in_transaction(
            conn=conn,
            actor=actor,
            lead_id=lead.id,
            event=SalesConversationEvent.INBOUND_RECEIVED,
            dedupe_key=transition_key,
            metadata={"channel": "telegram", "surface": "managed_bot"},
            model_confidence=1.0,
            unanswered_inbound=True,
        )
        return lead.id


__all__ = ["extract_customer_message_text", "record_managed_bot_customer_message"]
