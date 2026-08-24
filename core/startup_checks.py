from __future__ import annotations

import os
from pathlib import Path

from core.paths import resolve_data_dir, resolve_logs_dir


class StartupCheckError(RuntimeError):
    pass


def _truthy_env(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in {"1", "true", "yes", "on", "webhook"}


def _telegram_runtime_enabled() -> bool:
    raw = (os.getenv("CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED") or "1").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise StartupCheckError(
        "CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED must be one of "
        "1/0, true/false, yes/no, on/off"
    )


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name, str(default)) or str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise StartupCheckError(f"Invalid integer env {name}={raw!r}") from exc


def _env_any(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _is_prod() -> bool:
    return (os.getenv("APP_ENV", "dev") or "dev").strip().lower() in {"prod", "production"}


def _resolved_db_engine() -> str:
    raw = (os.getenv("METRO_DB_ENGINE") or "").strip().lower()
    if raw in {"postgres", "postgresql", "pg"}:
        return "postgres"
    if raw in {"sqlite", "sqlite3"}:
        return "sqlite"
    return "postgres" if (os.getenv("DATABASE_URL") or "").strip() else "sqlite"


def _prod_ingress_checks() -> None:
    app_env = (os.getenv("APP_ENV", "dev") or "dev").strip().lower()
    telegram_enabled = _telegram_runtime_enabled()
    telegram_transport = (os.getenv("TELEGRAM_TRANSPORT", os.getenv("RUN_MODE", "polling")) or "polling").strip().lower()
    telegram_webhook = telegram_enabled and (
        telegram_transport == "webhook" or _truthy_env("TELEGRAM_WEBHOOK_ENABLED")
    )
    messenger_webhook = _truthy_env("MESSENGER_WEBHOOK_ENABLED")
    any_webhook = messenger_webhook

    if app_env in {"prod", "production"}:
        if telegram_enabled and not (os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or "").strip():
            raise StartupCheckError("ADMIN_IDS or ADMIN_ID is required in prod when Telegram runtime is enabled")
        if not _truthy_env("HEALTHCHECK_ENABLED", "1"):
            raise StartupCheckError("HEALTHCHECK_ENABLED must be 1 in prod; readiness is part of the deployment contract")
        if telegram_webhook:
            raise StartupCheckError(
                "Telegram production ingress is polling-only: set TELEGRAM_TRANSPORT=polling and TELEGRAM_WEBHOOK_ENABLED=0"
            )
        if _truthy_env("TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED"):
            raise StartupCheckError("TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED must be 0 in prod")
        if _truthy_env("ALLOW_INSECURE_TELEGRAM_WEBHOOK"):
            raise StartupCheckError("ALLOW_INSECURE_TELEGRAM_WEBHOOK is forbidden in prod")

        if _resolved_db_engine() != "postgres":
            raise StartupCheckError("METRO_DB_ENGINE must be postgres in prod")
        database_url = (os.getenv("DATABASE_URL") or "").strip()
        if not database_url:
            raise StartupCheckError("DATABASE_URL is required in prod")
        if not database_url.lower().startswith(("postgresql://", "postgres://")):
            raise StartupCheckError("DATABASE_URL must use postgres/postgresql scheme in prod")

    if any_webhook and _truthy_env("HEALTHCHECK_ENABLED", "1"):
        messenger_host = (os.getenv("MESSENGER_WEBHOOK_HOST") or os.getenv("WEBHOOK_HOST") or "127.0.0.1").strip()
        messenger_port = _int_env("MESSENGER_WEBHOOK_PORT", _int_env("WEBHOOK_PORT", 8081))
        health_host = (os.getenv("HEALTHCHECK_HOST", "127.0.0.1") or "127.0.0.1").strip()
        health_port = _int_env("HEALTHCHECK_PORT", 8082)
        same_host = messenger_host == health_host or "0.0.0.0" in {messenger_host, health_host}  # nosec B104 - sentinel comparison, not a bind
        if same_host and messenger_port == health_port:
            raise StartupCheckError(
                f"Port collision: messenger webhook and healthcheck both bind {messenger_host}:{messenger_port}. "
                "Use separate ports, usually webhook=8081 and health=8082."
            )


def _assert_outside_release(path: Path, release_root: Path, *, label: str) -> None:
    try:
        path.relative_to(release_root)
    except ValueError:
        return
    raise StartupCheckError(f"{label} must be outside immutable production release: {path}")


def run_startup_checks(project_root: Path) -> None:
    """Fail fast when production topology or required project files are invalid.

    Development keeps its historical project-local data/log/audio directories.
    Production releases are content-addressed and read-only: all mutable data and
    logs must live under the external writable-state root, while audio is a sealed
    versioned asset pointer prepared by the immutable release builder.
    """
    root = project_root.resolve()
    production = _is_prod()

    data_dir = resolve_data_dir(root)
    logs_dir = resolve_logs_dir(root)
    if production:
        _assert_outside_release(data_dir, root, label="METRO_DATA_DIR")
        _assert_outside_release(logs_dir, root, label="METRO_LOGS_DIR")
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    audio_dir = root / "audio"
    demo_dir = audio_dir / "demo"
    full_dir = audio_dir / "full"
    if not production:
        demo_dir.mkdir(parents=True, exist_ok=True)
        full_dir.mkdir(parents=True, exist_ok=True)

    critical_files = [
        root / "services" / "idempotency_keys.py",
        root / "core" / "task_manager.py",
        root / "services" / "db_writer.py",
    ]
    for path in critical_files:
        if not path.exists():
            raise StartupCheckError(f"Missing required file: {path}")

    _prod_ingress_checks()

    if _telegram_runtime_enabled() and not _env_any("BOT_TOKEN", "TELEGRAM_BOT_TOKEN"):
        raise StartupCheckError(
            "BOT_TOKEN is empty while Telegram runtime is enabled. "
            "Set BOT_TOKEN or TELEGRAM_BOT_TOKEN, or explicitly disable "
            "CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED for a native-only deployment."
        )
