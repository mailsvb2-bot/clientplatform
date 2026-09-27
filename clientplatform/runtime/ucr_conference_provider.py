from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any, Mapping

from clientplatform.runtime.conference_provider import (
    ConferenceAttendance,
    ConferenceCapabilities,
    ConferenceCreateSpec,
    ConferenceJoin,
    ConferenceLifecycle,
    ConferenceMode,
    ConferenceProviderUnavailable,
    ConferenceRef,
    ConferenceRole,
)
from clientplatform.runtime.ucr_gateway import (
    UcrGatewayClient,
    UcrUniversalConferenceMethod,
)


_MODE = {
    ConferenceMode.MEETING: "UNIVERSAL_CONFERENCE_MODE_MEETING",
    ConferenceMode.WEBINAR: "UNIVERSAL_CONFERENCE_MODE_WEBINAR",
    ConferenceMode.BROADCAST: "UNIVERSAL_CONFERENCE_MODE_BROADCAST",
    ConferenceMode.AUDIO_ROOM: "UNIVERSAL_CONFERENCE_MODE_AUDIO_ROOM",
}
_LIFECYCLE = {
    ConferenceLifecycle.SCHEDULED: "UNIVERSAL_CONFERENCE_LIFECYCLE_SCHEDULED",
    ConferenceLifecycle.WAITING: "UNIVERSAL_CONFERENCE_LIFECYCLE_WAITING",
    ConferenceLifecycle.LIVE: "UNIVERSAL_CONFERENCE_LIFECYCLE_LIVE",
    ConferenceLifecycle.ENDING: "UNIVERSAL_CONFERENCE_LIFECYCLE_ENDING",
    ConferenceLifecycle.ENDED: "UNIVERSAL_CONFERENCE_LIFECYCLE_ENDED",
}
_ROLE = {
    ConferenceRole.OWNER: "CONFERENCE_PARTICIPANT_ROLE_OWNER",
    ConferenceRole.HOST: "CONFERENCE_PARTICIPANT_ROLE_HOST",
    ConferenceRole.MODERATOR: "CONFERENCE_PARTICIPANT_ROLE_MODERATOR",
    ConferenceRole.SPEAKER: "CONFERENCE_PARTICIPANT_ROLE_SPEAKER",
    ConferenceRole.ATTENDEE: "CONFERENCE_PARTICIPANT_ROLE_ATTENDEE",
}


_LIFECYCLE_FROM_UCR = {value: key for key, value in _LIFECYCLE.items()}


def _conference_lifecycle(raw: object) -> ConferenceLifecycle | None:
    if raw is None:
        return None
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return _LIFECYCLE_FROM_UCR[value]
    except KeyError as exc:
        raise ConferenceProviderUnavailable("UCR conference lifecycle is invalid") from exc


def _conference_ref(
    *,
    raw: Mapping[str, Any],
    external_conference_id: str,
    provider_key: str,
) -> ConferenceRef:
    conference_id = raw.get("conferenceId")
    if not isinstance(conference_id, Mapping) or not conference_id.get("value"):
        raise ConferenceProviderUnavailable("UCR conference id is unavailable")
    return ConferenceRef(
        provider_key=provider_key,
        external_conference_id=external_conference_id,
        provider_conference_id=str(conference_id["value"]),
        lifecycle=_conference_lifecycle(raw.get("lifecycle")),
    )


def _opaque(value: str) -> dict[str, str]:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("opaque conference identifier is required")
    return {"value": normalized}


