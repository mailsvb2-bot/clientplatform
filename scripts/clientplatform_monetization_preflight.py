from __future__ import annotations

"""Fail closed before production startup when paid-delivery guardrails are incomplete."""

import argparse
import json
import os
from pathlib import Path
from typing import Mapping

from core.payment_ingress import resolve_payment_http_enabled

_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})
_HARD_TOKEN_VALUES = frozenset({"hard", "1", "true", "yes", "on"})
_RECEIPT_EMAIL_KEYS = (
    "YOOKASSA_RECEIPT_EMAIL",
    "PAYMENT_RECEIPT_EMAIL",
    "ADMIN_EMAIL",
)


def _value(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name, default) or default).strip()


def _first_value(env: Mapping[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = _value(env, name)
        if value:
            return value
    return ""


def validate_environment(env: Mapping[str, str]) -> list[str]:
    """Mirror the startup monetization requirements without importing the app."""

    errors: list[str] = []
    token_economy = _value(env, "TOKEN_ECONOMY_ENABLED", "1").lower()
    token_mode = _value(env, "TOKEN_ENFORCEMENT_MODE").lower()

    if token_economy in _DISABLED_VALUES:
        errors.append("TOKEN_ECONOMY_ENABLED must not be disabled in prod")
    if token_mode not in _HARD_TOKEN_VALUES:
        errors.append("TOKEN_ENFORCEMENT_MODE must be hard in prod")
    payment_env = dict(env)
    payment_env.setdefault("APP_ENV", "prod")
    if resolve_payment_http_enabled(payment_env) and not _first_value(env, _RECEIPT_EMAIL_KEYS):
        errors.append(
            "YOOKASSA_RECEIPT_EMAIL or PAYMENT_RECEIPT_EMAIL or ADMIN_EMAIL "
            "is required in prod"
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
            print(f"CLIENTPLATFORM_MONETIZATION_PREFLIGHT_ERROR: {error}")
    else:
        print("CLIENTPLATFORM_MONETIZATION_PREFLIGHT_OK")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
