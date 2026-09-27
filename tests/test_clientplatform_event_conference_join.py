from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from clientplatform.application.event_conference_join import (
    issue_managed_event_session_join,
    ucr_conference_integration_id,
    ucr_managed_event_provider_available,
)
from clientplatform.domain.event_sessions import EventSession
from clientplatform.domain.events import EventRegistration
from clientplatform.runtime.conference_provider import (
    ConferenceCapabilities,
    ConferenceJoin,
    ConferenceRef,
)


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _session(*, business_id: str | None = None) -> EventSession:
    business = business_id or str(uuid4())
    event_id = str(uuid4())
    return EventSession(
        id=str(uuid4()),
        business_id=business,
        event_id=event_id,
        position=1,
        starts_at=NOW + timedelta(hours=2),
        ends_at=NOW + timedelta(hours=4),
        provider_key="ucr",
        provider_label="ClientPlatform эфир",
        join_url=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _registration(session: EventSession, *, business_id: str | None = None) -> EventRegistration:
    return EventRegistration(
        id=str(uuid4()),
        event_id=session.event_id,
        business_id=business_id or session.business_id,
        customer_id=None,
        status="registered",
        name="Участник",
        email="person@example.test",
        phone="79990000000",
        source=None,
        campaign_ref=None,
        token="A" * 40,
        consent_version="v1",
        consented_at=NOW,
        registered_at=NOW,
    )


class FakeManagedProvider:
    key = "ucr"

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def capabilities(self, *, tenant_id: str) -> ConferenceCapabilities:
        raise AssertionError("not used directly")

    async def create(self, spec, *, idempotency_key: str) -> ConferenceRef:
        self.calls.append(("create", (spec, idempotency_key)))
        return ConferenceRef(
            provider_key="ucr",
            external_conference_id=spec.external_conference_id,
            provider_conference_id="ucr-conference-1",
        )

    async def resolve(self, **kwargs):
        raise AssertionError("not used")

    async def set_lifecycle(self, *args, **kwargs):
        raise AssertionError("not used")

    async def set_entry_open(self, *args, **kwargs):
        raise AssertionError("not used")

    async def ensure_participant(self, conference, **kwargs) -> None:
        self.calls.append(("participant", (conference, kwargs)))

    async def prepare(self, conference, **kwargs) -> None:
        self.calls.append(("prepare", (conference, kwargs)))

    async def issue_join(self, conference, **kwargs) -> ConferenceJoin:
        self.calls.append(("join", (conference, kwargs)))
        return ConferenceJoin(url="https://join.example.test/grant", session_id="session-1")

    async def attendance(self, *args, **kwargs):
        raise AssertionError("not used")


@pytest.mark.asyncio
async def test_managed_join_uses_opaque_registration_identity_and_stable_setup_keys() -> None:
    session = _session()
    registration = _registration(session)
    provider = FakeManagedProvider()
    attempt = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    join = await issue_managed_event_session_join(
        registration=registration,
        session=session,
        provider=provider,
        join_attempt_id=attempt,
    )

    assert join.url == "https://join.example.test/grant"
    assert [name for name, _ in provider.calls] == [
        "create",
        "participant",
        "prepare",
        "join",
    ]
    create_spec, create_key = provider.calls[0][1]
    assert create_spec.tenant_id == session.business_id
    assert create_spec.external_conference_id == f"clientplatform:event-session:{session.id}"
    assert create_key == f"conference:{session.id}"

    _, participant_kwargs = provider.calls[1][1]
    assert participant_kwargs["external_user_id"] == (
        f"clientplatform:event-registration:{registration.id}"
    )
    assert participant_kwargs["idempotency_key"] == (
        f"participant:{session.id}:{registration.id}"
    )
    _, join_kwargs = provider.calls[3][1]
    assert join_kwargs["idempotency_key"].endswith(attempt)

    serialized = repr(provider.calls)
    assert registration.email not in serialized
    assert registration.name not in serialized
    assert str(registration.phone) not in serialized


@pytest.mark.asyncio
async def test_managed_join_rejects_cross_tenant_registration_before_provider_io() -> None:
    session = _session()
    registration = _registration(session, business_id=str(uuid4()))
    provider = FakeManagedProvider()

    with pytest.raises(ValueError, match="does not belong"):
        await issue_managed_event_session_join(
            registration=registration,
            session=session,
            provider=provider,
        )

    assert provider.calls == []


@pytest.mark.asyncio
async def test_managed_join_rejects_non_managed_session_before_provider_io() -> None:
    session = _session()
    external = EventSession(
        id=session.id,
        business_id=session.business_id,
        event_id=session.event_id,
        position=session.position,
        starts_at=session.starts_at,
        ends_at=session.ends_at,
        provider_key="external",
        provider_label=None,
        join_url="https://room.example.test/live",
        created_at=session.created_at,
        updated_at=session.updated_at,
    )
    registration = _registration(external)
    provider = FakeManagedProvider()

    with pytest.raises(ValueError, match="not managed"):
        await issue_managed_event_session_join(
            registration=registration,
            session=external,
            provider=provider,
        )

    assert provider.calls == []


def test_ucr_managed_venue_requires_both_gateway_and_integration_id() -> None:
    base = {
        "CLIENTPLATFORM_UCR_GATEWAY_URL": "https://gateway.example.test",
    }
    assert not ucr_managed_event_provider_available(base)
    assert not ucr_managed_event_provider_available(
        {**base, "CLIENTPLATFORM_UCR_GATEWAY_ENABLED": "true"}
    )
    enabled = {
        **base,
        "CLIENTPLATFORM_UCR_GATEWAY_ENABLED": "true",
        "CLIENTPLATFORM_UCR_CONFERENCE_INTEGRATION_ID": "clientplatform-prod",
    }
    assert ucr_managed_event_provider_available(enabled)
    assert ucr_conference_integration_id(enabled) == "clientplatform-prod"


def test_ucr_conference_integration_id_rejects_control_characters() -> None:
    with pytest.raises(Exception, match="invalid"):
        ucr_conference_integration_id(
            {"CLIENTPLATFORM_UCR_CONFERENCE_INTEGRATION_ID": "bad\nvalue"}
        )
