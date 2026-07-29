from __future__ import annotations

"""Fail-closed environment contract for the Managed Bot Gateway."""

import argparse
import json
import os
from pathlib import Path
from typing import Mapping

_TRUE = frozenset({"1", "true", "yes", "on"})


def _value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def _truthy(env: Mapping[str, str], name: str) -> bool:
    return _value(env, name).lower() in _TRUE


def _bounded_int(
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


def _bounded_float(
    env: Mapping[str, str],
    name: str,
    *,
    minimum: float,
    maximum: float,
    errors: list[str],
) -> float:
    raw = _value(env, name)
    try:
        value = float(raw)
    except ValueError:
        errors.append(f"{name} must be numeric")
        return minimum
    if value < minimum or value > maximum:
        errors.append(f"{name} must be between {minimum} and {maximum}")
    return value


def validate_environment(env: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    deployed = _value(env, "APP_ENV").lower() in {"prod", "production", "stage", "staging"}
    enabled = _truthy(env, "CLIENTPLATFORM_BOT_GATEWAY_ENABLED")
    if deployed and not enabled:
        errors.append("CLIENTPLATFORM_BOT_GATEWAY_ENABLED must be 1 in deployed environments")
    if enabled and not _truthy(env, "MESSENGER_WEBHOOK_ENABLED"):
        errors.append("Managed Bot Gateway requires MESSENGER_WEBHOOK_ENABLED=1")

    prefix = _value(env, "CLIENTPLATFORM_BOT_GATEWAY_PATH_PREFIX")
    if not prefix.startswith("/") or prefix.endswith("/"):
        errors.append("CLIENTPLATFORM_BOT_GATEWAY_PATH_PREFIX must be a normalized absolute path")
    lowered = prefix.lower()
    if any(marker in lowered for marker in ("token", "secret", "credential")):
        errors.append("Managed Bot Gateway path must not contain secret material")
    if "{" in prefix or "}" in prefix or "?" in prefix or "#" in prefix:
        errors.append("Managed Bot Gateway path prefix must be static")

    _bounded_int(
        env,
        "CLIENTPLATFORM_BOT_GATEWAY_BATCH_SIZE",
        minimum=1,
        maximum=100,
        errors=errors,
    )
    _bounded_float(
        env,
        "CLIENTPLATFORM_BOT_GATEWAY_INTERVAL_SEC",
        minimum=0.05,
        maximum=60.0,
        errors=errors,
    )
    _bounded_float(
        env,
        "CLIENTPLATFORM_BOT_GATEWAY_TICK_TIMEOUT_SEC",
        minimum=1.0,
        maximum=300.0,
        errors=errors,
    )
    lock_ttl = _bounded_int(
        env,
        "CLIENTPLATFORM_BOT_GATEWAY_LOCK_TTL_SEC",
        minimum=30,
        maximum=3600,
        errors=errors,
    )
    tick_timeout = _bounded_float(
        env,
        "CLIENTPLATFORM_BOT_GATEWAY_TICK_TIMEOUT_SEC",
        minimum=1.0,
        maximum=300.0,
        errors=[],
    )
    if lock_ttl <= tick_timeout:
        errors.append("Managed Bot Gateway lock TTL must exceed tick timeout")
    _bounded_int(
        env,
        "CLIENTPLATFORM_BOT_GATEWAY_MAX_ATTEMPTS",
        minimum=1,
        maximum=20,
        errors=errors,
    )
    _bounded_int(
        env,
        "CLIENTPLATFORM_BOT_GATEWAY_PER_BOT_PER_MINUTE",
        minimum=1,
        maximum=10_000,
        errors=errors,
    )
    _bounded_int(
        env,
        "CLIENTPLATFORM_BOT_GATEWAY_PER_BOT_QUEUE_LIMIT",
        minimum=1,
        maximum=100_000,
        errors=errors,
    )
    payload_limit = _bounded_int(
        env,
        "CLIENTPLATFORM_BOT_GATEWAY_MAX_PAYLOAD_BYTES",
        minimum=1024,
        maximum=1_048_576,
        errors=errors,
    )
    ingress_limit = _bounded_int(
        env,
        "HTTP_INGRESS_MAX_BODY_BYTES",
        minimum=1024,
        maximum=16_777_216,
        errors=errors,
    )
    if ingress_limit < payload_limit:
        errors.append("HTTP ingress body limit must cover the Managed Bot Gateway payload limit")
    if _truthy(env, "TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED"):
        errors.append("token-bearing legacy Telegram webhook paths are forbidden")
    return errors


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name.strip():
            raise ValueError("invalid environment file line")
        values[name.strip()] = value.strip()
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
    if args.json:
        print(json.dumps({"ok": not errors, "errors": errors}, sort_keys=True))
    if errors:
        for error in errors:
            print(f"CLIENTPLATFORM_BOT_GATEWAY_PREFLIGHT_FAILED:{error}")
        return 2
    print("CLIENTPLATFORM_BOT_GATEWAY_PREFLIGHT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
