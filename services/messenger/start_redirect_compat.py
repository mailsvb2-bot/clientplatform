from __future__ import annotations

"""Pure compatibility redirect for already-published historical start links.

No attribution, campaign state or database writes live here.  New acquisition
links are issued by the tenant-scoped ClientPlatform promotion subsystem.
"""

import hashlib
import os
import re
from urllib.parse import quote_plus

_TELEGRAM_START_MAX_LEN = 64


def _safe_username(value: object) -> str:
    text = str(value or "").strip().replace("@", "").lower()
    text = re.sub(r"[^a-z0-9_]+", "_", text).strip("_")
    return (text or "clientplatformbot")[:64]


def _safe_payload(value: object) -> str:
    clean = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if len(clean) <= _TELEGRAM_START_MAX_LEN:
        return clean
    digest = hashlib.blake2s(clean.encode("utf-8"), digest_size=5).hexdigest()
    suffix = f"__h_{digest}"
    prefix = clean[: _TELEGRAM_START_MAX_LEN - len(suffix)].rstrip("_-")
    return f"{prefix}{suffix}"


def historical_start_redirect(value: object) -> str:
    username = _safe_username(
        os.getenv("TELEGRAM_BOT_USERNAME")
        or os.getenv("BOT_USERNAME")
        or os.getenv("TELEGRAM_USERNAME")
    )
    return f"https://t.me/{username}?start={quote_plus(_safe_payload(value))}"


__all__ = ["historical_start_redirect"]
