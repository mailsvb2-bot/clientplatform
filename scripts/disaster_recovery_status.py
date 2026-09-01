from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = Path("/etc/clientplatform/clientplatform.env")


def _load_env_file(path: str | Path | None) -> dict[str, str]:
    if not path:
        return {}
    env_path = Path(path)
    if not env_path.exists():
        return {}
    loaded: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        try:
            parts = shlex.split(value, posix=True)
            loaded[key] = parts[0] if len(parts) == 1 else value
        except ValueError:
            loaded[key] = value.strip('"').strip("'")
    return loaded


def _apply_env(values: dict[str, str]) -> None:
    for key, value in values.items():
        os.environ.setdefault(str(key), str(value))


def main() -> int:
    parser = argparse.ArgumentParser(description="Show ClientPlatform backup/disaster-recovery status")
    parser.add_argument("--env-file", default=os.getenv("CLIENTPLATFORM_ENV_FILE", str(DEFAULT_ENV_FILE)))
    parser.add_argument("--include-hash", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when backup proof is not OK")
    args = parser.parse_args()

    _apply_env(_load_env_file(args.env_file))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from services.disaster_recovery_status import disaster_recovery_status, format_disaster_recovery_status_for_admin

    status = disaster_recovery_status(include_hash=bool(args.include_hash))
    if args.json:
        print(json.dumps(status.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(format_disaster_recovery_status_for_admin())
    return 2 if args.strict and not status.ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
