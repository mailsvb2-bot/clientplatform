from __future__ import annotations

import re
from typing import Any, Mapping

from clientplatform.application.sales_ai_settings import business_sales_ai_enabled_in_conn
from clientplatform.application.sales_orchestration import orchestrate_sales_signal_in_transaction
from clientplatform.domain.bookings import CustomerBusinessLink
from clientplatform.domain.bot_gateway import ManagedBotRoute
from clientplatform.domain.customers import CustomerPlatform, normalize_platform
from clientplatform.domain.sales import ContactBasis
from clientplatform.domain.sales_ai_jobs import normalize_sales_ai_source_order
from clientplatform.domain.sales_state_machine import SalesConversationEvent
from clientplatform.domain.tenancy import normalize_uuid
from clientplatform.infrastructure.sales_ai_job_repository import SalesAIJobRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db


_MAX_CAPTURED_MESSAGE_CHARS = 12_000


def extract_customer_message_text(payload: Mapping[str, Any]) -> str | None:
    """Extract bounded human text from one Telegram update."""

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
    return normalize_customer_message_text(raw)


def normalize_customer_message_text(value: object) -> str | None:
    normalized = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
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


def _printable(value: str, *, field_name: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must be 1..{maximum} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError(f"{field_name} contains control characters")
    return normalized


def record_customer_channel_message(
    *,
    business_id: str,
    customer_id: str,
    platform: CustomerPlatform | str,
    external_subject: str,
    source_ref: str,
    provider_event_id: str,
    source_order: int | str,
    message_text: str,
    runtime_ai_enabled: bool,
    runtime_ai_consent_target: str,
) -> str:
    """Persist one canonical inbound sales signal for Telegram, VK or MAX.

    Provider identity and ordering remain separate: ``provider_event_id`` provides
    exact dedupe while ``source_order`` preserves monotonic Sales AI freshness.
    No provider-specific account becomes business truth; the durable customer_id
    is always the canonical identity anchor.
    """

    business = normalize_uuid(business_id, field_name="business_id")
    customer = normalize_uuid(customer_id, field_name="customer_id")
    channel = normalize_platform(platform)
    if channel not in {CustomerPlatform.TELEGRAM, CustomerPlatform.VK, CustomerPlatform.MAX}:
        raise ValueError("sales ingress supports only Telegram, VK or MAX")
    subject = _printable(external_subject, field_name="external_subject", maximum=512)
    source = _printable(source_ref, field_name="source_ref", maximum=200)
    event_id = _printable(provider_event_id, field_name="provider_event_id", maximum=160)
    order_key = normalize_sales_ai_source_order(source_order)
    text = normalize_customer_message_text(message_text)
    if text is None:
        raise ValueError("message_text must contain non-command customer text")
    if not isinstance(runtime_ai_enabled, bool):
        raise ValueError("runtime_ai_enabled must be a boolean")
    consent_target = str(runtime_ai_consent_target or "").strip()

    opportunity_key = f"channel:{channel.value}:{source}:{subject}"
    source_event_key = f"customer-message:{channel.value}:{source}:{event_id}"
    transition_key = f"inbound:{channel.value}:{source}:{event_id}"
    if len(opportunity_key) > 240 or len(source_event_key) > 240 or len(transition_key) > 240:
        raise ValueError("channel identity is too long for durable sales dedupe keys")

    with get_db() as conn:
        customer_row = conn.execute(
            "SELECT 1 FROM customers WHERE id=? AND business_id=? AND status='active'",
            (customer, business),
        ).fetchone()
        if customer_row is None:
            raise ValueError("active customer was not found in the business")
        identity_row = conn.execute(
            """
            SELECT 1
            FROM customer_identities
            WHERE business_id=? AND customer_id=? AND platform=?
              AND external_subject=? AND status='active'
            LIMIT 1
            """,
            (business, customer, channel.value, subject),
        ).fetchone()
        if identity_row is None:
            raise ValueError("customer channel identity does not belong to the business")

        actor = _owner_actor(conn, business_id=business)
        sales = SalesRepository(conn)
        lead = sales.create_or_refresh_lead(
            actor=actor,
            opportunity_key=opportunity_key,
            customer_id=customer,
            source_kind=channel.value,
            contact_basis=ContactBasis.INBOUND,
            source_ref=f"{channel.value}:{source}",
        )

        ai_allowed = runtime_ai_enabled and business_sales_ai_enabled_in_conn(
            conn,
            business_id=business,
            consent_target=consent_target,
        )
        if ai_allowed:
            inserted = sales.record_event(
                actor=actor,
                lead_id=lead.id,
                event_type="customer_message",
                dedupe_key=source_event_key,
                payload={
                    "text": text,
                    "channel": channel.value,
                    "surface": "messenger",
                },
            )
            if inserted:
                SalesAIJobRepository(conn).enqueue(
                    business_id=business,
                    lead_id=lead.id,
                    source_event_dedupe_key=source_event_key,
                    source_order=order_key,
                )

        orchestrate_sales_signal_in_transaction(
            conn=conn,
            actor=actor,
            lead_id=lead.id,
            event=SalesConversationEvent.INBOUND_RECEIVED,
            dedupe_key=transition_key,
            metadata={"channel": channel.value, "surface": "messenger"},
            model_confidence=1.0,
            unanswered_inbound=True,
        )
        return lead.id


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
    """Backward-compatible Telegram wrapper over canonical channel evidence."""

    if route.business_id != customer_link.business_id:
        raise ValueError("managed bot route and customer link belong to different businesses")
    if isinstance(telegram_user_id, bool) or not isinstance(telegram_user_id, int) or telegram_user_id <= 0:
        raise ValueError("telegram_user_id must be a positive integer")
    update_id = str(provider_update_id or "").strip()
    if not update_id.isdigit() or len(update_id) > 32:
        raise ValueError("provider_update_id must be a positive decimal identifier")
    return record_customer_channel_message(
        business_id=route.business_id,
        customer_id=customer_link.customer_id,
        platform=CustomerPlatform.TELEGRAM,
        external_subject=str(telegram_user_id),
        source_ref=f"managed-bot:{route.managed_bot_id}",
        provider_event_id=update_id,
        source_order=update_id,
        message_text=message_text,
        runtime_ai_enabled=runtime_ai_enabled,
        runtime_ai_consent_target=runtime_ai_consent_target,
    )


__all__ = [
    "extract_customer_message_text",
    "normalize_customer_message_text",
    "record_customer_channel_message",
    "record_managed_bot_customer_message",
]
