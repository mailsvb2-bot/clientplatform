from __future__ import annotations

import os
from pathlib import Path

from core.runtime_paths import writable_root

# Единый источник истины по путям.
# Никаких зависимостей от "текущей папки запуска".

ROOT = Path(__file__).resolve().parents[1]


def _is_prod() -> bool:
    return (os.getenv("APP_ENV", "dev") or "dev").strip().lower() in {"prod", "production"}


def _first_env(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _explicit_path(*names: str) -> Path | None:
    raw = _first_env(*names)
    return Path(raw).expanduser().resolve() if raw else None


def resolve_data_dir(project_root: Path | None = None) -> Path:
    explicit = _explicit_path("CLIENTPLATFORM_DATA_DIR")
    if explicit is not None:
        return explicit
    if _is_prod():
        return (writable_root() / "data").resolve()
    return ((project_root or ROOT).resolve() / "data").resolve()


def resolve_logs_dir(project_root: Path | None = None) -> Path:
    explicit = _explicit_path("CLIENTPLATFORM_LOGS_DIR")
    if explicit is not None:
        return explicit
    if _is_prod():
        return (writable_root() / "logs").resolve()
    return ((project_root or ROOT).resolve() / "logs").resolve()


DATA_DIR = resolve_data_dir()
DB_ENGINE = (
    _first_env("CLIENTPLATFORM_DB_ENGINE")
    or ("postgres" if os.getenv("DATABASE_URL") else "sqlite")
).lower()
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
DB_PATH = Path(
    _first_env("CLIENTPLATFORM_DB_PATH")
    or (DATA_DIR / "data.db")
)

LOGS_DIR = resolve_logs_dir()
