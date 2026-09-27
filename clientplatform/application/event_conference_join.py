from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Mapping
from uuid import UUID, uuid4

from clientplatform.domain.event_sessions import EventSession
from clientplatform.domain.events import Event, EventRegistration
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.event_repository import EventRepository
from clientplatform.infrastructure.event_session_repository import EventSessionRepository
from clientplatform.runtime.conference_provider import (
    ConferenceCreateSpec,
    ConferenceJoin,
    ConferenceLifecycle,
    ConferenceMode,
    ConferenceProvider,
    ConferenceRef,
    ConferenceRole,
)
from clientplatform.runtime.ucr_conference_provider import UcrConferenceProvider
from clientplatform.runtime.ucr_gateway import (
    UcrGatewayClient,
    UcrGatewayConfigurationError,
    ucr_gateway_config,
)
from services.db import get_db_ro


UCR_EVENT_PROVIDER_KEY = "ucr"
_DEFAULT_JOIN_TTL_SECONDS = 15 * 60


def ucr_conference_integration_id(
    environment: Mapping[str, str] | None = None,
) -> str:
    env = os.environ if environment is None else environment
    value = str(env.get("CLIENTPLATFORM_UCR_CONFERENCE_INTEGRATION_ID") or "").strip()
    if not value or len(value) > 128:
        raise UcrGatewayConfigurationError("UCR conference integration id is required")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise UcrGatewayConfigurationError("UCR conference integration id is invalid")
    return value


def ucr_managed_event_provider_available(
    environment: Mapping[str, str] | None = None,
) -> bool:
    try:
        config = ucr_gateway_config(environment)
        if not config.enabled:
            return False
        ucr_conference_integration_id(environment)
    except UcrGatewayConfigurationError:
        return False
    return True


def is_managed_event_session_provider(session: EventSession) -> bool:
    return session.provider_key == UCR_EVENT_PROVIDER_KEY


def _validated_attempt_id(value: str | None) -> str:
    if value is None:
        return str(uuid4())
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("conference join attempt id is invalid") from exc
    return str(parsed)


def _validate_event_session(*, event: Event, session: EventSession) -> None:
    if not is_managed_event_session_provider(session):
        raise ValueError("event session is not managed by UCR")
    if event.business_id != session.business_id or event.id != session.event_id:
        raise ValueError("event session does not belong to the event")
    if event.status != "published":
        raise ValueError("event is not active")


def _conference_external_id(*, event: Event, session: EventSession) -> str:
    # Event + position is stable when schedule edits replace EventSession rows.
    return f"clientplatform:event:{event.id}:session:{session.position}"


async def _prepare_managed_conference(
    *,
    event: Event,
    session: EventSession,
    provider: ConferenceProvider,
) -> tuple[ConferenceRef, str]:
    _validate_event_session(event=event, session=session)
    external_id = _conference_external_id(event=event, session=session)
    conference = await provider.create(
        ConferenceCreateSpec(
            tenant_id=session.business_id,
            external_conference_id=external_id,
            mode=ConferenceMode.WEBINAR,
            # UCR conference schedule is the stable provider-room lifetime, not the
            # editable ClientPlatform EventSession schedule. ClientPlatform remains
            # the authority for the webinar's actual day/time.
            starts_at_unix_ms=max(1, int(event.created_at.timestamp() * 1000)),
            planned_end_unix_ms=None,
        ),
        idempotency_key=f"conference:{event.id}:{session.position}",
    )
    owner_external_id = f"clientplatform:event-owner:{event.created_by_member_id}"
    await provider.ensure_participant(
        conference,
        tenant_id=session.business_id,
        external_user_id=owner_external_id,
        role=ConferenceRole.OWNER,
        idempotency_key=f"owner:{event.id}:{session.position}",
    )
    return conference, owner_external_id


async def _open_and_prepare(
    *,
    provider: ConferenceProvider,
    conference: ConferenceRef,
    event: Event,
    session: EventSession,
    reconciler_external_id: str,
) -> None:
    reconcile_key = "runtime:" + hashlib.sha256(
        f"{event.id}:{session.position}:{reconciler_external_id}".encode("utf-8")
    ).hexdigest()
    await provider.prepare(
        conference,
        tenant_id=session.business_id,
        idempotency_key=reconcile_key,
    )
    if conference.lifecycle in {ConferenceLifecycle.ENDING, ConferenceLifecycle.ENDED}:
        raise ValueError("managed conference is no longer joinable")
    if conference.lifecycle in {None, ConferenceLifecycle.SCHEDULED}:
        conference = await provider.set_lifecycle(
            conference,
            tenant_id=session.business_id,
            lifecycle=ConferenceLifecycle.WAITING,
            idempotency_key=f"waiting:{event.id}:{session.position}",
        )
    await provider.set_entry_open(
        conference,
        tenant_id=session.business_id,
        open=True,
        idempotency_key=f"entry-open:{event.id}:{session.position}",
    )


