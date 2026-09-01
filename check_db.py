from __future__ import annotations

"""Read-only storage diagnostic for operators and local troubleshooting.

Without ``--sqlite-path`` the command reports the canonical active storage audit,
including Postgres readiness and legacy SQLite ambiguity. A specific SQLite file
is inspected only when explicitly requested and is always opened read-only.
"""

import argparse
import json
import os
import shlex
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = Path("/etc/clientplatform/clientplatform.env")


def _load_env_file(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    loaded: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
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


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = _resolved(path).as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10, check_same_thread=False)


def inspect_sqlite(path: Path) -> dict[str, Any]:
    resolved = _resolved(path)
    if not resolved.is_file():
        return {
            "ok": False,
            "mode": "sqlite_file",
            "path": str(resolved),
            "integrity_ok": False,
            "table_count": 0,
            "tables": [],
            "selected_plan_present": False,
            "error_code": "sqlite_file_not_found",
        }

    try:
        with closing(_readonly_connection(resolved)) as conn:
            integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = bool(
                integrity_row and str(integrity_row[0]).strip().casefold() == "ok"
            )
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
    except sqlite3.Error:
        return {
            "ok": False,
            "mode": "sqlite_file",
            "path": str(resolved),
            "integrity_ok": False,
            "table_count": 0,
            "tables": [],
            "selected_plan_present": False,
            "error_code": "sqlite_read_failed:SQLiteError",
        }
    except OSError:
        return {
            "ok": False,
            "mode": "sqlite_file",
            "path": str(resolved),
            "integrity_ok": False,
            "table_count": 0,
            "tables": [],
            "selected_plan_present": False,
            "error_code": "sqlite_read_failed:OSError",
        }

    tables = [str(row[0]) for row in rows if row and row[0] is not None]
    return {
        "ok": integrity_ok,
        "mode": "sqlite_file",
        "path": str(resolved),
        "size_bytes": int(resolved.stat().st_size),
        "integrity_ok": integrity_ok,
        "table_count": len(tables),
        "tables": tables[:500],
        "tables_truncated": len(tables) > 500,
        "selected_plan_present": "selected_plan" in tables,
        "error_code": "" if integrity_ok else "sqlite_integrity_check_failed",
    }


def active_storage_report() -> dict[str, Any]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from services.storage_legacy_audit import storage_legacy_audit

    audit = storage_legacy_audit()
    payload = audit.to_dict()
    payload["mode"] = "active_storage"
    return payload


def _print_human(payload: dict[str, Any]) -> None:
    mode = str(payload.get("mode") or "unknown")
    if mode == "active_storage":
        print(
            "STORAGE_DIAGNOSTIC "
            f"status={payload.get('status')} engine={payload.get('active_engine')} "
            f"ok={payload.get('ok')} target={payload.get('db_target')}"
        )
        print(
            "legacy_sqlite_present="
            f"{payload.get('legacy_sqlite_present')} "
            "repo_local_sqlite_present="
            f"{payload.get('repo_local_sqlite_present')} "
            "disallowed_direct_sqlite_connects="
            f"{len(payload.get('disallowed_direct_sqlite_connects') or [])}"
        )
    else:
        print(
            "SQLITE_DIAGNOSTIC "
            f"ok={payload.get('ok')} integrity_ok={payload.get('integrity_ok')} "
            f"tables={payload.get('table_count')} path={payload.get('path')}"
        )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only ClientPlatform storage diagnostic")
    parser.add_argument(
        "--env-file",
        default=os.getenv("CLIENTPLATFORM_ENV_FILE", str(DEFAULT_ENV_FILE)),
    )
    parser.add_argument(
        "--sqlite-path",
        default="",
        help="Explicit SQLite file to inspect read-only instead of active storage",
    )
    parser.add_argument("--strict", action="store_true", help="Return nonzero when the diagnostic is not OK")
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    args = parser.parse_args(argv)

    env_file = Path(args.env_file) if str(args.env_file or "").strip() else None
    _apply_env(_load_env_file(env_file))

    if str(args.sqlite_path or "").strip():
        payload = inspect_sqlite(Path(str(args.sqlite_path)))
    else:
        payload = active_storage_report()

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        _print_human(payload)
    if args.strict and payload.get("ok") is not True:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