def _bytes(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("external identifier is required")
    return base64.b64encode(normalized.encode("utf-8")).decode("ascii")


def _result(response: Mapping[str, Any]) -> Mapping[str, Any]:
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise ConferenceProviderUnavailable("UCR conference response is missing result")
    return result


def _protobuf_bool(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConferenceProviderUnavailable(
            f"UCR conference {field_name} must be boolean"
        )
    return value


def _protobuf_uint(
    value: object,
    *,
    field_name: str,
    maximum: int = 2**32 - 1,
) -> int:
    if isinstance(value, bool):
        raise ConferenceProviderUnavailable(
            f"UCR conference {field_name} must be an unsigned integer"
        )
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw or not raw.isascii() or not raw.isdecimal():
            raise ConferenceProviderUnavailable(
                f"UCR conference {field_name} must be an unsigned integer"
            )
        parsed = int(raw, 10)
    else:
        raise ConferenceProviderUnavailable(
            f"UCR conference {field_name} must be an unsigned integer"
        )
    if parsed < 0 or parsed > maximum:
        raise ConferenceProviderUnavailable(
            f"UCR conference {field_name} is out of range"
        )
    return parsed


def _protobuf_bytes(value: object, *, field_name: str) -> bytes:
    if not isinstance(value, str):
        raise ConferenceProviderUnavailable(
            f"UCR conference {field_name} must be protobuf JSON bytes"
        )
    raw = value.strip()
    if not raw or len(raw) > 8192:
        raise ConferenceProviderUnavailable(
            f"UCR conference {field_name} must be protobuf JSON bytes"
        )
    try:
        decoded = base64.b64decode(raw.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ConferenceProviderUnavailable(
            f"UCR conference {field_name} must be protobuf JSON bytes"
        ) from exc
    if not decoded or len(decoded) > 4096:
        raise ConferenceProviderUnavailable(
            f"UCR conference {field_name} is out of range"
        )
    return decoded


class UcrConferenceProvider:
    key = "ucr"

    def __init__(self, *, gateway: UcrGatewayClient, integration_id: str) -> None:
        self.gateway = gateway
        self.integration_id = str(integration_id or "").strip()
        if not self.integration_id:
            raise ValueError("UCR integration_id is required")

    def _base(self, tenant_id: str) -> dict[str, Any]:
        return {
            "scope": {"tenantId": _opaque(tenant_id)},
            "integrationId": _opaque(self.integration_id),
        }

    def _conference_id(self, conference: ConferenceRef) -> dict[str, str]:
        if conference.provider_key.casefold() != self.key:
            raise ValueError("conference reference belongs to a different provider")
        return _opaque(conference.provider_conference_id)

    async def capabilities(self, *, tenant_id: str) -> ConferenceCapabilities:
        response = await self.gateway.invoke_universal_conference(
            method=UcrUniversalConferenceMethod.GET_CAPABILITIES,
            request=self._base(tenant_id),
        )
        raw = _result(response).get("capabilities")
        if not isinstance(raw, Mapping):
            raise ConferenceProviderUnavailable("UCR capabilities are unavailable")
        browser_gateway = _protobuf_bool(
            raw.get("browserRealtimeGateway"),
            field_name="capabilities.browserRealtimeGateway",
        )
        production_webrtc = _protobuf_bool(
            raw.get("productionWebrtc"),
            field_name="capabilities.productionWebrtc",
        )
        turn = _protobuf_bool(
            raw.get("turn"),
            field_name="capabilities.turn",
        )
        recording = _protobuf_bool(
            raw.get("recording", False),
            field_name="capabilities.recording",
        )
        max_participants = _protobuf_uint(
            raw.get("maxParticipants", 0),
            field_name="capabilities.maxParticipants",
        )
        production_realtime = browser_gateway and production_webrtc and turn
        return ConferenceCapabilities(
            provider_key=self.key,
            managed_rooms=True,
            join_link=production_realtime,
            attendance=True,
            recording=recording,
            max_participants=max_participants or None,
            production_realtime=production_realtime,
        )

    async def create(
        self,
        spec: ConferenceCreateSpec,
        *,
        idempotency_key: str,
    ) -> ConferenceRef:
        schedule: dict[str, Any] = {
            "startsAtUnixMs": str(int(spec.starts_at_unix_ms)),
        }
        if spec.planned_end_unix_ms is not None:
            schedule["plannedEndUnixMs"] = str(int(spec.planned_end_unix_ms))
        if spec.timezone:
            schedule["timezone"] = str(spec.timezone)

        request = self._base(spec.tenant_id)
        request.update(
            {
                "externalConferenceId": _bytes(spec.external_conference_id),
                "idempotencyKey": idempotency_key,
                "mode": _MODE[spec.mode],
                "schedule": schedule,
            }
        )
        response = await self.gateway.invoke_universal_conference(
            method=UcrUniversalConferenceMethod.CREATE_CONFERENCE,
            request=request,
            idempotency_key=idempotency_key,
        )
        conference = _result(response).get("conference")
        if not isinstance(conference, Mapping):
            raise ConferenceProviderUnavailable("UCR did not return a conference")
        return _conference_ref(
            raw=conference,
            external_conference_id=spec.external_conference_id,
            provider_key=self.key,
        )

    async def resolve(
        self,
        *,
        tenant_id: str,
        external_conference_id: str,
    ) -> ConferenceRef:
        request = self._base(tenant_id)
        request["externalConferenceId"] = _bytes(external_conference_id)
        response = await self.gateway.invoke_universal_conference(
            method=UcrUniversalConferenceMethod.RESOLVE_CONFERENCE,
            request=request,
        )
        conference = _result(response).get("conference")
        if not isinstance(conference, Mapping):
            raise ConferenceProviderUnavailable("UCR conference could not be resolved")
        return _conference_ref(
            raw=conference,
            external_conference_id=external_conference_id,
            provider_key=self.key,
        )

    async def set_lifecycle(
        self,
        conference: ConferenceRef,
        *,
        tenant_id: str,
        lifecycle: ConferenceLifecycle,
        idempotency_key: str,
    ) -> ConferenceRef:
        request = self._base(tenant_id)
        request.update(
            {
                "conferenceId": self._conference_id(conference),
                "target": _LIFECYCLE[lifecycle],
                "idempotencyKey": idempotency_key,
            }
        )
        response = await self.gateway.invoke_universal_conference(
            method=UcrUniversalConferenceMethod.TRANSITION_CONFERENCE,
            request=request,
            idempotency_key=idempotency_key,
        )
        raw = _result(response).get("conference")
        if not isinstance(raw, Mapping):
            raise ConferenceProviderUnavailable("UCR lifecycle transition failed")
        return _conference_ref(
            raw=raw,
            external_conference_id=conference.external_conference_id,
            provider_key=self.key,
        )

    async def set_entry_open(
        self,
        conference: ConferenceRef,
        *,
        tenant_id: str,
        open: bool,
        idempotency_key: str,
    ) -> ConferenceRef:
        request = self._base(tenant_id)
        request.update(
            {
                "conferenceId": self._conference_id(conference),
                "entryOpen": bool(open),
                "idempotencyKey": idempotency_key,
            }
        )
        response = await self.gateway.invoke_universal_conference(
            method=UcrUniversalConferenceMethod.SET_ENTRY_OPEN,
            request=request,
            idempotency_key=idempotency_key,
        )
        raw = _result(response).get("conference")
        if not isinstance(raw, Mapping):
            raise ConferenceProviderUnavailable("UCR entry gate update failed")
        return _conference_ref(
            raw=raw,
            external_conference_id=conference.external_conference_id,
            provider_key=self.key,
        )

    async def ensure_participant(
        self,
        conference: ConferenceRef,
        *,
        tenant_id: str,
        external_user_id: str,
        role: ConferenceRole,
        idempotency_key: str,
    ) -> None:
        request = self._base(tenant_id)
        request.update(
            {
                "conferenceId": self._conference_id(conference),
                "externalUserId": _bytes(external_user_id),
                "role": _ROLE[role],
                "idempotencyKey": idempotency_key,
            }
        )
        await self.gateway.invoke_universal_conference(
            method=UcrUniversalConferenceMethod.ENSURE_PARTICIPANT,
            request=request,
            idempotency_key=idempotency_key,
        )

        device_key = "device:" + hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()
        device_request = self._base(tenant_id)
        device_request.update(
            {
                "conferenceId": self._conference_id(conference),
                "externalUserId": _bytes(external_user_id),
                "idempotencyKey": device_key,
            }
        )
        await self.gateway.invoke_universal_conference(
            method=UcrUniversalConferenceMethod.ENSURE_PARTICIPANT_DEVICE,
            request=device_request,
            idempotency_key=device_key,
        )

    async def prepare(
        self,
        conference: ConferenceRef,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> None:
        request = self._base(tenant_id)
        request.update(
            {
                "conferenceId": self._conference_id(conference),
                "idempotencyKey": idempotency_key,
            }
        )
        await self.gateway.invoke_universal_conference(
            method=UcrUniversalConferenceMethod.PREPARE_CONFERENCE_RUNTIME,
            request=request,
            idempotency_key=idempotency_key,
        )

    async def issue_join(
        self,
        conference: ConferenceRef,
        *,
        tenant_id: str,
        external_user_id: str,
        idempotency_key: str,
        ttl_seconds: int = 3600,
    ) -> ConferenceJoin:
        if not 60 <= int(ttl_seconds) <= 86_400:
            raise ValueError("conference join TTL must be 60..86400 seconds")
        capabilities = await self.capabilities(tenant_id=tenant_id)
        if not capabilities.production_realtime:
            raise ConferenceProviderUnavailable(
                "UCR production browser realtime is not proven by capabilities"
            )
        request = self._base(tenant_id)
        request.update(
            {
                "conferenceId": self._conference_id(conference),
                "externalUserId": _bytes(external_user_id),
                "ttlSeconds": int(ttl_seconds),
                "usePolicy": "JOIN_GRANT_USE_POLICY_SINGLE_USE",
                "idempotencyKey": idempotency_key,
            }
        )
        response = await self.gateway.invoke_universal_conference(
            method=UcrUniversalConferenceMethod.ISSUE_JOIN_GRANT,
            request=request,
            idempotency_key=idempotency_key,
        )
        grant = _result(response).get("grant")
        if not isinstance(grant, Mapping):
            raise ConferenceProviderUnavailable("UCR join grant is unavailable")
        session = grant.get("sessionId")
        session_id = (
            str(session.get("value"))
            if isinstance(session, Mapping) and session.get("value")
            else None
        )
        return ConferenceJoin(
            url=str(grant.get("joinUrl") or "").strip(),
            session_id=session_id,
        )

    async def attendance(
        self,
        conference: ConferenceRef,
        *,
        tenant_id: str,
        external_user_id: str,
    ) -> ConferenceAttendance:
        request = self._base(tenant_id)
        request.update(
            {
                "conferenceId": self._conference_id(conference),
                "externalUserId": _bytes(external_user_id),
            }
        )
        response = await self.gateway.invoke_universal_conference(
            method=UcrUniversalConferenceMethod.GET_PARTICIPANT_ATTENDANCE,
            request=request,
        )
        raw = _result(response).get("attendance")
        if not isinstance(raw, Mapping):
            raise ConferenceProviderUnavailable("UCR attendance is unavailable")
        echoed_user_id = _protobuf_bytes(
            raw.get("externalUserId"),
            field_name="attendance.externalUserId",
        )
        expected_user_id = str(external_user_id).strip().encode("utf-8")
        if not hmac.compare_digest(echoed_user_id, expected_user_id):
            raise ConferenceProviderUnavailable(
                "UCR attendance participant does not match request"
            )
        return ConferenceAttendance(
            connected=_protobuf_bool(
                raw.get("connected", False),
                field_name="attendance.connected",
            ),
            total_connected_seconds=_protobuf_uint(
                raw.get("totalConnectedSeconds", 0),
                field_name="attendance.totalConnectedSeconds",
                maximum=10 * 366 * 24 * 60 * 60,
            ),
            join_count=_protobuf_uint(
                raw.get("joinCount", 0),
                field_name="attendance.joinCount",
            ),
            reconnect_count=_protobuf_uint(
                raw.get("reconnectCount", 0),
                field_name="attendance.reconnectCount",
            ),
        )
