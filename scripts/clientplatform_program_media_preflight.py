from __future__ import annotations

"""Fail-closed startup contract for bot-independent ClientPlatform lesson media."""

import argparse
import json
import os
import re
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

_TRUE = frozenset({"1", "true", "yes", "on"})
_PLACEHOLDERS = frozenset({"", "changeme", "change-me", "secret", "password", "token"})
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def _value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def _truthy(env: Mapping[str, str], name: str) -> bool:
    return _value(env, name).lower() in _TRUE


def _placeholder(value: str) -> bool:
    normalized = str(value or "").strip()
    lowered = normalized.lower()
    return (
        lowered in _PLACEHOLDERS
        or normalized.upper().startswith(("PASTE_", "CHANGE_"))
        or "your-provider" in lowered
        or "your-domain" in lowered
    )


def _require(env: Mapping[str, str], name: str, errors: list[str]) -> str:
    value = _value(env, name)
    if _placeholder(value):
        errors.append(f"{name} is missing or placeholder")
    return value


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
        value = int(raw)
    except ValueError:
        errors.append(f"{name} must be an integer")
        return minimum
    if value < minimum or value > maximum:
        errors.append(f"{name} must be between {minimum} and {maximum}")
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
    return value


def validate_environment(env: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    production = (
        _value(env, "APP_ENV").lower() in {"prod", "production"}
        or _value(env, "CLIENTPLATFORM_ENVIRONMENT").lower() == "production"
    )
    if not production:
        return errors
    if not _truthy(env, "CLIENTPLATFORM_CONTROL_BOT_ENABLED"):
        return errors

    if not _truthy(env, "CLIENTPLATFORM_PROGRAM_MEDIA_INGEST_ENABLED"):
        errors.append("CLIENTPLATFORM_PROGRAM_MEDIA_INGEST_ENABLED must be 1")
    if not _truthy(env, "CLIENTPLATFORM_MEDIA_GATEWAY_ENABLED"):
        errors.append("CLIENTPLATFORM_MEDIA_GATEWAY_ENABLED must be 1")
    if _value(env, "CLIENTPLATFORM_MEDIA_GATEWAY_STORAGE_MODE").lower() != "s3":
        errors.append("CLIENTPLATFORM_MEDIA_GATEWAY_STORAGE_MODE must be s3")

    _https_url(env, "CLIENTPLATFORM_MEDIA_GATEWAY_BASE_URL", errors)
    _https_url(env, "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT", errors)
    _require(env, "CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION", errors)
    bucket = _require(env, "CLIENTPLATFORM_STORAGE_BUCKET", errors).lower()
    if bucket and (
        not _BUCKET_RE.fullmatch(bucket)
        or not bucket.startswith("clientplatform-")
        or "staging" in bucket
    ):
        errors.append("CLIENTPLATFORM_STORAGE_BUCKET must be dedicated to production")
    allowed = [
        item.strip().lower()
        for item in _value(env, "CLIENTPLATFORM_MEDIA_GATEWAY_ALLOWED_BUCKETS").split(",")
        if item.strip()
    ]
    if bucket and allowed != [bucket]:
        errors.append("media gateway allowlist must contain only CLIENTPLATFORM_STORAGE_BUCKET")

    for name in (
        "CLIENTPLATFORM_SECRET_S3_ACCESS_KEY",
        "CLIENTPLATFORM_SECRET_S3_SECRET_KEY",
        "CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY",
    ):
        _require(env, name, errors)

    _positive_int(
        env,
        "CLIENTPLATFORM_PROGRAM_MEDIA_MAX_BYTES",
        minimum=1,
        maximum=20_000_000,
        errors=errors,
    )
    _positive_int(
        env,
        "CLIENTPLATFORM_PROGRAM_MEDIA_TIMEOUT_SEC",
        minimum=1,
        maximum=120,
        errors=errors,
    )
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
    payload = {"ok": not errors, "errors": errors}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    elif errors:
        for error in errors:
            print(f"CLIENTPLATFORM_PROGRAM_MEDIA_PREFLIGHT_ERROR: {error}")
    else:
        print("CLIENTPLATFORM_PROGRAM_MEDIA_PREFLIGHT_OK")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
