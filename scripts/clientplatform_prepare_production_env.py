from __future__ import annotations

"""Prepare the dedicated ClientPlatform container environment without exposing secrets."""

import argparse
import os
import re
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_AD_IDENTITY_FILE = "/run/secrets/clientplatform-ad/identity.txt"
_AD_HOST_DIR = "/var/lib/clientplatform/ad-secrets"
_AD_OAUTH_REDIRECT_URI = "https://oauth.yandex.ru/verification_code"
_MANAGED_BOT_IDENTITY_FILE = "/run/secrets/clientplatform-managed-bot/identity.txt"
_MANAGED_BOT_HOST_DIR = "/var/lib/clientplatform/managed-bot-secrets"
_MAX_API2_BASE_URL = "https://platform-api2.max.ru"
_TELEGRAM_STARS_DEFAULTS = {
    "TELEGRAM_STARS_PRICING_MODE": "explicit",
    "TELEGRAM_STARS_PRICE_PRACTICE_START_7": "1500",
    "TELEGRAM_STARS_PRICE_PRACTICE_60": "2500",
    "TELEGRAM_STARS_PRICE_PRACTICE_ANTISTRESS_60": "5000",
    "TELEGRAM_STARS_PRICE_PRACTICE_PERSONAL_MONTH": "15000",
}


class EnvironmentPreparationError(RuntimeError):
    """Sanitized operator-facing environment preparation failure."""


