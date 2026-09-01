from __future__ import annotations

"""Fail-closed production-isolation contract for ClientPlatform.

The checker is dependency-light and does not contact Telegram, PostgreSQL or
object storage. Live probes are separate commands so this can run before the
application process receives production credentials.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

_TRUE = frozenset({"1", "true", "yes", "on"})
_PLACEHOLDERS = frozenset({"", "changeme", "change-me", "secret", "password", "token"})


def _value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def _truthy(env: Mapping[str, str], name: str) -> bool:
    return _value(env, name).lower() in _TRUE


def _looks_placeholder(value: str) -> bool:
    normalized = value.strip()
    lowered = normalized.lower()
    return (
        lowered in _PLACEHOLDERS
        or normalized.upper().startswith(("PASTE_", "CHANGE_"))
        or "your-domain" in lowered
        or lowered.endswith(".example.com")
    )


def _require(env: Mapping[str, str], name: str, errors: list[str]) -> str:
    value = _value(env, name)
    if _looks_placeholder(value):
        errors.append(f"{name} is missing or placeholder")
    return value


def _https_url(env: Mapping[str, str], name: str, errors: list[str]) -> str:
    value = _require(env, name, errors)
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        errors.append(f"{name} must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        errors.append(f"{name} must not contain credentials, query or fragment")
    return value.rstrip("/")


def _positive_int(
    env: Mapping[str, str],
    name: str,
    *,
    minimum: int,
    maximum: int,
    errors: list[str],
) -> int:
    raw = _value(env, name)
    try:
        parsed = int(raw)
    except ValueError:
        errors.append(f"{name} must be an integer")
        return minimum
    if parsed < minimum or parsed > maximum:
        errors.append(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _absolute_path(env: Mapping[str, str], name: str, errors: list[str]) -> Path | None:
    value = _require(env, name, errors)
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        errors.append(f"{name} must be an absolute path")
        return None
    return path.resolve()


def _validate_admin_ids(env: Mapping[str, str], errors: list[str]) -> None:
    raw = _require(env, "ADMIN_IDS", errors)
    values = [part.strip() for part in raw.split(",") if part.strip()]
    if not values or any(not value.isdigit() or int(value) <= 0 for value in values):
        errors.append("ADMIN_IDS must contain positive numeric Telegram IDs")


def _validate_identity_and_secrets(env: Mapping[str, str], errors: list[str]) -> None:
    if _value(env, "CLIENTPLATFORM_DEPLOYMENT_ID") != "clientplatform-production":
        errors.append("CLIENTPLATFORM_DEPLOYMENT_ID must equal clientplatform-production")
    if _value(env, "CLIENTPLATFORM_ENVIRONMENT").lower() != "production":
        errors.append("CLIENTPLATFORM_ENVIRONMENT must equal production")
    if _value(env, "APP_ENV").lower() not in {"prod", "production"}:
        errors.append("APP_ENV must be prod")
    _require(env, "BOT_TOKEN", errors)
    _validate_admin_ids(env, errors)
    username = _require(
        env, "CLIENTPLATFORM_PRODUCTION_BOT_USERNAME", errors
    ).lower().lstrip("@")
    if any(marker in username for marker in ("staging", "stage", "test")):
        errors.append("production bot username must not identify a staging/test bot")
    forbidden = sorted(
        name
        for name, value in env.items()
        if value
        and (
            name.startswith("CLIENTPLATFORM_STAGING_")
            or name.endswith("_STAGING_TOKEN")
        )
    )
    if forbidden:
        errors.append("staging-only secrets/configuration are present in production environment")


def _validate_database(env: Mapping[str, str], errors: list[str]) -> None:
    engine = _value(env, "CLIENTPLATFORM_DB_ENGINE")
    if engine.lower() not in {"postgres", "postgresql", "pg"}:
        errors.append("CLIENTPLATFORM_DB_ENGINE must be postgres")
    raw = _require(env, "DATABASE_URL", errors)
    if not raw:
        return
    parsed = urlsplit(raw)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        errors.append("DATABASE_URL must be a PostgreSQL URL with an explicit host")
        return
    database_name = parsed.path.lstrip("/")
    expected = _require(env, "CLIENTPLATFORM_DATABASE_NAME", errors)
    if not database_name or database_name != expected:
        errors.append("DATABASE_URL database must equal CLIENTPLATFORM_DATABASE_NAME")
    if not database_name.startswith("clientplatform"):
        errors.append("production database name must start with clientplatform")
    if _truthy(env, "ALLOW_SQLITE_IN_PROD"):
        errors.append("ALLOW_SQLITE_IN_PROD is forbidden")


def _validate_bind_hosts(env: Mapping[str, str], errors: list[str]) -> None:
    mode = _require(env, "CLIENTPLATFORM_DEPLOYMENT_MODE", errors).lower()
    if mode not in {"systemd", "container"}:
        errors.append("CLIENTPLATFORM_DEPLOYMENT_MODE must be systemd or container")
        return
    hosts = {
        "MESSENGER_WEBHOOK_HOST": _value(env, "MESSENGER_WEBHOOK_HOST"),
        "HEALTHCHECK_HOST": _value(env, "HEALTHCHECK_HOST"),
        "CLIENTPLATFORM_MEDIA_GATEWAY_HOST": _value(
            env, "CLIENTPLATFORM_MEDIA_GATEWAY_HOST"
        ),
    }
    if mode == "systemd":
        for name, host in hosts.items():
            if host not in {"127.0.0.1", "::1"}:
                errors.append(f"{name} must bind to loopback in systemd mode")
        if _truthy(env, "CLIENTPLATFORM_CONTAINER_NETWORK_ISOLATED"):
            errors.append("container isolation evidence is invalid in systemd mode")
        return
    if not _truthy(env, "CLIENTPLATFORM_CONTAINER_NETWORK_ISOLATED"):
        errors.append("container mode requires CLIENTPLATFORM_CONTAINER_NETWORK_ISOLATED=1")
    for name, host in hosts.items():
        if host not in {"0.0.0.0", "::"}:
            errors.append(f"{name} must bind inside the container network in container mode")


def _validate_telegram_polling(env: Mapping[str, str], errors: list[str]) -> None:
    if _value(env, "TELEGRAM_TRANSPORT").lower() != "polling":
        errors.append("TELEGRAM_TRANSPORT must be polling")
    run_mode = _value(env, "RUN_MODE").lower()
    if run_mode and run_mode != "polling":
        errors.append("RUN_MODE must be polling when configured")
    if _truthy(env, "TELEGRAM_WEBHOOK_ENABLED"):
        errors.append("TELEGRAM_WEBHOOK_ENABLED must be 0")
    if _truthy(env, "TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED"):
        errors.append("token-bearing Telegram webhook paths are forbidden")
    if _truthy(env, "ALLOW_INSECURE_TELEGRAM_WEBHOOK"):
        errors.append("ALLOW_INSECURE_TELEGRAM_WEBHOOK is forbidden")
    forbidden_values = {
        name: _value(env, name)
        for name in (
            "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL",
            "TELEGRAM_WEBHOOK_SECRET_TOKEN",
            "TELEGRAM_WEBHOOK_PREFIX",
        )
    }
    for name, value in forbidden_values.items():
        if value:
            errors.append(f"{name} must be empty for polling-only Telegram")


def _validate_public_ingress(env: Mapping[str, str], errors: list[str]) -> None:
    domain = _require(env, "CLIENTPLATFORM_DOMAIN", errors).lower()
    if domain in {"example.com", "localhost"} or domain.endswith(
        (".example.com", ".invalid")
    ):
        errors.append("CLIENTPLATFORM_DOMAIN must be a real dedicated production domain")
    public_base = _https_url(env, "CLIENTPLATFORM_PUBLIC_BASE_URL", errors)
    if public_base and urlsplit(public_base).hostname != domain:
        errors.append("CLIENTPLATFORM_PUBLIC_BASE_URL host must equal CLIENTPLATFORM_DOMAIN")

    for name in (
        "MESSENGER_PUBLIC_BASE_URL",
        "PRIVACY_EXPORT_PUBLIC_BASE_URL",
        "CLIENTPLATFORM_MEDIA_GATEWAY_BASE_URL",
    ):
        value = _https_url(env, name, errors)
        if (
            public_base
            and value
            and urlsplit(value).hostname != urlsplit(public_base).hostname
        ):
            errors.append(f"{name} must use the dedicated ClientPlatform domain")

    _validate_telegram_polling(env, errors)
    if not _truthy(env, "MESSENGER_WEBHOOK_ENABLED"):
        errors.append("MESSENGER_WEBHOOK_ENABLED must be 1")

    diagnostics_secret = _require(env, "HEALTHCHECK_DIAGNOSTICS_TOKEN", errors)
    if diagnostics_secret and len(diagnostics_secret) < 32:
        errors.append("HEALTHCHECK_DIAGNOSTICS_TOKEN must contain at least 32 characters")

    _validate_bind_hosts(env, errors)
    ingress_port = _positive_int(
        env, "MESSENGER_WEBHOOK_PORT", minimum=1024, maximum=65535, errors=errors
    )
    health_port = _positive_int(
        env, "HEALTHCHECK_PORT", minimum=1024, maximum=65535, errors=errors
    )
    media_port = _positive_int(
        env,
        "CLIENTPLATFORM_MEDIA_GATEWAY_PORT",
        minimum=1024,
        maximum=65535,
        errors=errors,
    )
    if len({ingress_port, health_port, media_port}) != 3:
        errors.append("ingress, health and media gateway ports must be distinct")
    if not _truthy(env, "HEALTHCHECK_ENABLED"):
        errors.append("HEALTHCHECK_ENABLED must be 1")


def _validate_storage(env: Mapping[str, str], errors: list[str]) -> None:
    if not _truthy(env, "CLIENTPLATFORM_MEDIA_GATEWAY_ENABLED"):
        errors.append("CLIENTPLATFORM_MEDIA_GATEWAY_ENABLED must be 1")
    if _value(env, "CLIENTPLATFORM_MEDIA_GATEWAY_STORAGE_MODE").lower() != "s3":
        errors.append("production media storage mode must be s3")
    _https_url(env, "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT", errors)
    _require(env, "CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION", errors)

    bucket = _require(env, "CLIENTPLATFORM_STORAGE_BUCKET", errors).lower()
    allowed = [
        item.strip().lower()
        for item in _value(env, "CLIENTPLATFORM_MEDIA_GATEWAY_ALLOWED_BUCKETS").split(",")
        if item.strip()
    ]
    if len(allowed) != 1 or allowed[0] != bucket:
        errors.append("media gateway allowlist must contain only CLIENTPLATFORM_STORAGE_BUCKET")
    if not bucket.startswith("clientplatform-") or "staging" in bucket:
        errors.append("production storage bucket must be dedicated and start with clientplatform-")

    expected_references = {
        "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ACCESS_KEY_REFERENCE": (
            "secret://env/CLIENTPLATFORM_SECRET_S3_ACCESS_KEY"
        ),
        "CLIENTPLATFORM_MEDIA_GATEWAY_S3_SECRET_KEY_REFERENCE": (
            "secret://env/CLIENTPLATFORM_SECRET_S3_SECRET_KEY"
        ),
        "CLIENTPLATFORM_MEDIA_SIGNING_SECRET_REFERENCE": (
            "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
        ),
    }
    for name, expected in expected_references.items():
        if _value(env, name) != expected:
            errors.append(f"{name} must use the dedicated ClientPlatform secret reference")
    for name in (
        "CLIENTPLATFORM_SECRET_S3_ACCESS_KEY",
        "CLIENTPLATFORM_SECRET_S3_SECRET_KEY",
        "CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY",
    ):
        _require(env, name, errors)
    if not _truthy(env, "CLIENTPLATFORM_S3_VERSIONING_ENABLED"):
        errors.append("CLIENTPLATFORM_S3_VERSIONING_ENABLED evidence must be 1")
    if not _truthy(env, "CLIENTPLATFORM_S3_BACKUP_REPLICATION_ENABLED"):
        errors.append("CLIENTPLATFORM_S3_BACKUP_REPLICATION_ENABLED evidence must be 1")


def _validate_runtime_paths(env: Mapping[str, str], errors: list[str]) -> None:
    runtime_root = _absolute_path(env, "CLIENTPLATFORM_RUNTIME_ROOT", errors)
    writable_root = _absolute_path(env, "CLIENTPLATFORM_WRITABLE_ROOT", errors)
    data_dir = _absolute_path(env, "CLIENTPLATFORM_DATA_DIR", errors)
    logs_dir = _absolute_path(env, "CLIENTPLATFORM_LOGS_DIR", errors)
    mpl_dir = _absolute_path(env, "MPLCONFIGDIR", errors)
    values = {
        "CLIENTPLATFORM_RUNTIME_ROOT": runtime_root,
        "CLIENTPLATFORM_WRITABLE_ROOT": writable_root,
        "CLIENTPLATFORM_DATA_DIR": data_dir,
        "CLIENTPLATFORM_LOGS_DIR": logs_dir,
        "MPLCONFIGDIR": mpl_dir,
    }
    expected_runtime = Path("/var/lib/clientplatform/runtime")
    expected_state = Path("/var/lib/clientplatform/state")
    expected_logs = Path("/var/log/clientplatform")
    if runtime_root is not None and runtime_root != expected_runtime:
        errors.append("CLIENTPLATFORM_RUNTIME_ROOT must equal /var/lib/clientplatform/runtime")
    if writable_root is not None and writable_root != expected_state:
        errors.append("CLIENTPLATFORM_WRITABLE_ROOT must equal /var/lib/clientplatform/state")
    if logs_dir is not None and logs_dir != expected_logs:
        errors.append("CLIENTPLATFORM_LOGS_DIR must equal /var/log/clientplatform")
    for name, path in (
        ("CLIENTPLATFORM_DATA_DIR", data_dir),
        ("MPLCONFIGDIR", mpl_dir),
    ):
        if path is not None and not path.is_relative_to(expected_state):
            errors.append(f"{name} must stay under /var/lib/clientplatform/state")


def _validate_backup_contract(env: Mapping[str, str], errors: list[str]) -> None:
    _absolute_path(env, "CLIENTPLATFORM_BACKUP_DIR", errors)
    _absolute_path(env, "CLIENTPLATFORM_RESTORE_EVIDENCE_DIR", errors)
    _positive_int(
        env,
        "CLIENTPLATFORM_BACKUP_RETENTION_DAYS",
        minimum=7,
        maximum=365,
        errors=errors,
    )
    if not _truthy(env, "CLIENTPLATFORM_BACKUP_ENCRYPTION_REQUIRED"):
        errors.append("CLIENTPLATFORM_BACKUP_ENCRYPTION_REQUIRED must be 1")
    if not _truthy(env, "CLIENTPLATFORM_RESTORE_DRILL_REQUIRED"):
        errors.append("CLIENTPLATFORM_RESTORE_DRILL_REQUIRED must be 1")


def validate_environment(env: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    _validate_identity_and_secrets(env, errors)
    _validate_database(env, errors)
    _validate_public_ingress(env, errors)
    _validate_storage(env, errors)
    _validate_runtime_paths(env, errors)
    _validate_backup_contract(env, errors)
    return errors


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"invalid environment line {line_number}")
        values[key.strip()] = value.strip()
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    env = dict(os.environ)
    if args.env_file is not None:
        env.update(_read_env_file(args.env_file))
    errors = validate_environment(env)
    payload = {
        "ok": not errors,
        "deployment_id": _value(env, "CLIENTPLATFORM_DEPLOYMENT_ID"),
        "environment": _value(env, "CLIENTPLATFORM_ENVIRONMENT"),
        "errors": errors,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    elif errors:
        for error in errors:
            print(f"CLIENTPLATFORM_PRODUCTION_PREFLIGHT_ERROR: {error}")
    else:
        print("CLIENTPLATFORM_PRODUCTION_PREFLIGHT_OK")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
