from __future__ import annotations

"""Prepare the dedicated ClientPlatform container environment without exposing secrets."""

import argparse
import os
import re
import secrets
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


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
        "CLIENTPLATFORM_POSTGRES_CLIENT_MAJOR": "16",
        "CLIENTPLATFORM_BACKUP_DIR": "/var/backups/clientplatform/postgres",
        "CLIENTPLATFORM_BACKUP_RETENTION_DAYS": "30",
        "CLIENTPLATFORM_BACKUP_ENCRYPTION_REQUIRED": "1",
        "CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED": "0",
        "CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_REQUIRED": "0",
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
