import asyncio
import logging
import os
import sys


_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "webhook"})


def _normalize_telegram_token_env() -> None:
    """Accept the deployment alias used on servers: TELEGRAM_BOT_TOKEN.

    The application settings use BOT_TOKEN as the canonical name. Older server
    snippets and manual webhook commands often export TELEGRAM_BOT_TOKEN instead.
    Normalizing before importing app.py prevents a silent split where Telegram
    setup uses one name while the bot process expects another.
    """

    if not (os.getenv("BOT_TOKEN") or "").strip():
        legacy_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        if legacy_token:
            os.environ["BOT_TOKEN"] = legacy_token


def _enforce_telegram_polling_env() -> bool:
    """Make polling the immutable Telegram transport at process entry.

    VK and MAX keep their independent webhook flags. Returning whether a stale
    Telegram webhook override was present lets the runtime emit a visible warning
    without refusing to start the polling bot.
    """

    raw_transport = (os.getenv("TELEGRAM_TRANSPORT") or os.getenv("RUN_MODE") or "polling").strip().lower()
    raw_webhook_flag = (os.getenv("TELEGRAM_WEBHOOK_ENABLED") or "").strip().lower()
    requested_webhook = raw_transport == "webhook" or raw_webhook_flag in _TRUE_VALUES

    os.environ["TELEGRAM_TRANSPORT"] = "polling"
    os.environ["RUN_MODE"] = "polling"
    os.environ["TELEGRAM_WEBHOOK_ENABLED"] = "0"
    os.environ["TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED"] = "0"
    return requested_webhook


_normalize_telegram_token_env()
_TELEGRAM_WEBHOOK_OVERRIDE_IGNORED = _enforce_telegram_polling_env()

# In production, neither Python bytecode nor third-party caches may mutate the
# content-addressed release directory after its digest has been sealed.
if os.getenv("APP_ENV", "dev").strip().lower() in {"prod", "production"}:
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    sys.dont_write_bytecode = True

from core.runtime_paths import matplotlib_cache_dir

os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache_dir()))

from app import create_application


log = logging.getLogger(__name__)

if _TELEGRAM_WEBHOOK_OVERRIDE_IGNORED:
    log.warning(
        "Telegram webhook configuration was ignored: ClientPlatform Telegram ingress is polling-only"
    )


def _restart_limit() -> int:
    """Return crash-loop limit for APP_SELF_HEAL_RESTART.

    0 means intentionally unlimited. The default is finite so a repeated boot
    failure is visible to systemd/monitoring instead of being hidden forever.
    """

    raw = (os.getenv("APP_SELF_HEAL_MAX_RESTARTS") or "3").strip()
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 3


def _restart_backoff_sec() -> int:
    raw = (os.getenv("APP_SELF_HEAL_BACKOFF_SEC") or "2").strip()
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        log.warning("Bad APP_SELF_HEAL_BACKOFF_SEC=%r; using 2 seconds", raw)
        return 2


async def _run_with_restart() -> None:
    restart_enabled = (os.getenv("APP_SELF_HEAL_RESTART", "0") or "0").strip() in {"1", "true", "yes", "on"}
    backoff = _restart_backoff_sec()
    max_restarts = _restart_limit()
    crash_count = 0

    while True:
        try:
            await create_application()
            return
        except KeyboardInterrupt:
            return
        except (RuntimeError, OSError, ValueError, TypeError, AttributeError, KeyError):  # validator: allow-wide-except
            crash_count += 1
            log.exception("Application crashed")
            if not restart_enabled:
                raise
            if max_restarts and crash_count >= max_restarts:
                log.critical(
                    "Application crash-loop limit reached (%s/%s); refusing to hide repeated failure",
                    crash_count,
                    max_restarts,
                )
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    try:
        asyncio.run(_run_with_restart())
    except KeyboardInterrupt:
        pass
