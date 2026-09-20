from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from clientplatform.domain.external_products import (
    ExternalObservationQuality,
    ExternalProductObservation,
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
        return datetime.fromtimestamp(parsed / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"UCR attendance {field_name} is invalid") from exc


def parse_ucr_participant_attendance(
    result: Mapping[str, Any],
) -> UcrParticipantAttendance:
    if not isinstance(result, Mapping):
        raise ValueError("UCR attendance result must be an object")
    attendance = result.get("attendance")
    if not isinstance(attendance, Mapping):
        raise ValueError("UCR attendance response is missing attendance")

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

    connected = attendance.get("connected")
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


def _provenance_ref(request: Mapping[str, Any]) -> str:
    try:
        canonical = json.dumps(
            dict(request),
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

    return ExternalProductObservation(
        observation_key=observation_key,
        kind="ucr.conference_attendance",
        label=_duration_label(attendance.total_connected_seconds),
        observed_at=_observed_at(attendance),
        provenance_ref=_provenance_ref(canonical_request),
        quality=ExternalObservationQuality.SOURCE_VERIFIED,
        limitations=tuple(limitations),
    )


async def read_ucr_attendance_observation(
    *,
    gateway: UcrAttendanceGateway,
    canonical_request: Mapping[str, Any],
    observation_key: str,
) -> UcrAttendanceObservationRead:
    response = await gateway.invoke_universal_conference(
        method=UcrUniversalConferenceMethod.GET_PARTICIPANT_ATTENDANCE,
        request=canonical_request,
    )
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("UCR attendance gateway result is missing")
    attendance = parse_ucr_participant_attendance(result)
    return UcrAttendanceObservationRead(
        attendance=attendance,
        observation=ucr_attendance_to_observation(
            attendance=attendance,
            observation_key=observation_key,
            canonical_request=canonical_request,
        ),
    )


__all__ = [
    "UcrAttendanceObservationRead",
    "UcrParticipantAttendance",
    "parse_ucr_participant_attendance",
    "read_ucr_attendance_observation",
    "ucr_attendance_to_observation",
]
