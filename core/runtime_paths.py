from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _is_prod() -> bool:
    return (os.getenv("APP_ENV", "dev") or "dev").strip().lower() in {"prod", "production"}


def writable_root() -> Path:
    """Return the only root where runtime-created files may be stored.

    Immutable release directories are content-addressed and verified after the
    process starts. Caches and one-time markers must therefore live outside the
    source/release tree. Production overrides the location explicitly with
    CLIENTPLATFORM_WRITABLE_ROOT.
    """

    explicit = (os.getenv("CLIENTPLATFORM_WRITABLE_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if _is_prod():
        runtime_root = Path(
            (os.getenv("CLIENTPLATFORM_RUNTIME_ROOT") or "/var/lib/clientplatform/runtime").strip()
        ).expanduser()
        return (runtime_root.parent / "state").resolve()
    return (PROJECT_ROOT / "data" / "runtime-state").resolve()


def runtime_dir(name: str) -> Path:
    clean = str(name or "").strip().replace("\\", "/").strip("/")
    if not clean or clean.startswith(".") or ".." in clean.split("/"):
        raise ValueError("invalid runtime directory name")
    path = writable_root() / clean
    path.mkdir(parents=True, exist_ok=True)
    return path


def matplotlib_cache_dir() -> Path:
    explicit = (os.getenv("MPLCONFIGDIR") or "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return runtime_dir("matplotlib")
