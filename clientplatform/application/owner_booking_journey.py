from __future__ import annotations

"""Owner-facing booking lifecycle and public storefront connection boundary."""

from datetime import datetime, timezone

from clientplatform.application.customer_role_guard import active_member_business_ids
from clientplatform.application.sales_orchestration import (
    orchestrate_sales_signal_in_transaction,
)
from clientplatform.domain.bookings import (
    BookingInvariantViolation,
    BookingNotFound,
    BookingSlotStatus,
    BookingSlotView,
    CustomerBusinessLink,
    normalize_telegram_principal,
)
from clientplatform.domain.customers import CustomerNotFound, CustomerPlatform
from clientplatform.domain.sales import ContactBasis
from clientplatform.domain.sales_state_machine import SalesConversationEvent
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.sales_followup_repository import SalesFollowupRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db, get_db_ro


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_owner_booking_slot(*, actor: TenantContext, slot_id: str) -> BookingSlotView:
    """Return one tenant-scoped slot after re-resolving the live membership."""

    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_customer_records()
        return BookingRepository(conn).get_slot(actor=current, slot_id=slot_id)


def cancel_owner_booking_slot(*, actor: TenantContext, slot_id: str) -> BookingSlotView:
    """Cancel an unclaimed future slot without permitting silent booking loss."""

    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_programs()
        repository = BookingRepository(conn)
        current_slot = repository.get_slot(actor=current, slot_id=slot_id)
        if current_slot.slot.status == BookingSlotStatus.BOOKED:
            raise BookingInvariantViolation(
                "На это время уже записан клиент. Сначала свяжитесь с ним — "
                "занятую запись нельзя удалить молча."
            )
        if current_slot.slot.status != BookingSlotStatus.OPEN:
            return current_slot
        timestamp = _utc_now()
        cursor = conn.execute(
            """
            UPDATE booking_slots
            SET status='cancelled', cancelled_at=?, updated_at=?
            WHERE id=? AND business_id=? AND status='open'
            """,
            (
                timestamp,
                timestamp,
                current_slot.slot.id,
                current.business_id,
            ),
        )
        if int(getattr(cursor, "rowcount", 1) or 0) != 1:
            raise BookingInvariantViolation(
                "Статус времени только что изменился. Откройте календарь ещё раз."
            )
        return repository.get_slot(actor=current, slot_id=current_slot.slot.id)


def replace_owner_booking_slot(
    *,
    actor: TenantContext,
    slot_id: str,
    local_start: str,
    duration_minutes: int,
) -> BookingSlotView:
    """Atomically replace one open slot, rolling back cancellation on failure."""

    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_programs()
        repository = BookingRepository(conn)
        old = repository.get_slot(actor=current, slot_id=slot_id)
        if old.slot.status == BookingSlotStatus.BOOKED:
            raise BookingInvariantViolation(
                "На это время уже записан клиент. Занятую запись нельзя перенести "
                "без согласования с клиентом."
            )
        if old.slot.status != BookingSlotStatus.OPEN:
            raise BookingInvariantViolation("Изменять можно только свободное опубликованное время")
        timestamp = _utc_now()
        cursor = conn.execute(
            """
            UPDATE booking_slots
            SET status='cancelled', cancelled_at=?, updated_at=?
            WHERE id=? AND business_id=? AND status='open'
            """,
            (timestamp, timestamp, old.slot.id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 1) or 0) != 1:
            raise BookingInvariantViolation(
                "Статус времени только что изменился. Откройте календарь ещё раз."
            )
        return repository.create_slot(
            actor=current,
            offering_id=old.slot.offering_id,
            local_start=local_start,
            duration_minutes=duration_minutes,
        )


def is_public_storefront_staff(
    *,
    business_id: str,
    telegram_user_id: int,
) -> bool:
    """Return whether this principal is active staff of the public-link tenant."""

    normalized_business_id = normalize_uuid(business_id, field_name="business_id")
    principal_id = normalize_telegram_principal(telegram_user_id)
    with get_db_ro() as conn:
        return normalized_business_id in active_member_business_ids(
            conn,
            telegram_user_id=principal_id,
        )


