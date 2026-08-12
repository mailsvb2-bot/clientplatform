from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clientplatform.runtime.sales_ai_config import SalesAIRuntimeConfig, sales_ai_secret_env_name


_PLACEHOLDERS = frozenset({"", "changeme", "change-me", "secret", "token", "password"})


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator:
            values[name.strip()] = value.strip()
    return values


def validate_sales_ai_environment(environment: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    try:
        config = SalesAIRuntimeConfig.from_env(environment)
    except ValueError as exc:
        return [str(exc)]
    if not config.enabled:
        return errors
    try:
        key_name = sales_ai_secret_env_name(config.api_key_reference)
    except ValueError as exc:
        return [str(exc)]
    secret = str(environment.get(key_name, "") or "").strip()
    if secret.lower() in _PLACEHOLDERS or secret.upper().startswith(("PASTE_", "CHANGE_")):
        errors.append(f"{key_name} is missing or placeholder")
    elif len(secret) < 20:
        errors.append(f"{key_name} is unexpectedly short")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate ClientPlatform Sales AI configuration")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    environment = dict(os.environ)
    if args.env_file is not None:
        environment.update(_load_env_file(args.env_file))
    errors = validate_sales_ai_environment(environment)
    if errors:
        for error in errors:
            print(f"CLIENTPLATFORM_SALES_AI_PREFLIGHT_ERROR:{error}")
        return 1
    print("CLIENTPLATFORM_SALES_AI_PREFLIGHT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