def _parse(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if _KEY_RE.fullmatch(key):
            values[key] = value.strip()
    return lines, values


def _required(values: dict[str, str], name: str) -> str:
    value = str(values.get(name, "") or "").strip()
    if not value or value.lower().startswith("change"):
        raise EnvironmentPreparationError(f"missing_{name.lower()}")
    return value


def _exact_or_missing(values: dict[str, str], name: str, expected: str) -> None:
    observed = str(values.get(name, "") or "").strip()
    if observed and observed != expected:
        raise EnvironmentPreparationError(f"mismatched_{name.lower()}")


def _enabled(values: dict[str, str], name: str) -> bool:
    return str(values.get(name, "") or "").strip().lower() in _TRUE_VALUES


def _validate_timezone(values: dict[str, str]) -> None:
    configured = _required(
        values,
        "CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE",
    )
    try:
        ZoneInfo(configured)
    except ZoneInfoNotFoundError as exc:
        raise EnvironmentPreparationError(
            "invalid_clientplatform_yandex_direct_report_timezone"
        ) from exc


def _validate_ad_connections(
    values: dict[str, str],
    *,
    domain: str | None = None,
) -> None:
    del domain  # retained for backward-compatible callers and tests
    connections_enabled = _enabled(
        values,
        "CLIENTPLATFORM_AD_CONNECTIONS_ENABLED",
    )
    mutations_enabled = _enabled(
        values,
        "CLIENTPLATFORM_AD_SPEND_MUTATIONS_ENABLED",
    )
    if mutations_enabled and not connections_enabled:
        raise EnvironmentPreparationError(
            "ad_spend_mutations_require_ad_connections"
        )
    if not connections_enabled:
        return
    _required(values, "CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID")
    _required(values, "CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET")
    observed_redirect = _required(values, "CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI")
    if observed_redirect != _AD_OAUTH_REDIRECT_URI:
        raise EnvironmentPreparationError(
            "mismatched_clientplatform_ad_oauth_redirect_uri"
        )
    if _required(values, "CLIENTPLATFORM_AD_CREDENTIAL_IDENTITY_FILE") != _AD_IDENTITY_FILE:
        raise EnvironmentPreparationError(
            "mismatched_clientplatform_ad_credential_identity_file"
        )
    if _required(values, "CLIENTPLATFORM_AD_CREDENTIAL_HOST_DIR") != _AD_HOST_DIR:
        raise EnvironmentPreparationError(
            "mismatched_clientplatform_ad_credential_host_dir"
        )
    _validate_timezone(values)


def _validate_managed_bot_auto_provisioning(values: dict[str, str]) -> None:
    if not _enabled(values, "CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED"):
        return
    if _required(
        values,
        "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE",
    ) != _MANAGED_BOT_IDENTITY_FILE:
        raise EnvironmentPreparationError(
            "mismatched_clientplatform_managed_bot_credential_identity_file"
        )
    if _required(
        values,
        "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_HOST_DIR",
    ) != _MANAGED_BOT_HOST_DIR:
        raise EnvironmentPreparationError(
            "mismatched_clientplatform_managed_bot_credential_host_dir"
        )
    if _enabled(values, "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_ALLOW_GENERATE"):
        raise EnvironmentPreparationError(
            "managed_bot_credential_generation_forbidden_in_production"
        )


def prepare(path: Path) -> tuple[str, ...]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise EnvironmentPreparationError("production_env_must_be_regular_file")
    resolved = expanded.resolve()
    if not resolved.is_file():
        raise EnvironmentPreparationError("production_env_must_be_regular_file")
    mode = resolved.stat().st_mode & 0o777
    if mode & 0o077:
        raise EnvironmentPreparationError("production_env_permissions_must_be_0600")

    lines, values = _parse(resolved)
    domain = _required(values, "CLIENTPLATFORM_DOMAIN")
    bucket = _required(values, "CLIENTPLATFORM_STORAGE_BUCKET")
    _required(values, "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT")
    _required(values, "CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION")
    _required(values, "CLIENTPLATFORM_SECRET_S3_ACCESS_KEY")
    _required(values, "CLIENTPLATFORM_SECRET_S3_SECRET_KEY")

    expected_public = f"https://{domain}"
    expected_media = f"https://{domain}/clientplatform"
    _exact_or_missing(values, "CLIENTPLATFORM_PUBLIC_BASE_URL", expected_public)
    _exact_or_missing(values, "CLIENTPLATFORM_MEDIA_GATEWAY_BASE_URL", expected_media)
    _exact_or_missing(values, "CLIENTPLATFORM_MEDIA_GATEWAY_ALLOWED_BUCKETS", bucket)
    _exact_or_missing(values, "CLIENTPLATFORM_MEDIA_GATEWAY_STORAGE_MODE", "s3")
    _exact_or_missing(values, "CLIENTPLATFORM_REQUIRE_AUDIO_ASSETS", "1")

    defaults = {
        "CLIENTPLATFORM_PUBLIC_BASE_URL": expected_public,
        "CLIENTPLATFORM_MEDIA_GATEWAY_ENABLED": "1",
        "CLIENTPLATFORM_MEDIA_GATEWAY_BASE_URL": expected_media,
        "CLIENTPLATFORM_MEDIA_GATEWAY_STORAGE_MODE": "s3",
        "CLIENTPLATFORM_MEDIA_GATEWAY_ALLOWED_BUCKETS": bucket,
        "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ACCESS_KEY_REFERENCE": (
            "secret://env/CLIENTPLATFORM_SECRET_S3_ACCESS_KEY"
        ),
        "CLIENTPLATFORM_MEDIA_GATEWAY_S3_SECRET_KEY_REFERENCE": (
            "secret://env/CLIENTPLATFORM_SECRET_S3_SECRET_KEY"
        ),
        "CLIENTPLATFORM_MEDIA_SIGNING_SECRET_REFERENCE": (
            "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
        ),
        "CLIENTPLATFORM_PROGRAM_MEDIA_INGEST_ENABLED": "1",
        "CLIENTPLATFORM_PROGRAM_MEDIA_MAX_BYTES": "20000000",
        "CLIENTPLATFORM_PROGRAM_MEDIA_TIMEOUT_SEC": "30",
        "CLIENTPLATFORM_REQUIRE_AUDIO_ASSETS": "1",
        "CLIENTPLATFORM_POSTGRES_CLIENT_MAJOR": "16",
        "CLIENTPLATFORM_BACKUP_DIR": "/var/backups/clientplatform/postgres",
        "CLIENTPLATFORM_BACKUP_RETENTION_DAYS": "30",
        "CLIENTPLATFORM_BACKUP_ENCRYPTION_REQUIRED": "1",
        "CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED": "0",
        "CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_REQUIRED": "0",
        "CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED": "0",
        "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE": (
            _MANAGED_BOT_IDENTITY_FILE
        ),
        "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_HOST_DIR": _MANAGED_BOT_HOST_DIR,
        "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_ALLOW_GENERATE": "0",
        "MAX_WEBHOOK_ENABLED": "0",
        "MAX_API_BASE_URL": _MAX_API2_BASE_URL,
        "VK_WEBHOOK_ENABLED": "0",
        "TELEGRAM_YOOKASSA_ENABLED": "0",
        "CLIENTPLATFORM_AD_CONNECTIONS_ENABLED": "0",
        "CLIENTPLATFORM_AD_SPEND_MUTATIONS_ENABLED": "0",
        "CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI": _AD_OAUTH_REDIRECT_URI,
        "CLIENTPLATFORM_AD_CREDENTIAL_IDENTITY_FILE": _AD_IDENTITY_FILE,
        "CLIENTPLATFORM_AD_CREDENTIAL_HOST_DIR": _AD_HOST_DIR,
        "CLIENTPLATFORM_AD_PUBLICATION_INTERVAL_SEC": "2",
        "CLIENTPLATFORM_AD_SPEND_GUARD_INTERVAL_SEC": "5",
        **_TELEGRAM_STARS_DEFAULTS,
    }
    if not str(values.get("CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY", "")).strip():
        defaults["CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"] = secrets.token_urlsafe(48)

    added: list[str] = []
    for key, value in defaults.items():
        if str(values.get(key, "")).strip():
            continue
        added.append(key)
        lines.append(f"{key}={value}")
        values[key] = value

    _validate_managed_bot_auto_provisioning(values)
    _validate_ad_connections(values, domain=domain)

    backup = resolved.with_name(resolved.name + ".before-current-main")
    if added:
        backup.write_bytes(resolved.read_bytes())
        os.chmod(backup, 0o600)
        payload = "\n".join(lines).rstrip() + "\n"
        temporary = resolved.with_name(resolved.name + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, resolved)
        os.chmod(resolved, 0o600)
    return tuple(added)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", type=Path)
    args = parser.parse_args()
    added = prepare(args.env_file)
    print(f"CLIENTPLATFORM_PRODUCTION_ENV_OK:added={len(added)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