async def issue_managed_event_session_join(
    *,
    registration: EventRegistration,
    event: Event,
    session: EventSession,
    provider: ConferenceProvider | None = None,
    join_attempt_id: str | None = None,
    ttl_seconds: int = _DEFAULT_JOIN_TTL_SECONDS,
) -> ConferenceJoin:
    """Issue one attendee-specific managed join without persisting the grant URL."""

    _validate_event_session(event=event, session=session)
    if (
        registration.business_id != session.business_id
        or registration.event_id != session.event_id
    ):
        raise ValueError("event registration does not belong to the selected session")
    if registration.status != "registered":
        raise ValueError("event registration is not active")

    managed = provider or UcrConferenceProvider(
        gateway=UcrGatewayClient(),
        integration_id=ucr_conference_integration_id(),
    )
    conference, _owner_external_id = await _prepare_managed_conference(
        event=event,
        session=session,
        provider=managed,
    )
    participant_external_id = f"clientplatform:event-registration:{registration.id}"
    await managed.ensure_participant(
        conference,
        tenant_id=session.business_id,
        external_user_id=participant_external_id,
        role=ConferenceRole.ATTENDEE,
        idempotency_key=f"participant:{event.id}:{session.position}:{registration.id}",
    )
    await _open_and_prepare(
        provider=managed,
        conference=conference,
        event=event,
        session=session,
        reconciler_external_id=participant_external_id,
    )
    attempt = _validated_attempt_id(join_attempt_id)
    return await managed.issue_join(
        conference,
        tenant_id=session.business_id,
        external_user_id=participant_external_id,
        idempotency_key=(
            f"join:{event.id}:{session.position}:{registration.id}:{attempt}"
        ),
        ttl_seconds=ttl_seconds,
    )


async def issue_managed_event_owner_join(
    *,
    actor: TenantContext,
    event: Event,
    session: EventSession,
    provider: ConferenceProvider | None = None,
    join_attempt_id: str | None = None,
    ttl_seconds: int = _DEFAULT_JOIN_TTL_SECONDS,
) -> ConferenceJoin:
    """Issue a managed owner/host join for an authorized ClientPlatform manager."""

    actor.assert_can_manage_business()
    actor.assert_business(event.business_id)
    _validate_event_session(event=event, session=session)
    managed = provider or UcrConferenceProvider(
        gateway=UcrGatewayClient(),
        integration_id=ucr_conference_integration_id(),
    )
    conference, owner_external_id = await _prepare_managed_conference(
        event=event,
        session=session,
        provider=managed,
    )
    if actor.membership_id == event.created_by_member_id:
        external_user_id = owner_external_id
    else:
        external_user_id = f"clientplatform:event-host:{actor.membership_id}"
        await managed.ensure_participant(
            conference,
            tenant_id=session.business_id,
            external_user_id=external_user_id,
            role=ConferenceRole.HOST,
            idempotency_key=(
                f"host:{event.id}:{session.position}:{actor.membership_id}"
            ),
        )
    await _open_and_prepare(
        provider=managed,
        conference=conference,
        event=event,
        session=session,
        reconciler_external_id=external_user_id,
    )
    attempt = _validated_attempt_id(join_attempt_id)
    return await managed.issue_join(
        conference,
        tenant_id=session.business_id,
        external_user_id=external_user_id,
        idempotency_key=(
            f"host-join:{event.id}:{session.position}:{actor.membership_id}:{attempt}"
        ),
        ttl_seconds=ttl_seconds,
    )


def _owner_join_context(
    *,
    actor: TenantContext,
    event_id: str,
    position: int,
) -> tuple[Event, EventSession]:
    actor.assert_can_manage_business()
    with get_db_ro() as conn:
        event = EventRepository(conn).get(actor=actor, event_id=event_id)
        sessions = EventSessionRepository(conn).list_for_event_record(event=event)
    for session in sessions:
        if session.position == position:
            return event, session
    raise LookupError("event session not found")


async def issue_managed_event_owner_join_for_position(
    *,
    actor: TenantContext,
    event_id: str,
    position: int,
) -> ConferenceJoin:
    event, session = await asyncio.to_thread(
        _owner_join_context,
        actor=actor,
        event_id=event_id,
        position=position,
    )
    return await issue_managed_event_owner_join(
        actor=actor,
        event=event,
        session=session,
    )


__all__ = [
    "UCR_EVENT_PROVIDER_KEY",
    "is_managed_event_session_provider",
    "issue_managed_event_owner_join",
    "issue_managed_event_owner_join_for_position",
    "issue_managed_event_session_join",
    "ucr_conference_integration_id",
    "ucr_managed_event_provider_available",
]
