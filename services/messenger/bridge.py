from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from config.settings import settings
from core.time_utils import utc_now
from services.accounts.identity import _link_channel_to_account_in_conn, ensure_account
from services.db import db, tx
from services.messenger.platforms import parse_platform


@dataclass(frozen=True)
class BridgeResolution:
    canonical_user_id: int
    token: str
    consumed: bool
    target_platform: str | None = None


PURPOSE_SWITCH = "switch_messenger"


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def issue_bridge_token(
    user_id: int,
    *,
    purpose: str = PURPOSE_SWITCH,
    target_platform: str | None = None,
    created_from_platform: str | None = None,
    created_from_external_user_id: str | None = None,
) -> str:
    account_id = ensure_account(int(user_id))
    token = secrets.token_urlsafe(18)
    now_dt = utc_now().replace(microsecond=0)
    now = now_dt.isoformat()
    ttl_hours = int(getattr(settings, "MESSENGER_BRIDGE_TOKEN_TTL_HOURS", 72) or 72)
    expires_at = (now_dt + timedelta(hours=ttl_hours)).isoformat()
    target = parse_platform(target_platform or "") if target_platform else None
    source = parse_platform(created_from_platform or "") if created_from_platform else None
    with db() as conn:
        with tx(conn):
            conn.execute(
                """
                INSERT INTO user_channel_bridge_tokens(
                    token, user_id, purpose, created_at,
                    account_id, target_platform, created_from_platform,
                    created_from_external_user_id, expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """.strip(),
                (
                    token,
                    int(user_id),
                    str(purpose),
                    now,
                    int(account_id),
                    target,
                    source,
                    (created_from_external_user_id or "").strip() or None,
                    expires_at,
                ),
            )
    return token


def _resolve_bridge_token_in_conn(conn: Any, raw: str) -> BridgeResolution | None:
    row = conn.execute(
        """
        SELECT token, user_id, used_at, created_at, account_id, target_platform, expires_at
        FROM user_channel_bridge_tokens
        WHERE token=? AND purpose=?
        """.strip(),
        (raw, PURPOSE_SWITCH),
    ).fetchone()
    if not row:
        return None
    expires_at = _row_value(row, "expires_at")
    created_at = _row_value(row, "created_at")
    if expires_at:
        try:
            if datetime.fromisoformat(str(expires_at)) < utc_now():
                return None
        except (ValueError, TypeError):
            return None
    elif created_at:
        try:
            created = datetime.fromisoformat(str(created_at))
            ttl_hours = int(getattr(settings, "MESSENGER_BRIDGE_TOKEN_TTL_HOURS", 72) or 72)
            if created + timedelta(hours=ttl_hours) < utc_now():
                return None
        except (ValueError, TypeError):
            return None
    account_id = _row_value(row, "account_id") or _row_value(row, "user_id")
    target_platform = _row_value(row, "target_platform")
    return BridgeResolution(
        canonical_user_id=int(account_id),
        token=str(_row_value(row, "token")),
        consumed=bool(_row_value(row, "used_at")),
        target_platform=(str(target_platform) if target_platform else None),
    )


def resolve_bridge_token_in_conn(conn: Any, token: str) -> BridgeResolution | None:
    raw = (token or "").strip()
    if not raw:
        return None
    return _resolve_bridge_token_in_conn(conn, raw)


def resolve_bridge_token(token: str) -> BridgeResolution | None:
    with db() as conn:
        return resolve_bridge_token_in_conn(conn, token)


def _consume_bridge_token_in_conn(
    conn: Any,
    *,
    raw: str,
    norm: str,
    external_user_id: str | None,
) -> BridgeResolution | None:
    resolved = _resolve_bridge_token_in_conn(conn, raw)
    if resolved is None or resolved.consumed:
        return None
    if resolved.target_platform and resolved.target_platform != norm:
        return None

    now = utc_now().replace(microsecond=0).isoformat()
    external_id = (external_user_id or "").strip() or None
    cursor = conn.execute(
        """
        UPDATE user_channel_bridge_tokens
        SET used_at=?,
            used_platform=?,
            used_external_user_id=?,
            consumed_account_id=?
        WHERE token=? AND purpose=? AND used_at IS NULL
        """.strip(),
        (
            now,
            norm,
            external_id,
            int(resolved.canonical_user_id),
            raw,
            PURPOSE_SWITCH,
        ),
    )
    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
        return None
    return BridgeResolution(
        canonical_user_id=resolved.canonical_user_id,
        token=resolved.token,
        consumed=True,
        target_platform=resolved.target_platform,
    )


def consume_bridge_token(token: str, *, platform: str, external_user_id: str | None) -> BridgeResolution | None:
    raw = (token or "").strip()
    norm = parse_platform(platform)
    if not raw or norm is None:
        return None
    with db() as conn:
        with tx(conn):
            return _consume_bridge_token_in_conn(
                conn,
                raw=raw,
                norm=norm,
                external_user_id=external_user_id,
            )


def consume_bridge_token_and_link_in_conn(
    conn: Any,
    token: str,
    *,
    platform: str,
    external_user_id: str | None,
    username: str | None = None,
    display_name: str | None = None,
) -> BridgeResolution | None:
    """Consume and link inside the caller's transaction."""

    raw = (token or "").strip()
    norm = parse_platform(platform)
    if not raw or norm is None:
        return None
    resolved = _consume_bridge_token_in_conn(
        conn, raw=raw, norm=norm, external_user_id=external_user_id
    )
    if resolved is None:
        return None
    _link_channel_to_account_in_conn(
        conn,
        int(resolved.canonical_user_id),
        norm,
        external_user_id,
        username=username,
        display_name=display_name,
        verified=True,
        link_source="bridge",
    )
    return resolved


def consume_bridge_token_and_link(
    token: str,
    *,
    platform: str,
    external_user_id: str | None,
    username: str | None = None,
    display_name: str | None = None,
) -> BridgeResolution | None:
    """Consume a bridge token and link the identity in one transaction.

    If identity linking fails, the token mutation is rolled back and the user may
    retry after resolving the conflict. Concurrent consumers still compete on the
    single ``used_at IS NULL`` update, so at most one identity can win.
    """

    raw = (token or "").strip()
    norm = parse_platform(platform)
    if not raw or norm is None:
        return None
    with db() as conn:
        with tx(conn):
            resolved = _consume_bridge_token_in_conn(
                conn,
                raw=raw,
                norm=norm,
                external_user_id=external_user_id,
            )
            if resolved is None:
                return None
            _link_channel_to_account_in_conn(
                conn,
                int(resolved.canonical_user_id),
                norm,
                external_user_id,
                username=username,
                display_name=display_name,
                verified=True,
                link_source="bridge",
            )
            return resolved
