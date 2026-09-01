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


def _apply_env(loaded: dict[str, str]) -> None:
    for key, value in loaded.items():
        os.environ.setdefault(str(key), str(value))


def _load_project_storage_module(env_file: str | Path | None):
    _apply_env(_load_env_file(env_file))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from services import storage_legacy_audit as audit_module

    return audit_module


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit active DB storage and legacy SQLite ambiguity")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when the audit has hard findings")
    parser.add_argument("--env-file", default=os.getenv("CLIENTPLATFORM_ENV_FILE", str(DEFAULT_ENV_FILE)))
    args = parser.parse_args()

    audit_module = _load_project_storage_module(args.env_file)
    audit = audit_module.storage_legacy_audit()
    if args.json:
        print(json.dumps(audit.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(audit_module.format_storage_legacy_audit_for_admin())

    if args.strict and audit.hard_failures:
        print("STORAGE_LEGACY_AUDIT_NOT_OK hard_findings=" + ",".join(audit.hard_failures), file=sys.stderr)
        return 1
    if not args.json:
        print("STORAGE_LEGACY_AUDIT_OK status=" + audit.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
