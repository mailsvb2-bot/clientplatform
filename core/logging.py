from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _file_logging_disabled() -> bool:
    raw = (os.getenv("LOG_FILE_DISABLED") or os.getenv("DISABLE_FILE_LOGGING") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _default_log_path() -> Path:
    return Path("/var/log/clientplatform/app.log")


def _resolve_log_path(raw: str | None) -> Path:
    path = Path((raw or "").strip() or str(_default_log_path()))
    if not path.is_absolute():
        from core.paths import ROOT

        path = ROOT / path
    return path


def setup_logging() -> None:
    level_name = (os.getenv("LOG_LEVEL", "INFO") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_path = _resolve_log_path(os.getenv("LOG_PATH"))
    max_bytes = int(os.getenv("LOG_MAX_BYTES", str(10_000_000)) or 10_000_000)
    backup_count = int(os.getenv("LOG_BACKUP_COUNT", "5") or 5)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    root = logging.getLogger()
    root.setLevel(level)

    if getattr(setup_logging, "_configured", False):
        return

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    if not _file_logging_disabled():
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            rotating = RotatingFileHandler(
                log_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            rotating.setLevel(level)
            rotating.setFormatter(formatter)
            root.addHandler(rotating)
        except PermissionError:
            root.debug("RotatingFileHandler skipped: permission denied for %s", log_path)
        except OSError:
            root.warning("RotatingFileHandler init failed for %s", log_path)

    setup_logging._configured = True
