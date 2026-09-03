from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class TelegramWebAppAuthError(ValueError):
    """Telegram Mini App admission proof is missing, stale or invalid."""


@dataclass(frozen=True, slots=True)
class TelegramWebAppPrincipal:
    user_id: int
    auth_date: int
    query_id: str | None


def _strict_pairs(init_data: str) -> dict[str, str]:
    raw = str(init_data or "")
    if not raw or len(raw.encode("utf-8")) > 12_288:
        raise TelegramWebAppAuthError("invalid initData size")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise TelegramWebAppAuthError("malformed initData") from exc
    values: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in values:
            raise TelegramWebAppAuthError("duplicate or empty initData field")
        values[key] = value
    return values


def verify_telegram_webapp_init_data(
    init_data: str,
    *,
    bot_token: str,
    now: float | int | None = None,
    max_age_seconds: int = 300,
    future_skew_seconds: int = 30,
) -> TelegramWebAppPrincipal:
    """Verify Telegram WebApp initData without trusting any frontend identity field."""

    token = str(bot_token or "").strip()
    if not token:
        raise TelegramWebAppAuthError("Telegram bot credential is unavailable")
    if max_age_seconds < 1 or future_skew_seconds < 0:
        raise ValueError("invalid Telegram WebApp freshness policy")

    values = _strict_pairs(init_data)
    supplied_hash = values.pop("hash", "")
    if not _HASH_RE.fullmatch(supplied_hash):
        raise TelegramWebAppAuthError("invalid initData hash")

    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied_hash.lower(), expected_hash):
        raise TelegramWebAppAuthError("initData signature mismatch")

    raw_auth_date = values.get("auth_date")
    if raw_auth_date is None:
        raise TelegramWebAppAuthError("invalid initData auth_date")
    try:
        auth_date = int(raw_auth_date)
    except ValueError as exc:
        raise TelegramWebAppAuthError("invalid initData auth_date") from exc
    current = int(time.time() if now is None else now)
    if auth_date > current + int(future_skew_seconds):
        raise TelegramWebAppAuthError("initData auth_date is in the future")
    if current - auth_date > int(max_age_seconds):
        raise TelegramWebAppAuthError("initData has expired")

    raw_user = values.get("user")
    if raw_user is None:
        raise TelegramWebAppAuthError("invalid initData user")
    try:
        user = json.loads(raw_user)
    except json.JSONDecodeError as exc:
        raise TelegramWebAppAuthError("invalid initData user") from exc
    if not isinstance(user, dict):
        raise TelegramWebAppAuthError("invalid initData user")
    user_id = user.get("id")
    if isinstance(user_id, bool):
        raise TelegramWebAppAuthError("invalid Telegram user id")
    if user_id is None:
        raise TelegramWebAppAuthError("invalid Telegram user id")
    try:
        normalized_user_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise TelegramWebAppAuthError("invalid Telegram user id") from exc
    if normalized_user_id <= 0:
        raise TelegramWebAppAuthError("invalid Telegram user id")

    query_id = str(values.get("query_id") or "").strip() or None
    return TelegramWebAppPrincipal(
        user_id=normalized_user_id,
        auth_date=auth_date,
        query_id=query_id,
    )


__all__ = [
    "TelegramWebAppAuthError",
    "TelegramWebAppPrincipal",
    "verify_telegram_webapp_init_data",
]
