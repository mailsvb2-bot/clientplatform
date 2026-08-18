from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl


class TelegramWebAppAuthError(ValueError):
    """Telegram Mini App authentication failed closed."""


@dataclass(frozen=True, slots=True)
class TelegramWebAppPrincipal:
    user_id: int
    auth_date: int


def validate_telegram_webapp_init_data(
    *,
    init_data: str,
    bot_token: str,
    now: datetime | None = None,
    max_age_seconds: int = 300,
) -> TelegramWebAppPrincipal:
    """Validate Telegram ``WebApp.initData`` and return the authenticated user.

    Raw init data is deliberately not retained in the return value so callers
    cannot accidentally log a reusable Mini App credential.
    """

    raw = str(init_data or "").strip()
    token = str(bot_token or "").strip()
    if not raw or not token:
        raise TelegramWebAppAuthError("telegram_webapp_credentials_missing")
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise TelegramWebAppAuthError("telegram_webapp_init_data_malformed") from exc
    if not pairs:
        raise TelegramWebAppAuthError("telegram_webapp_init_data_empty")

    values: dict[str, str] = {}
    for key, value in pairs:
        normalized_key = str(key or "").strip()
        if not normalized_key or normalized_key in values:
            raise TelegramWebAppAuthError("telegram_webapp_init_data_duplicate_field")
        values[normalized_key] = value

    provided_hash = str(values.get("hash") or "").strip().lower()
    if len(provided_hash) != 64:
        raise TelegramWebAppAuthError("telegram_webapp_hash_invalid")
    try:
        int(provided_hash, 16)
    except ValueError as exc:
        raise TelegramWebAppAuthError("telegram_webapp_hash_invalid") from exc

    # Telegram's bot-token validation signs the received field pairs.  The
    # transport integrity fields themselves are excluded; ``signature`` is the
    # independent Ed25519 proof intended for third-party validation.
    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(values.items())
        if key not in {"hash", "signature"}
    )
    secret_key = hmac.new(
        b"WebAppData",
        token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, provided_hash):
        raise TelegramWebAppAuthError("telegram_webapp_hash_mismatch")

    auth_date_raw = str(values.get("auth_date") or "").strip()
    try:
        auth_date = int(auth_date_raw)
    except (TypeError, ValueError) as exc:
        raise TelegramWebAppAuthError("telegram_webapp_auth_date_invalid") from exc
    if auth_date <= 0:
        raise TelegramWebAppAuthError("telegram_webapp_auth_date_invalid")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current_ts = int(current.timestamp())
    if auth_date > current_ts + 30:
        raise TelegramWebAppAuthError("telegram_webapp_auth_date_future")
    if current_ts - auth_date > max_age_seconds:
        raise TelegramWebAppAuthError("telegram_webapp_auth_date_stale")

    user_raw = values.get("user")
    if not user_raw:
        raise TelegramWebAppAuthError("telegram_webapp_user_missing")
    try:
        user = json.loads(user_raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelegramWebAppAuthError("telegram_webapp_user_invalid") from exc
    if not isinstance(user, dict) or isinstance(user.get("id"), bool):
        raise TelegramWebAppAuthError("telegram_webapp_user_invalid")
    try:
        user_id = int(user.get("id"))
    except (TypeError, ValueError) as exc:
        raise TelegramWebAppAuthError("telegram_webapp_user_invalid") from exc
    if user_id <= 0:
        raise TelegramWebAppAuthError("telegram_webapp_user_invalid")

    return TelegramWebAppPrincipal(user_id=user_id, auth_date=auth_date)


__all__ = [
    "TelegramWebAppAuthError",
    "TelegramWebAppPrincipal",
    "validate_telegram_webapp_init_data",
]