def connect_public_storefront_customer(
    *,
    business_id: str,
    telegram_user_id: int,
    username: str | None,
    display_name: str | None,
) -> CustomerBusinessLink:
    """Idempotently connect a Telegram visitor who opened a public business link.

    The public storefront intentionally makes the business discoverable. It does
    not grant staff access and refuses to turn an owner or employee into a
    customer of the same tenant. A genuine public visit is persisted as replay-
    safe inbound sales evidence and immediately flows through the internal sales
    orchestrator. The orchestrator may plan work, choose a commercial candidate
    or open a handoff, but it never sends an external message without owner
    approval.
    """

    normalized_business_id = normalize_uuid(business_id, field_name="business_id")
    principal_id = normalize_telegram_principal(telegram_user_id)
    with get_db() as conn:
        if normalized_business_id in active_member_business_ids(
            conn,
            telegram_user_id=principal_id,
        ):
            raise ValueError(
                "Это публичная ссылка для клиентов. Владелец и сотрудники могут "
                "проверить её через кнопку «Посмотреть глазами клиента»."
            )
        row = conn.execute(
            """
            SELECT b.name, bm.user_id
            FROM businesses b
            JOIN business_members bm
              ON bm.business_id=b.id
             AND bm.role='owner'
             AND bm.status='active'
            WHERE b.id=? AND b.status='active'
            ORDER BY bm.created_at, bm.id
            LIMIT 1
            """,
            (normalized_business_id,),
        ).fetchone()
        if row is None:
            raise BookingNotFound("Публичная страница этого бизнеса больше недоступна")
        business_name = str(row["name"] if hasattr(row, "keys") else row[0])
        owner_user_id = int(row["user_id"] if hasattr(row, "keys") else row[1])
        actor = TenancyRepository(conn).resolve_context(
            user_id=owner_user_id,
            business_id=normalized_business_id,
        )
        customers = CustomerRepository(conn)
        try:
            record = customers.find_by_identity(
                actor=actor,
                platform=CustomerPlatform.TELEGRAM,
                external_subject=str(principal_id),
            )
            customer_id = record.customer.id
        except CustomerNotFound:
            customer = customers.create_customer(actor=actor, display_name=display_name)
            customers.attach_identity(
                actor=actor,
                customer_id=customer.id,
                platform=CustomerPlatform.TELEGRAM,
                external_subject=str(principal_id),
                username=username,
                display_name=display_name,
            )
            customer_id = customer.id

        # One Telegram visitor has one stable public-storefront opportunity per
        # business. Reopening the same permanent link refreshes signal time but
        # the stable transition dedupe key prevents duplicate orchestration.
        lead = SalesRepository(conn).create_or_refresh_lead(
            actor=actor,
            opportunity_key=f"public-storefront:telegram:{principal_id}",
            customer_id=customer_id,
            source_kind="telegram",
            contact_basis=ContactBasis.INBOUND,
            source_ref="public_storefront",
        )
        SalesFollowupRepository(conn).stop_for_inbound(
            business_id=normalized_business_id,
            lead_id=lead.id,
        )
        orchestrate_sales_signal_in_transaction(
            conn=conn,
            actor=actor,
            lead_id=lead.id,
            event=SalesConversationEvent.INBOUND_RECEIVED,
            dedupe_key=f"public-storefront-open:{principal_id}",
            metadata={"channel": "telegram", "surface": "public_storefront"},
            # Opening the permanent storefront link is deterministic inbound
            # evidence; no uncertain semantic classification is being invented.
            model_confidence=1.0,
            unanswered_inbound=True,
        )
        return CustomerBusinessLink(
            business_id=normalized_business_id,
            business_name=business_name,
            customer_id=customer_id,
        )


__all__ = [
    "cancel_owner_booking_slot",
    "connect_public_storefront_customer",
    "get_owner_booking_slot",
    "is_public_storefront_staff",
    "replace_owner_booking_slot",
]
