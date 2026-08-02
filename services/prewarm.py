from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile

from config.settings import settings
from core.runtime_paths import prewarm_marker_path
from services.audio_cache import get_cached_file_id, save_cached_file_id
from services.catalog import AudioCatalog

log = logging.getLogger(__name__)

_audio_prewarm_task: asyncio.Task[None] | None = None
_matplotlib_prewarm_task: asyncio.Task[None] | None = None
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _marker_path() -> Path:
    return prewarm_marker_path()


def _content_fingerprint(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted((Path(item) for item in paths), key=lambda item: str(item)):
        resolved = path.resolve()
        digest.update(str(resolved).encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        try:
            stat = resolved.stat()
        except OSError:
            digest.update(b"missing")
        else:
            digest.update(str(int(stat.st_size)).encode("ascii"))
            digest.update(b":")
            digest.update(str(int(stat.st_mtime_ns)).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _already_done(fingerprint: str) -> bool:
    try:
        return _marker_path().read_text(encoding="utf-8").strip() == str(fingerprint)
    except FileNotFoundError:
        return False
    except OSError:
        logging.getLogger(__name__).exception("Failed to read prewarm marker")
        return False


def _mark_done(fingerprint: str) -> None:
    try:
        marker = _marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        temp = marker.with_name(f".{marker.name}.{int(time.time())}.tmp")
        temp.write_text(str(fingerprint), encoding="utf-8")
        temp.replace(marker)
    except OSError:
        logging.getLogger(__name__).exception("Failed to write prewarm marker")


def _admin_chat_id() -> int | None:
    raw_chat = str(getattr(settings, "PREWARM_CHAT_ID", "") or "").strip()
    if raw_chat:
        try:
            return int(raw_chat)
        except (TypeError, ValueError):
            logging.getLogger(__name__).warning("Invalid PREWARM_CHAT_ID; falling back to ADMIN_IDS")

    raw = str(getattr(settings, "ADMIN_IDS", "") or "").strip()
    if not raw:
        return None
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            return int(part)
        except (TypeError, ValueError):
            logging.getLogger(__name__).warning("Invalid ADMIN_IDS entry ignored: %r", part)
            continue
    return None


def _defer_during_clientplatform_startup() -> bool:
    """Keep optional cache warming out of the Telegram polling critical path."""

    explicit = (os.getenv("CLIENTPLATFORM_DEFER_PREWARM") or "").strip().lower()
    if explicit:
        return explicit in _TRUE_VALUES
    return (os.getenv("CLIENTPLATFORM_DEPLOYMENT_MODE") or "").strip().lower() == "container"


async def _prewarm_audio_cache_worker(bot: Bot) -> None:
    if not getattr(settings, "PREWARM_ENABLED", False):
        return
    chat_id = _admin_chat_id()
    if not chat_id:
        log.info("prewarm: ADMIN_IDS not set -> skip")
        return

    catalog = AudioCatalog()
    files: list[Path] = []
    retryable_failure = False
    try:
        files.extend(catalog.get_demo() or [])
    except OSError:
        retryable_failure = True
        logging.getLogger(__name__).exception("prewarm demo catalog read failed")
    try:
        files.extend(catalog.get_full() or [])
    except OSError:
        retryable_failure = True
        logging.getLogger(__name__).exception("prewarm full catalog read failed")

    seen: set[str] = set()
    unique: list[Path] = []
    for path in files:
        resolved = str(Path(path).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(Path(path))

    fingerprint = _content_fingerprint(unique)
    if not retryable_failure and _already_done(fingerprint):
        return

    for path in unique:
        try:
            kind = "voice" if path.suffix.lower() in (".ogg", ".opus") else "audio"
            if get_cached_file_id(path, kind):
                continue
            if not path.exists():
                retryable_failure = True
                log.warning("prewarm source missing: %s", path)
                continue

            if kind == "voice":
                message = await bot.send_voice(
                    chat_id,
                    voice=FSInputFile(path),
                    caption="(prewarm)",
                    disable_notification=True,
                    protect_content=True,
                )
                file_id = getattr(getattr(message, "voice", None), "file_id", None)
                if file_id:
                    save_cached_file_id(path, "voice", str(file_id))
                else:
                    retryable_failure = True
            else:
                message = await bot.send_audio(
                    chat_id,
                    audio=FSInputFile(path),
                    caption="(prewarm)",
                    disable_notification=True,
                    protect_content=True,
                )
                file_id = getattr(getattr(message, "audio", None), "file_id", None)
                if file_id:
                    save_cached_file_id(path, "audio", str(file_id))
                else:
                    retryable_failure = True

            await asyncio.sleep(0.2)
        except (TelegramAPIError, OSError, asyncio.TimeoutError) as exc:
            retryable_failure = True
            log.info("prewarm failed for %s: %s", str(path), exc)

    if not retryable_failure:
        _mark_done(fingerprint)


async def prewarm_audio_cache(bot: Bot) -> None:
    """Pre-upload audio without delaying ClientPlatform Telegram polling.

    Non-ClientPlatform callers retain the original await-until-complete contract.
    The production ClientPlatform container delegates the optional work to the
    canonical TaskManager, so startup and buttons are never blocked by uploads.
    """

    global _audio_prewarm_task

    if not _defer_during_clientplatform_startup():
        await _prewarm_audio_cache_worker(bot)
        return
    if _audio_prewarm_task is not None and not _audio_prewarm_task.done():
        return

    from services.bg import tm

    _audio_prewarm_task = tm().create(
        _prewarm_audio_cache_worker(bot),
        name="clientplatform-audio-prewarm",
    )
    await asyncio.sleep(0)


async def _prewarm_matplotlib_cache_worker() -> None:
    try:
        from services.charts import _ensure_mpl
    except (ImportError, OSError):
        logging.getLogger(__name__).exception("prewarm_matplotlib_cache: import failed")
        return
    try:
        await asyncio.to_thread(_ensure_mpl)
        log.info("matplotlib cache prewarmed")
    except OSError:
        logging.getLogger(__name__).exception("prewarm_matplotlib_cache failed")


async def prewarm_matplotlib_cache() -> None:
    """Warm Matplotlib without keeping ClientPlatform polling offline."""

    global _matplotlib_prewarm_task

    if not _defer_during_clientplatform_startup():
        await _prewarm_matplotlib_cache_worker()
        return
    if _matplotlib_prewarm_task is not None and not _matplotlib_prewarm_task.done():
        return

    from services.bg import tm

    _matplotlib_prewarm_task = tm().create(
        _prewarm_matplotlib_cache_worker(),
        name="clientplatform-matplotlib-prewarm",
    )
    await asyncio.sleep(0)
