from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from clientplatform.domain.owner_input import OwnerInputSession
from clientplatform.domain.tenancy import TenantAccessDenied, TenantPermissionDenied
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository

_ALLOWED_PLATFORMS = frozenset({"telegram", "vk", "max"})
_ALLOWED_ACTIONS = frozenset(
    {
        "activity_description",
        "booking_time",
        "member_user",
        "offering",
        "payment",
        "price",
        "program_lesson",
        "program_title",
        "publication_draft",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _normalize_platform(value: object) -> str:
    platform = str(value or "").strip().casefold()
    if platform not in _ALLOWED_PLATFORMS:
        raise ValueError("owner input platform is invalid")
    return platform


def _normalize_action(value: object) -> str:
    action = str(value or "").strip().casefold()
    if action not in _ALLOWED_ACTIONS:
        raise ValueError("owner input action is invalid")
    return action


def _normalize_context(value: Mapping[str, object] | None) -> dict[str, str]:
    context: dict[str, str] = {}
    for raw_key, raw_value in dict(value or {}).items():
        key = str(raw_key or "").strip()
        item = str(raw_value or "").strip()
        if not key or len(key) > 80 or len(item) > 2048:
            raise ValueError("owner input context is invalid")
        context[key] = item
    encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 4096:
        raise ValueError("owner input context is too large")
    return context


class OwnerInputRepository:
    def __init__(self, conn: Any):
        self._conn = conn

    def set(
        self,
        *,
        user_id: int,
        platform: str,
        business_id: str,
        action: str,
        context: Mapping[str, object] | None = None,
        now: str | None = None,
    ) -> OwnerInputSession:
        current = TenancyRepository(self._conn).resolve_context(
            user_id=user_id,
            business_id=business_id,
        )
        normalized_platform = _normalize_platform(platform)
        normalized_action = _normalize_action(action)
        normalized_context = _normalize_context(context)
        timestamp = str(now or _utc_now())
        encoded = json.dumps(
            normalized_context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._conn.execute(
            """
            INSERT INTO clientplatform_owner_input_sessions(
                user_id, platform, business_id, action, context_json, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, platform) DO UPDATE SET
                business_id=excluded.business_id,
                action=excluded.action,
                context_json=excluded.context_json,
                updated_at=excluded.updated_at
            """,
            (
                current.user_id,
                normalized_platform,
                current.business_id,
                normalized_action,
                encoded,
                timestamp,
            ),
        )
        return OwnerInputSession(
            user_id=current.user_id,
            platform=normalized_platform,
            business_id=current.business_id,
            action=normalized_action,
            context=normalized_context,
            updated_at=timestamp,
        )

    def get(self, *, user_id: int, platform: str) -> OwnerInputSession | None:
        normalized_platform = _normalize_platform(platform)
        row = self._conn.execute(
            """
            SELECT user_id, platform, business_id, action, context_json, updated_at
            FROM clientplatform_owner_input_sessions
            WHERE user_id=? AND platform=?
            LIMIT 1
            """,
            (int(user_id), normalized_platform),
        ).fetchone()
        if row is None:
            return None
        business_id = str(_value(row, "business_id", 2))
        try:
            current = TenancyRepository(self._conn).resolve_context(
                user_id=int(user_id),
                business_id=business_id,
            )
        except (TenantAccessDenied, TenantPermissionDenied):
            return None
        action = _normalize_action(_value(row, "action", 3))
        try:
            context_raw = json.loads(str(_value(row, "context_json", 4) or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("owner input context is corrupt") from exc
        if not isinstance(context_raw, dict):
            raise ValueError("owner input context is corrupt")
        context = _normalize_context(context_raw)
        return OwnerInputSession(
            user_id=current.user_id,
            platform=normalized_platform,
            business_id=current.business_id,
            action=action,
            context=context,
            updated_at=str(_value(row, "updated_at", 5)),
        )

    def clear(self, *, user_id: int, platform: str) -> None:
        self._conn.execute(
            "DELETE FROM clientplatform_owner_input_sessions WHERE user_id=? AND platform=?",
            (int(user_id), _normalize_platform(platform)),
        )
