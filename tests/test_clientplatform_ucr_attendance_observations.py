from __future__ import annotations

import unittest
from datetime import datetime, timezone

from clientplatform.application.ucr_attendance_observations import (
    parse_ucr_participant_attendance,
    read_ucr_attendance_observation,
    ucr_attendance_to_observation,
)
from clientplatform.domain.external_products import (
    ExternalObservationQuality,
    ExternalObservationState,
    ExternalProductEventType,
    ExternalProductReceipt,
)
from clientplatform.runtime.ucr_gateway import (
    UcrGatewayUnavailable,
    UcrUniversalConferenceMethod,
)


_REQUEST = {
    "scope": {"tenantId": {"value": "tenant-a"}},
    "conferenceId": {"value": "conference-a"},
    "integrationId": {"value": "integration-a"},
    "externalUserId": "c2VyZ2V5QGV4YW1wbGUuaW52YWxpZA==",
}


def _head(*, observation_key: str, revision: int = 1) -> ExternalProductReceipt:
    return ExternalProductReceipt(
        id="11111111-1111-4111-8111-111111111111",
        business_id="22222222-2222-4222-8222-222222222222",
        connector_id="33333333-3333-4333-8333-333333333333",
        external_event_id=f"ucr-attendance-r{revision}",
        event_type=ExternalProductEventType.EVIDENCE,
        customer_id="44444444-4444-4444-8444-444444444444",
        customer_fingerprint=None,
        payload_fingerprint="9" * 64,
        outcome_event_id=None,
        occurred_at="2026-09-20T12:00:00+00:00",
        received_at="2026-09-20T12:00:01+00:00",
        observation_key=observation_key,
        observation_revision=revision,
        observation_state=ExternalObservationState.ACTIVE,
    )


class _Gateway:
    def __init__(self, result: dict[str, object] | None = None, error: Exception | None = None):
        self.result = result or {}
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def invoke_universal_conference(self, *, method, request):
        self.calls.append({"method": method, "request": request})
        if self.error is not None:
            raise self.error
        return {"ok": True, "result": self.result}


