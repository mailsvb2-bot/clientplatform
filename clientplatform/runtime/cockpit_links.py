from __future__ import annotations

from urllib.parse import urlsplit

from config.settings import settings


def cockpit_web_app_url() -> str | None:
    """Return the first-party HTTPS Mini App URL without creating a new config authority."""

    raw = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not raw:
        return None
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return f"{raw}/clientplatform/cockpit"


__all__ = ["cockpit_web_app_url"]
