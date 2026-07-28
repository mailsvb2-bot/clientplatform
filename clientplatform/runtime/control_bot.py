from __future__ import annotations

import os

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
CONTROL_BOT_CREDENTIAL_ENV = "CLIENTPLATFORM_SECRET_CONTROL_TELEGRAM_BOT_TOKEN"
CONTROL_BOT_CREDENTIAL_REFERENCE = f"secret://env/{CONTROL_BOT_CREDENTIAL_ENV}"


def control_bot_enabled() -> bool:
    """Return the canonical ClientPlatform control-bot mode.

    ClientPlatform is the default product runtime. Operators retain one explicit
    emergency opt-out: ``CLIENTPLATFORM_CONTROL_BOT_ENABLED=0``. Unknown values
    fail fast instead of silently exposing the imported legacy interface.
    """

    raw = os.getenv("CLIENTPLATFORM_CONTROL_BOT_ENABLED")
    if raw is None or not str(raw).strip():
        return True
    normalized = str(raw).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise RuntimeError("clientplatform_control_bot_enabled_invalid")


def bind_control_bot_secret(token: str) -> None:
    """Expose the already-loaded bot token only through the ClientPlatform secret namespace."""
    if not control_bot_enabled():
        return
    normalized = str(token or "").strip()
    if not normalized:
        raise RuntimeError("clientplatform_control_bot_token_missing")
    os.environ.setdefault(CONTROL_BOT_CREDENTIAL_ENV, normalized)