class UcrAttendanceObservationTests(unittest.TestCase):
    def test_parser_accepts_protobuf_json_integer_strings_and_preserves_source_times(self) -> None:
        attendance = parse_ucr_participant_attendance(
            {
                "attendance": {
                    "externalUserId": "raw-must-not-propagate",
                    "firstJoinAtUnixMs": "1789912800000",
                    "lastLeaveAtUnixMs": "1789915320000",
                    "firstMediaReadyAtUnixMs": "1789912815000",
                    "totalConnectedSeconds": "2520",
                    "currentConnectedSeconds": "0",
                    "joinCount": 1,
                    "reconnectCount": 0,
                    "mediaReadyCount": 1,
                    "connected": False,
                }
            }
        )
        self.assertEqual(attendance.total_connected_seconds, 2520)
        self.assertEqual(attendance.join_count, 1)
        self.assertEqual(
            attendance.first_join_at,
            datetime.fromtimestamp(1789912800, tz=timezone.utc),
        )
        self.assertEqual(
            attendance.last_leave_at,
            datetime.fromtimestamp(1789915320, tz=timezone.utc),
        )

    def test_omitted_connected_uses_protobuf_default_false(self) -> None:
        attendance = parse_ucr_participant_attendance(
            {
                "attendance": {
                    "firstJoinAtUnixMs": "1789912800000",
                    "lastLeaveAtUnixMs": "1789912860000",
                    "totalConnectedSeconds": "60",
                    "joinCount": 1,
                }
            }
        )
        self.assertFalse(attendance.connected)
        self.assertEqual(attendance.current_connected_seconds, 0)

    def test_verified_attendance_becomes_safe_provenance_observation_without_raw_identity(self) -> None:
        attendance = parse_ucr_participant_attendance(
            {
                "attendance": {
                    "externalUserId": "sergey@example.invalid",
                    "firstJoinAtUnixMs": "1789912800000",
                    "lastLeaveAtUnixMs": "1789915320000",
                    "firstMediaReadyAtUnixMs": "1789912815000",
                    "totalConnectedSeconds": "2520",
                    "currentConnectedSeconds": 0,
                    "joinCount": 1,
                    "reconnectCount": 0,
                    "mediaReadyCount": 1,
                    "connected": False,
                }
            }
        )
        observation = ucr_attendance_to_observation(
            attendance=attendance,
            observation_key="ucr:webinar-42:attendance",
            canonical_request=_REQUEST,
            current_head=None,
        )
        assert observation is not None
        self.assertEqual(observation.kind, "ucr.conference_attendance")
        self.assertEqual(observation.quality, ExternalObservationQuality.SOURCE_VERIFIED)
        self.assertEqual(observation.label, "Участие в онлайн-событии подтверждено: 42 мин")
        self.assertEqual(
            observation.observed_at,
            datetime.fromtimestamp(1789915320, tz=timezone.utc),
        )
        self.assertTrue(observation.provenance_ref.startswith("ucr-attendance:"))
        self.assertNotIn("sergey@example.invalid", repr(observation))
        self.assertNotIn(_REQUEST["externalUserId"], repr(observation))
        self.assertIsNone(observation.fresh_until)

    def test_existing_head_produces_exact_next_revision_and_supersession(self) -> None:
        attendance = parse_ucr_participant_attendance(
            {
                "attendance": {
                    "firstJoinAtUnixMs": "1789912800000",
                    "lastLeaveAtUnixMs": "1789912920000",
                    "totalConnectedSeconds": "120",
                    "joinCount": 2,
                }
            }
        )
        head = _head(observation_key="ucr:event-42:attendance", revision=3)
        observation = ucr_attendance_to_observation(
            attendance=attendance,
            observation_key="ucr:event-42:attendance",
            canonical_request=_REQUEST,
            current_head=head,
        )
        assert observation is not None
        self.assertEqual(observation.revision, 4)
        self.assertEqual(
            observation.supersedes_external_event_id,
            head.external_event_id,
        )

        with self.assertRaisesRegex(ValueError, "another key"):
            ucr_attendance_to_observation(
                attendance=attendance,
                observation_key="ucr:other:attendance",
                canonical_request=_REQUEST,
                current_head=head,
            )

    def test_connected_attendance_is_explicitly_incomplete(self) -> None:
        attendance = parse_ucr_participant_attendance(
            {
                "attendance": {
                    "firstJoinAtUnixMs": "1789912800000",
                    "firstMediaReadyAtUnixMs": "1789912815000",
                    "totalConnectedSeconds": "120",
                    "currentConnectedSeconds": "120",
                    "joinCount": 1,
                    "reconnectCount": 0,
                    "mediaReadyCount": 1,
                    "connected": True,
                }
            }
        )
        observation = ucr_attendance_to_observation(
            attendance=attendance,
            observation_key="ucr:live:attendance",
            canonical_request=_REQUEST,
        )
        assert observation is not None
        self.assertIn("итоговая длительность ещё может измениться", observation.limitations[0])

    def test_zero_attendance_remains_a_distinct_zero_without_invented_observation(self) -> None:
        attendance = parse_ucr_participant_attendance(
            {
                "attendance": {
                    "totalConnectedSeconds": "0",
                    "currentConnectedSeconds": 0,
                    "joinCount": 0,
                    "reconnectCount": 0,
                    "mediaReadyCount": 0,
                    "connected": False,
                }
            }
        )
        self.assertFalse(attendance.has_attendance)
        self.assertIsNone(
            ucr_attendance_to_observation(
                attendance=attendance,
                observation_key="ucr:zero:attendance",
                canonical_request=_REQUEST,
            )
        )

    def test_parser_fails_closed_on_conflicting_or_malformed_source_values(self) -> None:
        cases = (
            {"joinCount": True, "connected": False},
            {"joinCount": -1, "connected": False},
            {"joinCount": 0, "connected": True},
            {
                "firstJoinAtUnixMs": "1789912800000",
                "currentConnectedSeconds": 1,
                "joinCount": 1,
                "connected": False,
            },
            {
                "firstJoinAtUnixMs": "1789912800000",
                "lastLeaveAtUnixMs": "1789910000000",
                "joinCount": 1,
                "connected": False,
            },
            {
                "firstJoinAtUnixMs": "1789912800000",
                "joinCount": 1,
                "connected": False,
                "unexpectedField": "must-fail",
            },
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    parse_ucr_participant_attendance({"attendance": payload})

    def test_join_evidence_without_source_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires firstJoinAtUnixMs"):
            parse_ucr_participant_attendance(
                {
                    "attendance": {
                        "joinCount": 1,
                        "totalConnectedSeconds": "60",
                        "connected": False,
                    }
                }
            )


class UcrAttendanceObservationReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_calls_only_public_attendance_rpc_and_returns_observation(self) -> None:
        gateway = _Gateway(
            result={
                "attendance": {
                    "externalUserId": _REQUEST["externalUserId"],
                    "firstJoinAtUnixMs": "1789912800000",
                    "lastLeaveAtUnixMs": "1789912860000",
                    "totalConnectedSeconds": "60",
                    "currentConnectedSeconds": "0",
                    "joinCount": 1,
                    "reconnectCount": 0,
                    "mediaReadyCount": 0,
                    "connected": False,
                }
            }
        )
        read = await read_ucr_attendance_observation(
            gateway=gateway,
            canonical_request=_REQUEST,
            observation_key="ucr:event-1:attendance",
            current_head=None,
        )
        self.assertEqual(
            gateway.calls,
            [
                {
                    "method": UcrUniversalConferenceMethod.GET_PARTICIPANT_ATTENDANCE,
                    "request": _REQUEST,
                }
            ],
        )
        self.assertIsNotNone(read.observation)
        assert read.observation is not None
        self.assertNotIn(str(_REQUEST["externalUserId"]), repr(read.observation))
        self.assertIn("Источник не подтвердил готовность медиа.", read.observation.limitations)

    async def test_mismatched_echoed_participant_fails_closed(self) -> None:
        gateway = _Gateway(
            result={
                "attendance": {
                    "externalUserId": "b3RoZXItcGFydGljaXBhbnQ=",
                    "firstJoinAtUnixMs": "1789912800000",
                    "totalConnectedSeconds": "60",
                    "joinCount": 1,
                }
            }
        )
        with self.assertRaisesRegex(ValueError, "does not match request"):
            await read_ucr_attendance_observation(
                gateway=gateway,
                canonical_request=_REQUEST,
                observation_key="ucr:event-mismatch:attendance",
                current_head=None,
            )

    async def test_gateway_failure_propagates_without_fake_observation(self) -> None:
        gateway = _Gateway(error=UcrGatewayUnavailable("ucr unavailable"))
        with self.assertRaises(UcrGatewayUnavailable):
            await read_ucr_attendance_observation(
                gateway=gateway,
                canonical_request=_REQUEST,
                observation_key="ucr:event-2:attendance",
                current_head=None,
            )


if __name__ == "__main__":
    unittest.main()
