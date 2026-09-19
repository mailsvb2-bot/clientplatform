from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clientplatform.domain.events import Event, EventRegistration

_CUSTOMER_CONNECTION_TYPES = {
    "telegram": frozenset({"telegram_shared_bot", "telegram_managed_bot", "telegram_business"}),
    "vk": frozenset({"vk_community"}),
    "max": frozenset({"max_shared_bot", "max_personal_bot"}),
}


@dataclass(frozen=True, slots=True)
class EventDeliveryTarget:
    platform: str
    connection_id: str
    recipient_kind: str
    customer_identity_id: str | None
    external_subject: str


def resolve_event_email_target(
    conn: Any,
    *,
    event: Event,
    registration: EventRegistration,
) -> EventDeliveryTarget | None:
    connection_id = event.notification_connection_id
    if not connection_id:
        return None
    row = conn.execute(
        """
        SELECT id FROM connections
        WHERE id=? AND business_id=? AND platform='email'
          AND connection_type='email_smtp' AND status='active'
        LIMIT 1
        """,
        (connection_id, event.business_id),
    ).fetchone()
    if row is None:
        return None
    return EventDeliveryTarget(
        platform="email",
        connection_id=connection_id,
        recipient_kind="external_subject",
        customer_identity_id=None,
        external_subject=registration.email,
    )


def resolve_event_registration_messenger_target(
    conn: Any,
    *,
    business_id: str,
    event_id: str,
    registration_id: str,
    platform: str,
) -> EventDeliveryTarget | None:
    if platform not in {"telegram", "vk", "max"}:
        raise ValueError("unsupported event messenger platform")
    row = conn.execute(
        """
        SELECT external_subject,connection_id
        FROM clientplatform_event_registration_channels
        WHERE business_id=? AND event_id=? AND registration_id=? AND platform=?
        LIMIT 1
        """,
        (business_id, event_id, registration_id, platform),
    ).fetchone()
    if row is None:
        return None
    external_subject = str(
        row["external_subject"] if hasattr(row, "keys") else row[0]
    ).strip()
    if not external_subject:
        return None
    stored_connection_id = (
        row["connection_id"] if hasattr(row, "keys") else row[1]
    )
    allowed_types = _CUSTOMER_CONNECTION_TYPES[platform]
    connection_id: str | None = None
    if stored_connection_id is not None:
        connection = conn.execute(
            """
            SELECT id,connection_type
            FROM connections
            WHERE id=? AND business_id=? AND platform=? AND status='active'
            LIMIT 1
            """,
            (str(stored_connection_id), business_id, platform),
        ).fetchone()
        if connection is not None:
            connection_type = str(
                connection["connection_type"]
                if hasattr(connection, "keys")
                else connection[1]
            )
            if connection_type in allowed_types:
                connection_id = str(
                    connection["id"] if hasattr(connection, "keys") else connection[0]
                )
    if connection_id is None:
        connections = conn.execute(
            """
            SELECT id,connection_type
            FROM connections
            WHERE business_id=? AND platform=? AND status='active'
            ORDER BY created_at,id
            """,
            (business_id, platform),
        ).fetchall()
        eligible = [
            item
            for item in connections
            if str(item["connection_type"] if hasattr(item, "keys") else item[1])
            in allowed_types
        ]
        if len(eligible) != 1:
            return None
        connection_id = str(
            eligible[0]["id"] if hasattr(eligible[0], "keys") else eligible[0][0]
        )
    return EventDeliveryTarget(
        platform=platform,
        connection_id=connection_id,
        recipient_kind="external_subject",
        customer_identity_id=None,
        external_subject=external_subject,
    )


def resolve_event_messenger_target(
    conn: Any,
    *,
    event: Event,
    registration: EventRegistration,
    platform: str,
) -> EventDeliveryTarget | None:
    scoped = resolve_event_registration_messenger_target(
        conn,
        business_id=event.business_id,
        event_id=event.id,
        registration_id=registration.id,
        platform=platform,
    )
    if scoped is not None:
        return scoped
    if platform not in {"telegram", "vk", "max"}:
        raise ValueError("unsupported event messenger platform")
    if not registration.customer_id:
        return None

    history = conn.execute(
        """
        SELECT ci.id AS identity_id,ci.external_subject,d.connection_id
        FROM provider_dispatch_outbox d
        JOIN customer_identities ci
          ON ci.id=d.customer_identity_id AND ci.business_id=d.business_id
         AND ci.platform=d.platform AND ci.status='active'
        JOIN connections c
          ON c.id=d.connection_id AND c.business_id=d.business_id
         AND c.platform=d.platform AND c.status='active'
        WHERE d.business_id=? AND d.platform=? AND ci.customer_id=?
          AND d.status='sent'
        ORDER BY COALESCE(d.sent_at,d.updated_at) DESC,d.id DESC
        LIMIT 1
        """,
        (event.business_id, platform, registration.customer_id),
    ).fetchone()
    if history is not None:
        identity_id = str(
            history["identity_id"] if hasattr(history, "keys") else history[0]
        )
        external_subject = str(
            history["external_subject"] if hasattr(history, "keys") else history[1]
        ).strip()
        connection_id = str(
            history["connection_id"] if hasattr(history, "keys") else history[2]
        )
        if not external_subject:
            return None
    else:
        identities = conn.execute(
            """
            SELECT id,external_subject
            FROM customer_identities
            WHERE business_id=? AND customer_id=? AND platform=? AND status='active'
            ORDER BY COALESCE(last_contact_at,updated_at,created_at) DESC,id DESC
            LIMIT 2
            """,
            (event.business_id, registration.customer_id, platform),
        ).fetchall()
        if len(identities) != 1:
            return None
        identity = identities[0]
        identity_id = str(identity["id"] if hasattr(identity, "keys") else identity[0])
        external_subject = str(
            identity["external_subject"] if hasattr(identity, "keys") else identity[1]
        ).strip()
        if not external_subject:
            return None
        connections = conn.execute(
            """
            SELECT id,connection_type
            FROM connections
            WHERE business_id=? AND platform=? AND status='active'
            ORDER BY created_at,id
            """,
            (event.business_id, platform),
        ).fetchall()
        allowed_types = _CUSTOMER_CONNECTION_TYPES[platform]
        eligible = [
            row
            for row in connections
            if str(row["connection_type"] if hasattr(row, "keys") else row[1])
            in allowed_types
        ]
        if len(eligible) != 1:
            return None
        connection_id = str(
            eligible[0]["id"] if hasattr(eligible[0], "keys") else eligible[0][0]
        )

    return EventDeliveryTarget(
        platform=platform,
        connection_id=connection_id,
        recipient_kind="customer_identity",
        customer_identity_id=identity_id,
        external_subject=external_subject,
    )


def resolve_event_organizational_targets(
    conn: Any,
    *,
    event: Event,
    registration: EventRegistration,
) -> tuple[EventDeliveryTarget, ...]:
    targets: list[EventDeliveryTarget] = []
    email = resolve_event_email_target(conn, event=event, registration=registration)
    if email is not None:
        targets.append(email)
    for platform in ("telegram", "vk", "max"):
        target = resolve_event_messenger_target(
            conn,
            event=event,
            registration=registration,
            platform=platform,
        )
        if target is not None:
            targets.append(target)
    return tuple(targets)


__all__ = [
    "EventDeliveryTarget",
    "resolve_event_email_target",
    "resolve_event_messenger_target",
    "resolve_event_registration_messenger_target",
    "resolve_event_organizational_targets",
]
