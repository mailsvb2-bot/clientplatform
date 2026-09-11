from __future__ import annotations

import logging
from typing import Any

from clientplatform.domain.customers import CustomerIdentityConflict, CustomerNotFound
from clientplatform.domain.events import Event, EventRegistration
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.event_repository import EventRepository


log = logging.getLogger(__name__)


def _automation_actor(conn: Any, *, event: Event) -> TenantContext | None:
    """Select an active staff principal for canonical CRM writes.

    Prefer the member who created the event. If that membership was later
    revoked, fall back only to an active owner/administrator/manager. Public
    registration never manufactures a membership or bypasses CustomerRepository.
    """

    row = conn.execute(
        """
        SELECT bm.id,bm.user_id,bm.role
        FROM business_members bm
        JOIN businesses b ON b.id=bm.business_id AND b.status='active'
        WHERE bm.business_id=? AND bm.status='active'
          AND bm.role IN ('owner','administrator','manager','support')
        ORDER BY CASE WHEN bm.id=? THEN 0
                      WHEN bm.role='owner' THEN 1
                      WHEN bm.role='administrator' THEN 2
                      WHEN bm.role='manager' THEN 3 ELSE 4 END,
                 bm.created_at,bm.id
        LIMIT 1
        """,
        (event.business_id, event.created_by_member_id),
    ).fetchone()
    if row is None:
        return None
    value = lambda key, index: row[key] if hasattr(row, "keys") else row[index]
    return TenantContext(
        membership_id=str(value("id", 0)),
        business_id=event.business_id,
        user_id=int(value("user_id", 1)),
        role=PlatformRole(str(value("role", 2))),
    )


def attach_event_registration_to_customer(
    conn: Any,
    *,
    event: Event,
    registration: EventRegistration,
) -> str | None:
    """Project a public registration into the canonical CustomerRepository.

    Registration durability must not depend on CRM projection. The caller may
    safely retry this function; e-mail identity uniqueness makes it idempotent.
    """

    actor = _automation_actor(conn, event=event)
    if actor is None:
        log.warning(
            "Event registration CRM projection skipped: no active staff principal",
            extra={"business_id": event.business_id, "event_id": event.id},
        )
        return None
    customers = CustomerRepository(conn)
    try:
        record = customers.find_by_identity(
            actor=actor,
            platform="email",
            external_subject=registration.email,
        )
        customer = record.customer
    except CustomerNotFound:
        customer = customers.create_customer(actor=actor, display_name=registration.name)
        customers.attach_identity(
            actor=actor,
            customer_id=customer.id,
            platform="email",
            external_subject=registration.email,
            display_name=registration.name,
        )
    if registration.phone:
        try:
            customers.attach_identity(
                actor=actor,
                customer_id=customer.id,
                platform="phone",
                external_subject=registration.phone,
                display_name=registration.name,
            )
        except CustomerIdentityConflict:
            # Never merge two canonical CRM customers merely because a public
            # form reused a phone number. E-mail remains the event identity.
            log.info(
                "Event registration phone identity belongs to another customer",
                extra={"business_id": event.business_id, "event_id": event.id},
            )
    EventRepository(conn).attach_customer(
        business_id=event.business_id,
        registration_id=registration.id,
        customer_id=customer.id,
    )
    return customer.id


__all__ = ["attach_event_registration_to_customer"]
