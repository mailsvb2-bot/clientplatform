from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clientplatform.application.event_customer_projection import resolve_event_automation_actor
from clientplatform.domain.customers import CustomerPlatform
from clientplatform.domain.events import EventRegistration
from clientplatform.infrastructure.event_repository import EventRepository
from clientplatform.infrastructure.messenger_channel_repository import MessengerChannelRepository


@dataclass(frozen=True, slots=True)
class EventReminderChannelLink:
    platform: str
    token: str
    expires_at: str


def issue_event_reminder_channel_links_in_transaction(
    conn: Any,
    *,
    registration: EventRegistration,
    ttl_seconds: int = 3600,
    skip_platforms: tuple[str, ...] = (),
) -> tuple[EventReminderChannelLink, ...]:
    """Issue short-lived canonical channel-link tokens after a public registration.

    The public form never writes a messenger identity directly. It may only ask
    the existing canonical customer-link repository for one platform-bound token.
    The participant must then open that messenger and consume the token there.
    """

    if registration.status != "registered" or not registration.customer_id:
        return ()
    row = conn.execute(
        """
        SELECT public_slug
        FROM clientplatform_events
        WHERE id=? AND business_id=? AND status='published'
        LIMIT 1
        """,
        (registration.event_id, registration.business_id),
    ).fetchone()
    if row is None:
        return ()
    public_slug = str(row["public_slug"] if hasattr(row, "keys") else row[0])
    event = EventRepository(conn).get_public_owner_event(public_slug=public_slug)
    actor = resolve_event_automation_actor(conn, event=event)
    if actor is None:
        return ()

    repository = MessengerChannelRepository(conn)
    links: list[EventReminderChannelLink] = []
    skipped = {str(item).strip().lower() for item in skip_platforms}
    for platform in (
        CustomerPlatform.TELEGRAM,
        CustomerPlatform.VK,
        CustomerPlatform.MAX,
    ):
        if platform.value in skipped:
            continue
        issued = repository.issue_customer_link(
            actor=actor,
            customer_id=registration.customer_id,
            target_platform=platform,
            ttl_seconds=ttl_seconds,
        )
        links.append(
            EventReminderChannelLink(
                platform=platform.value,
                token=issued.token,
                expires_at=issued.expires_at,
            )
        )
    return tuple(links)


__all__ = ["EventReminderChannelLink", "issue_event_reminder_channel_links_in_transaction"]
