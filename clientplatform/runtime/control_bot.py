from __future__ import annotations

import os

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
CONTROL_BOT_CREDENTIAL_ENV = "CLIENTPLATFORM_SECRET_CONTROL_TELEGRAM_BOT_TOKEN"
CONTROL_BOT_CREDENTIAL_REFERENCE = f"secret://env/{CONTROL_BOT_CREDENTIAL_ENV}"


def control_bot_enabled() -> bool:
    return str(os.getenv("CLIENTPLATFORM_CONTROL_BOT_ENABLED") or "").strip().lower() in _TRUE_VALUES


def bind_control_bot_secret(token: str) -> None:
    """Expose the already-loaded bot token only through the clientplatform secret namespace."""
    if not control_bot_enabled():
        return
    normalized = str(token or "").strip()
    if not normalized:
        raise RuntimeError("clientplatform_control_bot_token_missing")
    os.environ.setdefault(CONTROL_BOT_CREDENTIAL_ENV, normalized)
