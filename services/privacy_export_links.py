from __future__ import annotations

import hashlib
import os
import re
import secrets
import urllib.parse
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from config.settings import settings
from core.time_utils import utc_now
from services.db import db, tx

PRIVACY_EXPORT_PREFIX = "/privacy/export/"
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")


@dataclass(frozen=True)
class PrivacyExportGrant:
    token_hash: str
    user_id: int
    platform: str
    created_at: str
    consumed_at: str | None


def privacy_export_ttl_minutes() -> int:
    raw = (os.getenv("PRIVACY_EXPORT_TOKEN_TTL_MINUTES") or "10").strip()
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return 10
    return max(2, min(parsed, 30))


def _app_env() -> str:
    return (os.getenv("APP_ENV") or getattr(settings, "APP_ENV", "") or "dev").strip().lower()


def privacy_export_public_base_url() -> str:
    raw = (
        os.getenv("PRIVACY_EXPORT_PUBLIC_BASE_URL", "").strip()
        or os.getenv("MESSENGER_PUBLIC_BASE_URL", "").strip()
        or os.getenv("PUBLIC_BASE_URL", "").strip()
        or str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip()
        or str(getattr(settings, "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL", "") or "").strip()
    ).rstrip("/")
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if _app_env() in {"prod", "production", "stage", "staging"} and parsed.scheme != "https":
        return ""
    return raw


def privacy_export_http_enabled() -> bool:
    raw = os.getenv("PRIVACY_EXPORT_HTTP_ENABLED")
    enabled = str(raw or "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return False
    if not privacy_export_public_base_url():
        raise RuntimeError("PRIVACY_EXPORT_HTTP_ENABLED requires a valid public HTTPS base URL")
    return True


def _grant_expired(created_at: str, *, now: datetime | None = None) -> bool:
    try:
        created = datetime.fromisoformat(str(created_at))
    except (TypeError, ValueError):
        return True
    current = now or utc_now()
    if created.tzinfo is None:
        created = created.replace(tzinfo=current.tzinfo)
    return current >= created + timedelta(minutes=privacy_export_ttl_minutes())


def _token_hash(token: str) -> str:
    raw = str(token or "").strip()
    if _TOKEN_PATTERN.fullmatch(raw) is None:
        return ""
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def _row_to_grant(row: Any) -> PrivacyExportGrant:
    return PrivacyExportGrant(
        token_hash=str(row["token_hash"]),
        user_id=int(row["user_id"]),
        platform=str(row["platform"]),
        created_at=str(row["created_at"] or ""),
        consumed_at=str(row["consumed_at"]) if row["consumed_at"] is not None else None,
    )


def issue_privacy_export_token(user_id: int, *, platform: str) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = _token_hash(token)
    if not token_hash:
        raise RuntimeError("privacy_export_token_generation_failed")
    created_at = utc_now().replace(microsecond=0).isoformat()
    with db() as conn:
        with tx(conn):
            conn.execute(
                "DELETE FROM user_privacy_export_tokens WHERE user_id=? AND consumed_at IS NULL",
                (int(user_id),),
            )
            conn.execute(
                """
                INSERT INTO user_privacy_export_tokens(
                    token_hash, user_id, platform, created_at, consumed_at
                ) VALUES(?,?,?,?,NULL)
                """.strip(),
                (
                    token_hash,
                    int(user_id),
                    str(platform or "unknown").strip().lower(),
                    created_at,
                ),
            )
    return token


def issue_privacy_export_url(user_id: int, *, platform: str) -> str:
    if not privacy_export_http_enabled():
        return ""
    base = privacy_export_public_base_url()
    token = issue_privacy_export_token(int(user_id), platform=platform)
    return f"{base}{PRIVACY_EXPORT_PREFIX}{urllib.parse.quote(token, safe='')}"


def get_privacy_export_grant(token: str) -> PrivacyExportGrant | None:
    token_hash = _token_hash(token)
    if not token_hash:
        return None
    with db() as conn:
        row = conn.execute(
            """
            SELECT token_hash, user_id, platform, created_at, consumed_at
            FROM user_privacy_export_tokens
            WHERE token_hash=?
            """.strip(),
            (token_hash,),
        ).fetchone()
    if not row:
        return None
    grant = _row_to_grant(row)
    if grant.consumed_at is not None or _grant_expired(grant.created_at):
        return None
    return grant


def claim_privacy_export_grant(token: str) -> PrivacyExportGrant | None:
    token_hash = _token_hash(token)
    if not token_hash:
        return None
    grant = get_privacy_export_grant(token)
    if grant is None:
        return None
    consumed_at = utc_now().replace(microsecond=0).isoformat()
    with db() as conn:
        with tx(conn):
            cursor = conn.execute(
                """
                UPDATE user_privacy_export_tokens
                SET consumed_at=?
                WHERE token_hash=? AND consumed_at IS NULL
                """.strip(),
                (consumed_at, token_hash),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                return None
    return replace(grant, consumed_at=consumed_at)
