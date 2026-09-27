from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from clientplatform.application.event_conference_join import (
    issue_managed_event_owner_join,
    issue_managed_event_session_join,
    ucr_conference_integration_id,
    ucr_managed_event_provider_available,
)
from clientplatform.domain.event_sessions import EventSession
from clientplatform.domain.events import EventRegistration
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.runtime.conference_provider import (
    ConferenceCapabilities,
    ConferenceJoin,
    ConferenceLifecycle,
    ConferenceRef,
)
from clientplatform.runtime.ucr_gateway import UcrGatewayConfigurationError


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
EVENT_ID = "22222222-2222-4222-8222-222222222222"
OWNER_MEMBERSHIP_ID = "33333333-3333-4333-8333-333333333333"


def _event(*, business_id: str = BUSINESS_ID):
    return SimpleNamespace(
        id=EVENT_ID,
        business_id=business_id,
        created_by_member_id=OWNER_MEMBERSHIP_ID,
        status="published",
        created_at=NOW - timedelta(days=10),
    )


def _session(
    *,
    business_id: str = BUSINESS_ID,
    session_id: str | None = None,
    starts_at: datetime | None = None,
) -> EventSession:
    return EventSession(
        id=session_id or str(uuid4()),
        business_id=business_id,
        event_id=EVENT_ID,
        position=1,
        starts_at=starts_at or (NOW + timedelta(hours=2)),
        ends_at=NOW + timedelta(hours=4),
        provider_key="ucr",
        provider_label="ClientPlatform эфир",
        join_url=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _registration(
    session: EventSession,
    *,
    business_id: str | None = None,
) -> EventRegistration:
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


def _actor(
    *,
    membership_id: str = OWNER_MEMBERSHIP_ID,
    role: PlatformRole = PlatformRole.OWNER,
) -> TenantContext:
    return TenantContext(
        business_id=BUSINESS_ID,
        user_id=101,
        membership_id=membership_id,
        role=role,
    )


class FakeManagedProvider:
    key = "ucr"

    def __init__(
        self,
        *,
        lifecycle: ConferenceLifecycle = ConferenceLifecycle.SCHEDULED,
    ) -> None:
        self.calls: list[tuple[str, object]] = []
        self.lifecycle = lifecycle

    async def capabilities(self, *, tenant_id: str) -> ConferenceCapabilities:
        raise AssertionError("not used directly")

    async def create(self, spec, *, idempotency_key: str) -> ConferenceRef:
        self.calls.append(("create", (spec, idempotency_key)))
        return ConferenceRef(
            provider_key="ucr",
            external_conference_id=spec.external_conference_id,
            provider_conference_id="ucr-conference-1",
            lifecycle=self.lifecycle,
        )

    async def resolve(self, **kwargs):
        raise AssertionError("not used")

    async def set_lifecycle(self, conference, **kwargs):
        self.calls.append(("lifecycle", (conference, kwargs)))
        self.lifecycle = kwargs["lifecycle"]
        return ConferenceRef(
            provider_key=conference.provider_key,
            external_conference_id=conference.external_conference_id,
            provider_conference_id=conference.provider_conference_id,
            lifecycle=self.lifecycle,
        )

    async def set_entry_open(self, conference, **kwargs):
        self.calls.append(("entry", (conference, kwargs)))
        return conference

    async def ensure_participant(self, conference, **kwargs) -> None:
        self.calls.append(("participant", (conference, kwargs)))

    async def prepare(self, conference, **kwargs) -> None:
        self.calls.append(("prepare", (conference, kwargs)))

    async def issue_join(self, conference, **kwargs) -> ConferenceJoin:
        self.calls.append(("join", (conference, kwargs)))
        return ConferenceJoin(url="https://join.example.test/grant", session_id="session-1")

    async def attendance(self, *args, **kwargs):
        raise AssertionError("not used")


class ManagedEventConferenceJoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_attendee_join_ensures_owner_and_uses_opaque_identity(self) -> None:
        event = _event()
        session = _session()
        registration = _registration(session)
        provider = FakeManagedProvider()
        attempt = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

        join = await issue_managed_event_session_join(
            registration=registration,
            event=event,
            session=session,
            provider=provider,
            join_attempt_id=attempt,
        )

        self.assertEqual(join.url, "https://join.example.test/grant")
        self.assertEqual(
            [name for name, _ in provider.calls],
            ["create", "participant", "participant", "prepare", "lifecycle", "entry", "join"],
        )
        create_spec, create_key = provider.calls[0][1]
        self.assertEqual(create_spec.tenant_id, session.business_id)
        self.assertEqual(
            create_spec.external_conference_id,
            f"clientplatform:event:{event.id}:session:1",
        )
        self.assertEqual(create_spec.starts_at_unix_ms, int(event.created_at.timestamp() * 1000))
        self.assertIsNone(create_spec.planned_end_unix_ms)
        self.assertEqual(create_key, f"conference:{event.id}:1")

        _, owner_kwargs = provider.calls[1][1]
        self.assertEqual(
            owner_kwargs["external_user_id"],
            f"clientplatform:event-owner:{event.created_by_member_id}",
        )
        _, participant_kwargs = provider.calls[2][1]
        self.assertEqual(
            participant_kwargs["external_user_id"],
            f"clientplatform:event-registration:{registration.id}",
        )
        _, join_kwargs = provider.calls[-1][1]
        self.assertTrue(join_kwargs["idempotency_key"].endswith(attempt))
        self.assertEqual(join_kwargs["ttl_seconds"], 15 * 60)

        serialized = repr(provider.calls)
        self.assertNotIn(registration.email, serialized)
        self.assertNotIn(registration.name, serialized)
        self.assertNotIn(str(registration.phone), serialized)

    async def test_provider_room_identity_is_stable_across_session_row_replacement(self) -> None:
        event = _event()
        first = _session(
            session_id="44444444-4444-4444-8444-444444444444",
            starts_at=NOW + timedelta(hours=2),
        )
        second = _session(
            session_id="55555555-5555-4555-8555-555555555555",
            starts_at=NOW + timedelta(days=2),
        )
        first_provider = FakeManagedProvider()
        second_provider = FakeManagedProvider()

        await issue_managed_event_owner_join(
            actor=_actor(),
            event=event,
            session=first,
            provider=first_provider,
            join_attempt_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )
        await issue_managed_event_owner_join(
            actor=_actor(),
            event=event,
            session=second,
            provider=second_provider,
            join_attempt_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )

        first_spec, first_key = first_provider.calls[0][1]
        second_spec, second_key = second_provider.calls[0][1]
        self.assertEqual(first_spec.external_conference_id, second_spec.external_conference_id)
        self.assertEqual(first_spec.starts_at_unix_ms, second_spec.starts_at_unix_ms)
        self.assertEqual(first_key, second_key)

    async def test_repeat_join_does_not_regress_active_conference_to_waiting(self) -> None:
        event = _event()
        session = _session()
        registration = _registration(session)
        provider = FakeManagedProvider(lifecycle=ConferenceLifecycle.LIVE)

        await issue_managed_event_session_join(
            registration=registration,
            event=event,
            session=session,
            provider=provider,
            join_attempt_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

        self.assertNotIn(
            "lifecycle",
            [name for name, _payload in provider.calls],
        )
        self.assertIn("entry", [name for name, _payload in provider.calls])
        self.assertIn("join", [name for name, _payload in provider.calls])

    async def test_owner_join_uses_canonical_owner_identity(self) -> None:
        event = _event()
        session = _session()
        provider = FakeManagedProvider()

        join = await issue_managed_event_owner_join(
            actor=_actor(),
            event=event,
            session=session,
            provider=provider,
            join_attempt_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

        self.assertEqual(join.url, "https://join.example.test/grant")
        participant_calls = [payload for name, payload in provider.calls if name == "participant"]
        self.assertEqual(len(participant_calls), 1)
        _, kwargs = participant_calls[0]
        self.assertEqual(
            kwargs["external_user_id"],
            f"clientplatform:event-owner:{event.created_by_member_id}",
        )

    async def test_administrator_join_is_host_without_second_owner(self) -> None:
        event = _event()
        session = _session()
        provider = FakeManagedProvider()
        admin_membership = "66666666-6666-4666-8666-666666666666"

        await issue_managed_event_owner_join(
            actor=_actor(
                membership_id=admin_membership,
                role=PlatformRole.ADMINISTRATOR,
            ),
            event=event,
            session=session,
            provider=provider,
            join_attempt_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

        participant_calls = [payload for name, payload in provider.calls if name == "participant"]
        self.assertEqual(len(participant_calls), 2)
        _, owner_kwargs = participant_calls[0]
        _, host_kwargs = participant_calls[1]
        self.assertEqual(
            owner_kwargs["external_user_id"],
            f"clientplatform:event-owner:{event.created_by_member_id}",
        )
        self.assertEqual(
            host_kwargs["external_user_id"],
            f"clientplatform:event-host:{admin_membership}",
        )

    async def test_managed_join_rejects_cross_tenant_before_provider_io(self) -> None:
        event = _event()
        session = _session()
        registration = _registration(session, business_id=str(uuid4()))
        provider = FakeManagedProvider()

        with self.assertRaisesRegex(ValueError, "does not belong"):
            await issue_managed_event_session_join(
                registration=registration,
                event=event,
                session=session,
                provider=provider,
            )

        self.assertEqual(provider.calls, [])

    async def test_managed_join_rejects_non_managed_session_before_provider_io(self) -> None:
        event = _event()
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

        with self.assertRaisesRegex(ValueError, "not managed"):
            await issue_managed_event_session_join(
                registration=registration,
                event=event,
                session=external,
                provider=provider,
            )

        self.assertEqual(provider.calls, [])


class ManagedEventConferenceConfigTests(unittest.TestCase):
    def test_ucr_managed_venue_requires_gateway_and_integration_id(self) -> None:
        base = {
            "CLIENTPLATFORM_UCR_GATEWAY_URL": "https://gateway.example.test",
        }
        self.assertFalse(ucr_managed_event_provider_available(base))
        self.assertFalse(
            ucr_managed_event_provider_available(
                {**base, "CLIENTPLATFORM_UCR_GATEWAY_ENABLED": "true"}
            )
        )
        enabled = {
            **base,
            "CLIENTPLATFORM_UCR_GATEWAY_ENABLED": "true",
            "CLIENTPLATFORM_UCR_CONFERENCE_INTEGRATION_ID": "clientplatform-prod",
        }
        self.assertTrue(ucr_managed_event_provider_available(enabled))
        self.assertEqual(
            ucr_conference_integration_id(enabled),
            "clientplatform-prod",
        )

    def test_ucr_conference_integration_id_rejects_control_characters(self) -> None:
        with self.assertRaisesRegex(UcrGatewayConfigurationError, "invalid"):
            ucr_conference_integration_id(
                {"CLIENTPLATFORM_UCR_CONFERENCE_INTEGRATION_ID": "bad\nvalue"}
            )


if __name__ == "__main__":
    unittest.main()
