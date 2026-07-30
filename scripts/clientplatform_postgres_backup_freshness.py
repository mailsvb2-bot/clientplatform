from __future__ import annotations

"""Validate the age and integrity of the latest offsite PostgreSQL backup evidence."""

import argparse
import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

_TRUE = frozenset({"1", "true", "yes", "on"})
_DEFAULT_MAX_AGE_SECONDS = 3 * 60 * 60
_CLOCK_SKEW_SECONDS = 5 * 60
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_SUFFIXES = (
    ".dump.age",
    ".dump.age.sha256",
    ".dump.age.json",
)


def _value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def _truthy(env: Mapping[str, str], name: str) -> bool:
    return _value(env, name).lower() in _TRUE


def _max_age_seconds(env: Mapping[str, str]) -> int:
    raw = _value(env, "CLIENTPLATFORM_POSTGRES_BACKUP_MAX_AGE_SECONDS")
    try:
        value = int(raw or _DEFAULT_MAX_AGE_SECONDS)
    except ValueError:
        raise ValueError(
            "CLIENTPLATFORM_POSTGRES_BACKUP_MAX_AGE_SECONDS must be an integer"
        ) from None
    if value < 60 * 60 or value > 7 * 24 * 60 * 60:
        raise ValueError(
            "CLIENTPLATFORM_POSTGRES_BACKUP_MAX_AGE_SECONDS must be between 3600 and 604800"
        )
    return value


def _evidence_path(env: Mapping[str, str]) -> Path:
    raw = _value(env, "CLIENTPLATFORM_POSTGRES_BACKUP_S3_EVIDENCE_DIR")
    selected = Path(
        raw or "/var/lib/clientplatform/postgres-backup-s3-evidence"
    ).expanduser()
    if not selected.is_absolute():
        raise ValueError(
            "CLIENTPLATFORM_POSTGRES_BACKUP_S3_EVIDENCE_DIR must be absolute"
        )
    return selected.resolve() / "latest.json"


def _parse_completed_at(value: object) -> datetime:
    rendered = str(value or "").strip()
    if rendered.endswith("Z"):
        rendered = rendered[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError:
        raise ValueError("offsite backup evidence has invalid completed_at") from None
    if parsed.tzinfo is None:
        raise ValueError("offsite backup evidence completed_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _validate_objects(value: object, errors: list[str]) -> None:
    if not isinstance(value, list) or len(value) != len(_REQUIRED_SUFFIXES):
        errors.append("offsite backup evidence must contain three bundle objects")
        return

    keys: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            errors.append("offsite backup evidence contains an invalid object record")
            continue
        key = str(item.get("key") or "")
        size = item.get("size")
        sha256 = str(item.get("sha256") or "").lower()
        if not key or "\x00" in key:
            errors.append("offsite backup evidence contains an invalid object key")
        else:
            keys.append(key)
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            errors.append("offsite backup evidence contains an invalid object size")
        if _SHA256_RE.fullmatch(sha256) is None:
            errors.append("offsite backup evidence contains an invalid object SHA-256")

    if len(keys) == len(_REQUIRED_SUFFIXES):
        for suffix in _REQUIRED_SUFFIXES:
            if sum(key.endswith(suffix) for key in keys) != 1:
                errors.append(
                    "offsite backup evidence must contain one ciphertext, checksum, and metadata object"
                )
                break


def _inspect_evidence(
    env: Mapping[str, str],
    *,
    now: float,
    payload: dict[str, object],
    errors: list[str],
) -> None:
    max_age = _max_age_seconds(env)
    evidence = _evidence_path(env)
    payload["max_age_seconds"] = max_age
    payload["evidence_file"] = str(evidence)
    if evidence.is_symlink() or not evidence.is_file():
        errors.append("offsite backup evidence latest.json is missing or not a regular file")
        return

    mode = stat.S_IMODE(evidence.stat().st_mode)
    if mode & 0o077:
        errors.append("offsite backup evidence must not be group/world accessible")
    document = json.loads(evidence.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        errors.append("offsite backup evidence must be a JSON object")
        return
    if document.get("ok") is not True:
        errors.append("offsite backup evidence is not successful")
    if document.get("operation") != "postgres_backup_s3_upload":
        errors.append("offsite backup evidence operation is invalid")
    if document.get("encryption") != "age-x25519":
        errors.append("offsite backup evidence is not age encrypted")
    _validate_objects(document.get("objects"), errors)

    completed_at = _parse_completed_at(document.get("completed_at"))
    current = datetime.fromtimestamp(now, tz=timezone.utc)
    age_seconds = (current - completed_at).total_seconds()
    payload["completed_at"] = completed_at.isoformat().replace("+00:00", "Z")
    payload["age_seconds"] = round(age_seconds, 3)
    if age_seconds < -_CLOCK_SKEW_SECONDS:
        errors.append("offsite backup evidence timestamp is in the future")
    elif age_seconds > max_age:
        errors.append("offsite PostgreSQL backup is stale")


def evaluate_freshness(
    env: Mapping[str, str] | None = None,
    *,
    now: float | None = None,
) -> dict[str, object]:
    values = os.environ if env is None else env
    required = _truthy(values, "CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_REQUIRED")
    enabled = _truthy(values, "CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED")
    payload: dict[str, object] = {
        "schema_version": 1,
        "required": required,
        "offsite_enabled": enabled,
        "ok": True,
        "errors": [],
    }
    errors: list[str] = []
    if not required:
        return payload
    if not enabled:
        errors.append(
            "CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED must be enabled when freshness is required"
        )
        payload["ok"] = False
        payload["errors"] = errors
        return payload

    current_time = time.time() if now is None else now
    try:
        _inspect_evidence(values, now=current_time, payload=payload, errors=errors)
    except OSError as exc:
        errors.append(str(exc))
    except ValueError as exc:
        errors.append(str(exc))
    except TypeError as exc:
        errors.append(str(exc))

    payload["ok"] = not errors
    payload["errors"] = errors
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = evaluate_freshness()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    elif payload["ok"]:
        if payload["required"]:
            print("CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_OK")
        else:
            print("CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_DISABLED")
    else:
        for error in payload["errors"]:
            print(f"CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_ERROR:{error}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
