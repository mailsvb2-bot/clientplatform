from __future__ import annotations

"""Fail-closed environment contract for managed Telegram bot polling."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.clientplatform_bot_gateway_contract import validate_environment
from clientplatform.infrastructure.managed_bot_credentials import (
    AgeManagedBotCredentialVault,
    ManagedBotCredentialError,
)

_TRUE = frozenset({"1", "true", "yes", "on", "webhook"})


def _value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def _truthy(env: Mapping[str, str], name: str) -> bool:
    return _value(env, name).lower() in _TRUE


def _validate_managed_bot_identity(
    env: Mapping[str, str],
    errors: list[str],
) -> None:
    if not _truthy(
        env,
        "CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED",
    ):
        return
    identity = _value(
        env,
        "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE",
    )
    if not identity or not Path(identity).is_absolute():
        return
    marker = "clientplatform-managed-bot-preflight"
    try:
        vault = AgeManagedBotCredentialVault(identity)
        ciphertext = vault.seal(marker)
        if vault.open(ciphertext) != marker:
            raise ManagedBotCredentialError(
                "managed bot credential round trip failed"
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
