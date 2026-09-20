from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from clientplatform.domain.external_products import (
    ExternalObservationQuality,
    ExternalProductObservation,
    ExternalProductReceipt,
)
from clientplatform.runtime.ucr_gateway import (
    UcrUniversalConferenceMethod,
)


_MAX_ATTENDANCE_SECONDS = 10 * 366 * 24 * 60 * 60
_UINT32_MAX = 2**32 - 1
_MAX_UNIX_MS = 253402300799999


class UcrAttendanceGateway(Protocol):
    async def invoke_universal_conference(
        self,
        *,
        method: UcrUniversalConferenceMethod,
        request: Mapping[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class UcrParticipantAttendance:
    first_join_at: datetime | None
    last_leave_at: datetime | None
    first_media_ready_at: datetime | None
    total_connected_seconds: int
    current_connected_seconds: int
    join_count: int
    reconnect_count: int
    media_ready_count: int
    connected: bool

    @property
    def has_attendance(self) -> bool:
        return self.join_count > 0


@dataclass(frozen=True, slots=True)
class UcrAttendanceObservationRead:
    attendance: UcrParticipantAttendance
    observation: ExternalProductObservation | None


def _uint(
    value: object,
    *,
    field_name: str,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"UCR attendance {field_name} must be an unsigned integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw or not raw.isascii() or not raw.isdecimal():
            raise ValueError(f"UCR attendance {field_name} must be an unsigned integer")
        parsed = int(raw, 10)
    else:
        raise ValueError(f"UCR attendance {field_name} must be an unsigned integer")
    if parsed < 0 or parsed > maximum:
        raise ValueError(f"UCR attendance {field_name} is out of range")
    return parsed


def _optional_unix_ms(value: object, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    parsed = _uint(value, field_name=field_name, maximum=_MAX_UNIX_MS)
    try:
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=parsed)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"UCR attendance {field_name} is invalid") from exc


def _proto_bytes(value: object, *, field_name: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"UCR attendance {field_name} must be protobuf JSON bytes")
    raw = value.strip()
    if not raw or len(raw) > 8192:
        raise ValueError(f"UCR attendance {field_name} must be protobuf JSON bytes")
    try:
        decoded = base64.b64decode(raw.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError(
            f"UCR attendance {field_name} must be protobuf JSON bytes"
        ) from exc
    if not decoded or len(decoded) > 4096:
        raise ValueError(f"UCR attendance {field_name} is out of range")
    return decoded


def _requested_external_user_id(request: Mapping[str, Any]) -> bytes:
    camel = request.get("externalUserId")
    snake = request.get("external_user_id")
    if camel is not None and snake is not None:
        camel_bytes = _proto_bytes(camel, field_name="externalUserId")
        snake_bytes = _proto_bytes(snake, field_name="external_user_id")
        if not hmac.compare_digest(camel_bytes, snake_bytes):
            raise ValueError("UCR attendance request contains conflicting participant ids")
        return camel_bytes
    raw = camel if camel is not None else snake
    if raw is None:
        raise ValueError("UCR attendance request is missing external user id")
    return _proto_bytes(raw, field_name="externalUserId")


def parse_ucr_participant_attendance(
    result: Mapping[str, Any],
    *,
    expected_external_user_id: bytes | None = None,
) -> UcrParticipantAttendance:
    if not isinstance(result, Mapping):
        raise ValueError("UCR attendance result must be an object")
    attendance = result.get("attendance")
    if not isinstance(attendance, Mapping):
        raise ValueError("UCR attendance response is missing attendance")
    raw_echoed = attendance.get("externalUserId")
    echoed = (
        None
        if raw_echoed is None
        else _proto_bytes(raw_echoed, field_name="externalUserId")
    )
    if expected_external_user_id is not None:
        if echoed is None or not hmac.compare_digest(echoed, expected_external_user_id):
            raise ValueError("UCR attendance participant does not match request")

    allowed = {
        "externalUserId",
        "firstJoinAtUnixMs",
        "lastLeaveAtUnixMs",
        "firstMediaReadyAtUnixMs",
        "totalConnectedSeconds",
        "currentConnectedSeconds",
        "joinCount",
        "reconnectCount",
        "mediaReadyCount",
        "connected",
    }
    unknown = set(attendance) - allowed
    if unknown:
        raise ValueError("UCR attendance response contains unsupported fields")

    connected = attendance.get("connected", False)
    if not isinstance(connected, bool):
        raise ValueError("UCR attendance connected must be boolean")

    parsed = UcrParticipantAttendance(
        first_join_at=_optional_unix_ms(
            attendance.get("firstJoinAtUnixMs"),
            field_name="firstJoinAtUnixMs",
        ),
        last_leave_at=_optional_unix_ms(
            attendance.get("lastLeaveAtUnixMs"),
            field_name="lastLeaveAtUnixMs",
        ),
        first_media_ready_at=_optional_unix_ms(
            attendance.get("firstMediaReadyAtUnixMs"),
            field_name="firstMediaReadyAtUnixMs",
        ),
        total_connected_seconds=_uint(
            attendance.get("totalConnectedSeconds", 0),
            field_name="totalConnectedSeconds",
            maximum=_MAX_ATTENDANCE_SECONDS,
        ),
        current_connected_seconds=_uint(
            attendance.get("currentConnectedSeconds", 0),
            field_name="currentConnectedSeconds",
            maximum=_MAX_ATTENDANCE_SECONDS,
        ),
        join_count=_uint(
            attendance.get("joinCount", 0),
            field_name="joinCount",
            maximum=_UINT32_MAX,
        ),
        reconnect_count=_uint(
            attendance.get("reconnectCount", 0),
            field_name="reconnectCount",
            maximum=_UINT32_MAX,
        ),
        media_ready_count=_uint(
            attendance.get("mediaReadyCount", 0),
            field_name="mediaReadyCount",
            maximum=_UINT32_MAX,
        ),
        connected=connected,
    )
    if parsed.join_count == 0:
        if (
            parsed.first_join_at is not None
            or parsed.last_leave_at is not None
            or parsed.first_media_ready_at is not None
            or parsed.total_connected_seconds
            or parsed.current_connected_seconds
            or parsed.connected
        ):
            raise ValueError("UCR attendance zero joins conflicts with connection evidence")
        return parsed

    if parsed.first_join_at is None:
        raise ValueError("UCR attendance with joins requires firstJoinAtUnixMs")
    if parsed.last_leave_at is not None and parsed.last_leave_at < parsed.first_join_at:
        raise ValueError("UCR attendance last leave predates first join")
    if (
        parsed.first_media_ready_at is not None
        and parsed.first_media_ready_at < parsed.first_join_at
    ):
        raise ValueError("UCR attendance media ready predates first join")
    if not parsed.connected and parsed.current_connected_seconds != 0:
        raise ValueError("UCR disconnected attendance has current connected duration")
    return parsed


def _provenance_ref(
    request: Mapping[str, Any],
    attendance: UcrParticipantAttendance,
) -> str:
    source_facts = {
        "first_join_at": (
            None if attendance.first_join_at is None else attendance.first_join_at.isoformat()
        ),
        "last_leave_at": (
            None if attendance.last_leave_at is None else attendance.last_leave_at.isoformat()
        ),
        "first_media_ready_at": (
            None
            if attendance.first_media_ready_at is None
            else attendance.first_media_ready_at.isoformat()
        ),
        "total_connected_seconds": attendance.total_connected_seconds,
        "current_connected_seconds": attendance.current_connected_seconds,
        "join_count": attendance.join_count,
        "reconnect_count": attendance.reconnect_count,
        "media_ready_count": attendance.media_ready_count,
        "connected": attendance.connected,
    }
    try:
        canonical = json.dumps(
            {"request": dict(request), "attendance": source_facts},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("UCR attendance request must be finite JSON") from exc
    if not canonical or len(canonical) > 64 * 1024:
        raise ValueError("UCR attendance request is invalid")
    return "ucr-attendance:" + hashlib.sha256(canonical).hexdigest()


def _observed_at(attendance: UcrParticipantAttendance) -> datetime:
    timestamps = tuple(
        value
        for value in (
            attendance.first_join_at,
            attendance.first_media_ready_at,
            attendance.last_leave_at,
        )
        if value is not None
    )
    if not timestamps:
        raise ValueError("UCR attendance has no source observation timestamp")
    return max(timestamps)


def _duration_label(seconds: int) -> str:
    if seconds < 60:
        return "Подключение к онлайн-событию подтверждено"
    minutes = seconds // 60
    if minutes < 120:
        return f"Участие в онлайн-событии подтверждено: {minutes} мин"
    hours = minutes // 60
    remainder = minutes % 60
    if remainder:
        return f"Участие в онлайн-событии подтверждено: {hours} ч {remainder} мин"
    return f"Участие в онлайн-событии подтверждено: {hours} ч"


def ucr_attendance_to_observation(
    *,
    attendance: UcrParticipantAttendance,
    observation_key: str,
    canonical_request: Mapping[str, Any],
    current_head: ExternalProductReceipt | None,
) -> ExternalProductObservation | None:
    if not attendance.has_attendance:
        return None

    limitations: list[str] = []
    if attendance.connected:
        limitations.append(
            "Участник сейчас подключён; итоговая длительность ещё может измениться."
        )
    if attendance.media_ready_count == 0:
        limitations.append("Источник не подтвердил готовность медиа.")
    if attendance.last_leave_at is None and not attendance.connected:
        limitations.append("Источник не сообщил время выхода участника.")

    if current_head is None:
        revision = 1
        supersedes_external_event_id = None
    else:
        if current_head.observation_key != observation_key:
            raise ValueError("current observation head belongs to another key")
        if (
            current_head.observation_revision is None
            or current_head.observation_revision < 1
        ):
            raise ValueError("current observation head has no valid revision")
        revision = current_head.observation_revision + 1
        supersedes_external_event_id = current_head.external_event_id

    return ExternalProductObservation(
        observation_key=observation_key,
        kind="ucr.conference_attendance",
        label=_duration_label(attendance.total_connected_seconds),
        observed_at=_observed_at(attendance),
        provenance_ref=_provenance_ref(canonical_request, attendance),
        revision=revision,
        supersedes_external_event_id=supersedes_external_event_id,
        quality=ExternalObservationQuality.SOURCE_VERIFIED,
        limitations=tuple(limitations),
    )


async def read_ucr_attendance_observation(
    *,
    gateway: UcrAttendanceGateway,
    canonical_request: Mapping[str, Any],
    observation_key: str,
    current_head: ExternalProductReceipt | None,
) -> UcrAttendanceObservationRead:
    response = await gateway.invoke_universal_conference(
        method=UcrUniversalConferenceMethod.GET_PARTICIPANT_ATTENDANCE,
        request=canonical_request,
    )
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("UCR attendance gateway result is missing")
    attendance = parse_ucr_participant_attendance(
        result,
        expected_external_user_id=_requested_external_user_id(canonical_request),
    )
    return UcrAttendanceObservationRead(
        attendance=attendance,
        observation=ucr_attendance_to_observation(
            attendance=attendance,
            observation_key=observation_key,
            canonical_request=canonical_request,
            current_head=current_head,
        ),
    )


__all__ = [
    "UcrAttendanceObservationRead",
    "UcrParticipantAttendance",
    "parse_ucr_participant_attendance",
    "read_ucr_attendance_observation",
    "ucr_attendance_to_observation",
]
