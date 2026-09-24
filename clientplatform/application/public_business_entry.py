from __future__ import annotations

"""Public website lead capture into the canonical ClientPlatform customer/sales model."""

from dataclasses import dataclass

from clientplatform.application.control_callbacks import token_uuid, uuid_token
from clientplatform.application.sales_orchestration import (
    orchestrate_sales_signal_in_transaction,
)
from clientplatform.domain.customers import (
    CustomerIdentityConflict,
    CustomerNotFound,
    CustomerPlatform,
    normalize_identity_subject,
)
from clientplatform.domain.sales import ContactBasis
from clientplatform.domain.sales_state_machine import SalesConversationEvent
from clientplatform.domain.tenancy import normalize_uuid
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.sales_followup_repository import SalesFollowupRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db, get_db_ro


@dataclass(frozen=True, slots=True)
class PublicBusinessEntry:
    business_id: str
    business_token: str
    business_name: str
    activity_description: str


@dataclass(frozen=True, slots=True)
class PublicBusinessLeadReceipt:
    business_id: str
    customer_id: str
    lead_id: str


def _bounded(value: object, *, maximum: int) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split())
    if len(normalized) > maximum:
        raise ValueError("public business entry value is too long")
    return normalized


def _active_business_row(conn, business_id: str):
    return conn.execute(
        """
        SELECT b.id, b.name, bm.user_id,
               COALESCE(bp.activity_description, '') AS activity_description
        FROM businesses b
        JOIN business_members bm
          ON bm.business_id=b.id
         AND bm.role='owner'
         AND bm.status='active'
        LEFT JOIN business_profiles bp ON bp.business_id=b.id
        WHERE b.id=? AND b.status='active'
        ORDER BY bm.created_at, bm.id
        LIMIT 1
        """,
        (business_id,),
    ).fetchone()


def get_public_business_entry(*, business_token: str) -> PublicBusinessEntry:
    try:
        business_id = normalize_uuid(
            token_uuid(str(business_token or "").strip()),
            field_name="business_id",
        )
    except (TypeError, ValueError):
        raise ValueError("public business entry is invalid") from None
    with get_db_ro() as conn:
        row = _active_business_row(conn, business_id)
        if row is None:
            raise ValueError("public business entry is unavailable")
        return PublicBusinessEntry(
            business_id=business_id,
            business_token=uuid_token(business_id),
            business_name=str(row["name"] if hasattr(row, "keys") else row[1]),
            activity_description=str(
                row["activity_description"] if hasattr(row, "keys") else row[3]
            ).strip(),
        )


def public_business_entry_url(*, public_base_url: str, business_id: str) -> str:
    base = str(public_base_url or "").strip().rstrip("/")
    if not base.startswith("https://") or " " in base:
        raise ValueError("public_base_url must be HTTPS")
    normalized_business_id = normalize_uuid(business_id, field_name="business_id")
    return f"{base}/clientplatform/b/{uuid_token(normalized_business_id)}"


def capture_public_business_lead(
    *,
    business_token: str,
    display_name: str,
    email: str | None,
    phone: str | None,
    source: str | None = None,
    campaign_ref: str | None = None,
) -> PublicBusinessLeadReceipt:
    """Persist a public web form submission as canonical inbound sales evidence.

    The form owns no parallel CRM state. Email/phone identities are resolved through
    CustomerRepository, and the resulting Customer flows into the existing sales
    state machine. No external message is sent from this boundary.
    """

    entry = get_public_business_entry(business_token=business_token)
    name = _bounded(display_name, maximum=200)
    if not name:
        raise ValueError("display_name is required")
    raw_email = _bounded(email, maximum=320)
    raw_phone = _bounded(phone, maximum=40)
    if not raw_email and not raw_phone:
        raise ValueError("email or phone is required")

    normalized_email: str | None = None
    normalized_phone: str | None = None
    if raw_email:
        _platform, normalized_email = normalize_identity_subject(
            CustomerPlatform.EMAIL,
            raw_email,
        )
        if "@" not in normalized_email or normalized_email.startswith("@") or normalized_email.endswith("@"):
            raise ValueError("email is invalid")
    if raw_phone:
        _platform, normalized_phone = normalize_identity_subject(
            CustomerPlatform.PHONE,
            raw_phone,
        )

    source_value = _bounded(source, maximum=120) or "business_page"
    campaign_value = _bounded(campaign_ref, maximum=200)

    with get_db() as conn:
        row = _active_business_row(conn, entry.business_id)
        if row is None:
            raise ValueError("public business entry is unavailable")
        owner_user_id = int(row["user_id"] if hasattr(row, "keys") else row[2])
        actor = TenancyRepository(conn).resolve_context(
            user_id=owner_user_id,
            business_id=entry.business_id,
        )
        customers = CustomerRepository(conn)

        matched_customer_ids: set[str] = set()
        for platform, subject in (
            (CustomerPlatform.EMAIL, normalized_email),
            (CustomerPlatform.PHONE, normalized_phone),
        ):
            if not subject:
                continue
            try:
                record = customers.find_by_identity(
                    actor=actor,
                    platform=platform,
                    external_subject=subject,
                )
            except CustomerNotFound:
                continue
            matched_customer_ids.add(record.customer.id)

        if len(matched_customer_ids) > 1:
            raise CustomerIdentityConflict(
                "submitted contacts belong to different customers"
            )
        if matched_customer_ids:
            customer_id = next(iter(matched_customer_ids))
        else:
            customer_id = customers.create_customer(
                actor=actor,
                display_name=name,
            ).id

        if normalized_email:
            customers.attach_identity(
                actor=actor,
                customer_id=customer_id,
                platform=CustomerPlatform.EMAIL,
                external_subject=normalized_email,
                display_name=name,
            )
        if normalized_phone:
            customers.attach_identity(
                actor=actor,
                customer_id=customer_id,
                platform=CustomerPlatform.PHONE,
                external_subject=normalized_phone,
                display_name=name,
            )

        stable_subject = (
            f"email:{normalized_email}"
            if normalized_email
            else f"phone:{normalized_phone}"
        )
        lead = SalesRepository(conn).create_or_refresh_lead(
            actor=actor,
            opportunity_key=f"website-form:{stable_subject}",
            customer_id=customer_id,
            source_kind="website",
            contact_basis=ContactBasis.INBOUND,
            source_ref="public_business_entry",
        )
        SalesFollowupRepository(conn).stop_for_inbound(
            business_id=entry.business_id,
            lead_id=lead.id,
        )
        orchestrate_sales_signal_in_transaction(
            conn=conn,
            actor=actor,
            lead_id=lead.id,
            event=SalesConversationEvent.INBOUND_RECEIVED,
            dedupe_key=f"website-form-submit:{stable_subject}",
            metadata={
                "channel": "website",
                "surface": "public_business_entry",
                "source": source_value,
                "campaign_ref": campaign_value,
            },
            model_confidence=1.0,
            unanswered_inbound=True,
        )
        return PublicBusinessLeadReceipt(
            business_id=entry.business_id,
            customer_id=customer_id,
            lead_id=lead.id,
        )


__all__ = [
    "PublicBusinessEntry",
    "PublicBusinessLeadReceipt",
    "capture_public_business_lead",
    "get_public_business_entry",
    "public_business_entry_url",
]
