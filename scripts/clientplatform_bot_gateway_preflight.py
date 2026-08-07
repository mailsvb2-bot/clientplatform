from __future__ import annotations

"""Fail-closed environment contract for managed Telegram bot polling."""

import argparse
import json
import os
from pathlib import Path
from typing import Mapping

from clientplatform.infrastructure.managed_bot_credentials import (
    AgeManagedBotCredentialVault,
    ManagedBotCredentialError,
)

_TRUE = frozenset({"1", "true", "yes", "on", "webhook"})


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

    auto_provisioning = _truthy(
        env,
        "CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED",
    )
    if auto_provisioning:
        if not enabled:
            errors.append(
                "CLIENTPLATFORM_BOT_GATEWAY_ENABLED must be 1 when managed bot auto provisioning is enabled"
            )
        identity = _value(
            env,
            "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE",
        )
        if not identity:
            errors.append(
                "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE is required when managed bot auto provisioning is enabled"
            )
        elif not Path(identity).is_absolute():
            errors.append(
                "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE must be an absolute path"
            )
        if deployed and _truthy(
            env,
            "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_ALLOW_GENERATE",
        ):
            errors.append(
                "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_ALLOW_GENERATE must be 0 in deployed environments"
            )

    telegram_transport = (_value(env, "TELEGRAM_TRANSPORT") or "polling").lower()
    if telegram_transport != "polling":
        errors.append("TELEGRAM_TRANSPORT must be polling")
    if _truthy(env, "TELEGRAM_WEBHOOK_ENABLED"):
        errors.append("TELEGRAM_WEBHOOK_ENABLED must be 0 for polling-only Telegram")
    if _truthy(env, "TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED"):
        errors.append("token-bearing legacy Telegram webhook paths are forbidden")

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
    tick_timeout = _bounded_float(
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
    _bounded_int(
        env,
        "CLIENTPLATFORM_BOT_GATEWAY_MAX_PAYLOAD_BYTES",
        minimum=1024,
        maximum=1_048_576,
        errors=errors,
    )
    _bounded_int(
        env,
        "CLIENTPLATFORM_BOT_GATEWAY_POLL_TIMEOUT_SEC",
        minimum=1,
        maximum=50,
        errors=errors,
    )
    _bounded_float(
        env,
        "CLIENTPLATFORM_BOT_GATEWAY_RECONCILE_INTERVAL_SEC",
        minimum=0.1,
        maximum=300.0,
        errors=errors,
    )
    return errors


def _validate_managed_bot_identity(
    env: Mapping[str, str],
    errors: list[str],
) -> None:
    if not _truthy(env, "CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED"):
        return
    identity = _value(env, "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE")
    if not identity or not Path(identity).is_absolute():
        return
    try:
        AgeManagedBotCredentialVault(identity).seal(
            "clientplatform-managed-bot-preflight"
        )
    except ManagedBotCredentialError:
        errors.append(
            "managed bot credential identity must be a private usable age identity"
        )


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
    _validate_managed_bot_identity(env, errors)
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
