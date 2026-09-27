from __future__ import annotations

import unittest
from typing import Any

from clientplatform.runtime.conference_provider import (
    ConferenceCreateSpec,
    ConferenceLifecycle,
    ConferenceMode,
    ConferenceProviderRegistry,
    ConferenceProviderUnavailable,
    ConferenceRef,
    ConferenceRole,
    ExternalHttpsRoomProvider,
)
from clientplatform.runtime.ucr_conference_provider import UcrConferenceProvider
from clientplatform.runtime.ucr_gateway import UcrUniversalConferenceMethod


class _Gateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.capabilities = {
            "recording": False,
            "maxParticipants": 1024,
            "browserRealtimeGateway": True,
            "productionWebrtc": True,
            "turn": True,
        }

    async def invoke_universal_conference(
        self, *, method, request, idempotency_key=None
    ):
        self.calls.append(
            {
                "method": method,
                "request": request,
                "idempotency_key": idempotency_key,
            }
        )
        if method is UcrUniversalConferenceMethod.GET_CAPABILITIES:
            return {"result": {"capabilities": dict(self.capabilities)}}
        if method in {
            UcrUniversalConferenceMethod.CREATE_CONFERENCE,
            UcrUniversalConferenceMethod.RESOLVE_CONFERENCE,
            UcrUniversalConferenceMethod.TRANSITION_CONFERENCE,
            UcrUniversalConferenceMethod.SET_ENTRY_OPEN,
        }:
            lifecycle = "UNIVERSAL_CONFERENCE_LIFECYCLE_SCHEDULED"
            if method is UcrUniversalConferenceMethod.TRANSITION_CONFERENCE:
                lifecycle = str(request.get("target") or lifecycle)
            return {
                "result": {
                    "conference": {
                        "conferenceId": {"value": "ucr-conf-1"},
                        "lifecycle": lifecycle,
                    }
                }
            }
        if method is UcrUniversalConferenceMethod.ISSUE_JOIN_GRANT:
            return {
                "result": {
                    "grant": {
                        "joinUrl": "https://conference.example.test/join/token",
                        "sessionId": {"value": "session-1"},
                    }
                }
            }
        if method is UcrUniversalConferenceMethod.GET_PARTICIPANT_ATTENDANCE:
            return {
                "result": {
                    "attendance": {
                        "externalUserId": "dXNlci1h",
                        "connected": False,
                        "totalConnectedSeconds": "2520",
                        "joinCount": 2,
                        "reconnectCount": 1,
                    }
                }
            }
        return {"result": {"accepted": True}}


class ConferenceProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_only_provider_exposes_join_but_not_fake_management(self):
        provider = ExternalHttpsRoomProvider(
            key="external",
            room_url="https://meet.example.test/room",
        )
        ref = await provider.create(
            ConferenceCreateSpec(
                tenant_id="tenant-a",
                external_conference_id="event-a",
                mode=ConferenceMode.WEBINAR,
                starts_at_unix_ms=1_800_000_000_000,
            ),
            idempotency_key="event:event-a",
        )
        join = await provider.issue_join(
            ref,
            tenant_id="tenant-a",
            external_user_id="user-a",
            idempotency_key="join:user-a",
        )
        self.assertEqual(join.url, "https://meet.example.test/room")
        caps = await provider.capabilities(tenant_id="tenant-a")
        self.assertTrue(caps.join_link)
        self.assertFalse(caps.managed_rooms)
        self.assertFalse(caps.attendance)

        with self.assertRaises(ConferenceProviderUnavailable):
            await provider.attendance(
                ref,
                tenant_id="tenant-a",
                external_user_id="user-a",
            )
        with self.assertRaises(ConferenceProviderUnavailable):
            await provider.set_lifecycle(
                ref,
                tenant_id="tenant-a",
                lifecycle=ConferenceLifecycle.LIVE,
                idempotency_key="lifecycle:event-a",
            )
        with self.assertRaises(ConferenceProviderUnavailable):
            await provider.set_entry_open(
                ref,
                tenant_id="tenant-a",
                open=True,
                idempotency_key="entry:event-a",
            )
        with self.assertRaises(ConferenceProviderUnavailable):
            await provider.ensure_participant(
                ref,
                tenant_id="tenant-a",
                external_user_id="user-a",
                role=ConferenceRole.ATTENDEE,
                idempotency_key="participant:user-a",
            )
        with self.assertRaises(ConferenceProviderUnavailable):
            await provider.prepare(
                ref,
                tenant_id="tenant-a",
                idempotency_key="runtime:event-a",
            )

    async def test_link_only_join_rejects_foreign_provider_reference(self):
        provider = ExternalHttpsRoomProvider(
            key="external",
            room_url="https://meet.example.test/room",
        )
        foreign = ConferenceRef(
            provider_key="ucr",
            external_conference_id="event-a",
            provider_conference_id="ucr-conf-1",
        )
        with self.assertRaisesRegex(ValueError, "different provider"):
            await provider.issue_join(
                foreign,
                tenant_id="tenant-a",
                external_user_id="user-a",
                idempotency_key="join:user-a",
            )

    async def test_registry_routes_without_owning_business_state(self):
        provider = ExternalHttpsRoomProvider(
            key="external",
            room_url="https://meet.example.test/room",
        )
        registry = ConferenceProviderRegistry((provider,))
        self.assertIs(registry.get("EXTERNAL"), provider)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            registry.get("missing")

    async def test_ucr_webinar_flow_uses_only_universal_facade(self):
        gateway = _Gateway()
        provider = UcrConferenceProvider(
            gateway=gateway,  # type: ignore[arg-type]
            integration_id="clientplatform-prod",
        )
        self.assertTrue(
            (await provider.capabilities(tenant_id="tenant-a")).production_realtime
        )
        ref = await provider.create(
            ConferenceCreateSpec(
                tenant_id="tenant-a",
                external_conference_id="event-a",
                mode=ConferenceMode.WEBINAR,
                starts_at_unix_ms=1_800_000_000_000,
            ),
            idempotency_key="conference:event-a",
        )
        resolved = await provider.resolve(
            tenant_id="tenant-a",
            external_conference_id="event-a",
        )
        self.assertEqual(resolved.provider_conference_id, "ucr-conf-1")
        self.assertEqual(resolved.lifecycle, ConferenceLifecycle.SCHEDULED)
        await provider.set_lifecycle(
            ref,
            tenant_id="tenant-a",
            lifecycle=ConferenceLifecycle.WAITING,
            idempotency_key="lifecycle:waiting:event-a",
        )
        await provider.set_entry_open(
            ref,
            tenant_id="tenant-a",
            open=True,
            idempotency_key="entry:open:event-a",
        )
        await provider.ensure_participant(
            ref,
            tenant_id="tenant-a",
            external_user_id="user-a",
            role=ConferenceRole.ATTENDEE,
            idempotency_key="participant:user-a",
        )
        await provider.prepare(
            ref,
            tenant_id="tenant-a",
            idempotency_key="runtime:event-a",
        )
        join = await provider.issue_join(
            ref,
            tenant_id="tenant-a",
            external_user_id="user-a",
            idempotency_key="join:user-a",
        )
        self.assertEqual(join.url, "https://conference.example.test/join/token")
        attendance = await provider.attendance(
            ref,
            tenant_id="tenant-a",
            external_user_id="user-a",
        )
        self.assertEqual(attendance.total_connected_seconds, 2520)

        methods = [call["method"] for call in gateway.calls]
        for expected in (
            UcrUniversalConferenceMethod.CREATE_CONFERENCE,
            UcrUniversalConferenceMethod.RESOLVE_CONFERENCE,
            UcrUniversalConferenceMethod.TRANSITION_CONFERENCE,
            UcrUniversalConferenceMethod.SET_ENTRY_OPEN,
            UcrUniversalConferenceMethod.ENSURE_PARTICIPANT,
            UcrUniversalConferenceMethod.ENSURE_PARTICIPANT_DEVICE,
            UcrUniversalConferenceMethod.PREPARE_CONFERENCE_RUNTIME,
            UcrUniversalConferenceMethod.ISSUE_JOIN_GRANT,
            UcrUniversalConferenceMethod.GET_PARTICIPANT_ATTENDANCE,
        ):
            self.assertIn(expected, methods)

    async def test_ucr_join_fails_closed_without_production_realtime(self):
        for missing in ("browserRealtimeGateway", "productionWebrtc", "turn"):
            with self.subTest(missing=missing):
                gateway = _Gateway()
                gateway.capabilities[missing] = False
                provider = UcrConferenceProvider(
                    gateway=gateway,  # type: ignore[arg-type]
                    integration_id="clientplatform-prod",
                )
                ref = await provider.create(
                    ConferenceCreateSpec(
                        tenant_id="tenant-a",
                        external_conference_id="event-a",
                        mode=ConferenceMode.WEBINAR,
                        starts_at_unix_ms=1_800_000_000_000,
                    ),
                    idempotency_key="conference:event-a",
                )
                with self.assertRaisesRegex(
                    ConferenceProviderUnavailable,
                    "production browser realtime",
                ):
                    await provider.issue_join(
                        ref,
                        tenant_id="tenant-a",
                        external_user_id="user-a",
                        idempotency_key="join:user-a",
                    )

    async def test_ucr_capabilities_reject_string_booleans(self):
        gateway = _Gateway()
        gateway.capabilities["productionWebrtc"] = "false"
        provider = UcrConferenceProvider(
            gateway=gateway,  # type: ignore[arg-type]
            integration_id="clientplatform-prod",
        )
        with self.assertRaisesRegex(
            ConferenceProviderUnavailable,
            "must be boolean",
        ):
            await provider.capabilities(tenant_id="tenant-a")

    async def test_ucr_attendance_rejects_nonfinite_semantic_types(self):
        gateway = _Gateway()

        async def malformed(**kwargs: Any):
            if kwargs["method"] is UcrUniversalConferenceMethod.GET_PARTICIPANT_ATTENDANCE:
                return {
                    "result": {
                        "attendance": {
                            "externalUserId": "dXNlci1h",
                            "connected": "false",
                            "totalConnectedSeconds": "-1",
                            "joinCount": 1,
                            "reconnectCount": 0,
                        }
                    }
                }
            return await _Gateway().invoke_universal_conference(**kwargs)

        gateway.invoke_universal_conference = malformed  # type: ignore[method-assign]
        provider = UcrConferenceProvider(
            gateway=gateway,  # type: ignore[arg-type]
            integration_id="clientplatform-prod",
        )
        ref = ConferenceRef(
            provider_key="ucr",
            external_conference_id="event-a",
            provider_conference_id="ucr-conf-1",
        )
        with self.assertRaisesRegex(
            ConferenceProviderUnavailable,
            "must be boolean",
        ):
            await provider.attendance(
                ref,
                tenant_id="tenant-a",
                external_user_id="user-a",
            )

    async def test_ucr_attendance_rejects_wrong_participant_echo(self):
        gateway = _Gateway()

        async def wrong_participant(**kwargs: Any):
            if kwargs["method"] is UcrUniversalConferenceMethod.GET_PARTICIPANT_ATTENDANCE:
                return {
                    "result": {
                        "attendance": {
                            "externalUserId": "dXNlci1i",
                            "connected": False,
                            "totalConnectedSeconds": "10",
                            "joinCount": 1,
                            "reconnectCount": 0,
                        }
                    }
                }
            return await _Gateway().invoke_universal_conference(**kwargs)

        gateway.invoke_universal_conference = wrong_participant  # type: ignore[method-assign]
        provider = UcrConferenceProvider(
            gateway=gateway,  # type: ignore[arg-type]
            integration_id="clientplatform-prod",
        )
        ref = ConferenceRef(
            provider_key="ucr",
            external_conference_id="event-a",
            provider_conference_id="ucr-conf-1",
        )
        with self.assertRaisesRegex(
            ConferenceProviderUnavailable,
            "participant does not match",
        ):
            await provider.attendance(
                ref,
                tenant_id="tenant-a",
                external_user_id="user-a",
            )

    async def test_ucr_rejects_foreign_provider_reference_before_network(self):
        gateway = _Gateway()
        provider = UcrConferenceProvider(
            gateway=gateway,  # type: ignore[arg-type]
            integration_id="clientplatform-prod",
        )
        foreign = ConferenceRef(
            provider_key="external",
            external_conference_id="event-a",
            provider_conference_id="event-a",
        )
        with self.assertRaisesRegex(ValueError, "different provider"):
            await provider.prepare(
                foreign,
                tenant_id="tenant-a",
                idempotency_key="runtime:event-a",
            )
        self.assertEqual(gateway.calls, [])

    async def test_ucr_join_rejects_invalid_ttl_before_network(self):
        gateway = _Gateway()
        provider = UcrConferenceProvider(
            gateway=gateway,  # type: ignore[arg-type]
            integration_id="clientplatform-prod",
        )
        ref = ConferenceRef(
            provider_key="ucr",
            external_conference_id="event-a",
            provider_conference_id="ucr-conf-1",
        )
        with self.assertRaisesRegex(ValueError, "TTL"):
            await provider.issue_join(
                ref,
                tenant_id="tenant-a",
                external_user_id="user-a",
                idempotency_key="join:user-a",
                ttl_seconds=30,
            )
        self.assertEqual(gateway.calls, [])

    def test_create_spec_rejects_invalid_schedule(self):
        with self.assertRaisesRegex(ValueError, "after start"):
            ConferenceCreateSpec(
                tenant_id="tenant-a",
                external_conference_id="event-a",
                mode=ConferenceMode.WEBINAR,
                starts_at_unix_ms=1000,
                planned_end_unix_ms=1000,
            )


if __name__ == "__main__":
    unittest.main()
