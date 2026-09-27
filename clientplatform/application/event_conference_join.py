from __future__ import annotations

import os
from collections.abc import Mapping
from uuid import UUID, uuid4

from clientplatform.domain.event_sessions import EventSession
from clientplatform.domain.events import EventRegistration
from clientplatform.runtime.conference_provider import (
    ConferenceCreateSpec,
    ConferenceJoin,
    ConferenceMode,
    ConferenceProvider,
    ConferenceRole,
)
from clientplatform.runtime.ucr_conference_provider import UcrConferenceProvider
from clientplatform.runtime.ucr_gateway import (
    UcrGatewayClient,
    UcrGatewayConfigurationError,
    ucr_gateway_config,
)


UCR_EVENT_PROVIDER_KEY = "ucr"
_DEFAULT_JOIN_TTL_SECONDS = 4 * 60 * 60


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


async def issue_managed_event_session_join(
    *,
    registration: EventRegistration,
    session: EventSession,
    provider: ConferenceProvider | None = None,
    join_attempt_id: str | None = None,
    ttl_seconds: int = _DEFAULT_JOIN_TTL_SECONDS,
) -> ConferenceJoin:
    """Issue one participant-specific managed join without persisting the grant URL."""

    if not is_managed_event_session_provider(session):
        raise ValueError("event session is not managed by UCR")
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
    conference_external_id = f"clientplatform:event-session:{session.id}"
    participant_external_id = f"clientplatform:event-registration:{registration.id}"

    conference = await managed.create(
        ConferenceCreateSpec(
            tenant_id=session.business_id,
            external_conference_id=conference_external_id,
            mode=ConferenceMode.WEBINAR,
            starts_at_unix_ms=int(session.starts_at.timestamp() * 1000),
            planned_end_unix_ms=(
                None
                if session.ends_at is None
                else int(session.ends_at.timestamp() * 1000)
            ),
        ),
        idempotency_key=f"conference:{session.id}",
    )
    await managed.ensure_participant(
        conference,
        tenant_id=session.business_id,
        external_user_id=participant_external_id,
        role=ConferenceRole.ATTENDEE,
        idempotency_key=f"participant:{session.id}:{registration.id}",
    )
    await managed.prepare(
        conference,
        tenant_id=session.business_id,
        idempotency_key=f"runtime:{session.id}",
    )
    attempt = _validated_attempt_id(join_attempt_id)
    return await managed.issue_join(
        conference,
        tenant_id=session.business_id,
        external_user_id=participant_external_id,
        idempotency_key=f"join:{session.id}:{registration.id}:{attempt}",
        ttl_seconds=ttl_seconds,
    )


__all__ = [
    "UCR_EVENT_PROVIDER_KEY",
    "is_managed_event_session_provider",
    "issue_managed_event_session_join",
    "ucr_conference_integration_id",
    "ucr_managed_event_provider_available",
]
