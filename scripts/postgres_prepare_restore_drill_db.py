from __future__ import annotations

"""Prepare a non-production Postgres database for restore drills.

This operator creates a clearly named drill database owned by the production DB
user. It never creates or drops the production database and never runs CREATE
DATABASE inside a transaction/function block.

Expected use on the server:
    python scripts/postgres_prepare_restore_drill_db.py --json

It normally needs to run as root because it uses ``runuser -u postgres`` for the
single database-create operation. JSON output is redacted by default; use
``--print-export`` only inside shell command substitution when the full connection
URL is needed by the next process.
"""

import argparse
import json
import os
import re
import shlex
# Reviewed: operator restore-drill DB preparer invokes fixed runuser/psql/createdb commands without shell.
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

DEFAULT_ENV_FILE = Path("/etc/clientplatform/clientplatform.env")
FORBIDDEN_DB_NAMES = {"postgres", "template0", "template1", "clientplatform"}
SAFE_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class DrillDbPrepareResult:
    ok: bool
    target_db: str
    owner: str
    created: bool
    existed: bool
    target_url: str
    problems: list[str]

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "target_db": self.target_db,
            "owner": self.owner,
            "created": self.created,
            "existed": self.existed,
            "target_url_redacted": _redact_url(self.target_url),
            "problems": self.problems,
        }


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


def _prod_url() -> str:
    value = os.getenv("DATABASE_URL") or os.getenv("CLIENTPLATFORM_DATABASE_URL") or ""
    if not value.strip():
        raise SystemExit("DATABASE_URL/CLIENTPLATFORM_DATABASE_URL is required")
    return value.strip()


def _safe_db_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise SystemExit("target database name is empty")
    if not SAFE_DB_NAME_RE.fullmatch(name):
        raise SystemExit("target database name may contain only ASCII letters, digits, and underscore")
    if name in FORBIDDEN_DB_NAMES:
        raise SystemExit(f"refusing unsafe target database name: {name}")
    if "drill" not in name and "restore" not in name and "test" not in name:
        raise SystemExit("target database name must include one of: drill, restore, test")
    return name


def _target_url(*, prod_url: str, target_db: str) -> str:
    parts = urlsplit(prod_url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + quote(target_db), parts.query, parts.fragment))


def _redact_url(value: str) -> str:
    parts = urlsplit(value)
    user = unquote(parts.username or "")
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    auth = f"{user}:***@" if user else ""
    return urlunsplit((parts.scheme, f"{auth}{host}{port}", parts.path, parts.query, parts.fragment))


def _owner_from_url(prod_url: str) -> str:
    parts = urlsplit(prod_url)
    owner = unquote(parts.username or "").strip()
    if not owner:
        raise SystemExit("database URL username is required to choose drill DB owner")
    return owner


def _run(cmd: list[str]) -> str:
    # Reviewed: fixed runuser/psql/createdb command lists; target DB name is allow-listed before use.
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)  # nosec B603
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(output.strip() or str(proc.returncode))
    return output.strip()


def _db_exists(target_db: str) -> bool:
    target_db = _safe_db_name(target_db)
    sql = "SELECT 1 FROM pg_database WHERE datname = :'target_db'"
    output = _run(
        [
            "runuser",
            "-u",
            "postgres",
            "--",
            "psql",
            "-d",
            "postgres",
            "--tuples-only",
            "--no-align",
            "--set",
            f"target_db={target_db}",
            "--command",
            sql,
        ]
    )
    return output.strip() == "1"


def prepare_drill_db(*, target_db: str) -> DrillDbPrepareResult:
    prod_url = _prod_url()
    target_db = _safe_db_name(target_db)
    prod_db = unquote(urlsplit(prod_url).path.strip("/"))
    if prod_db and target_db == prod_db:
        raise SystemExit("target database matches production database; refusing")
    owner = _owner_from_url(prod_url)
    target = _target_url(prod_url=prod_url, target_db=target_db)
    problems: list[str] = []
    created = False
    existed = False
    try:
        if _db_exists(target_db):
            existed = True
        else:
            _run(["runuser", "-u", "postgres", "--", "createdb", "--owner", owner, target_db])
            created = True
    except RuntimeError as exc:
        problems.append(str(exc))
    return DrillDbPrepareResult(
        ok=not problems,
        target_db=target_db,
        owner=owner,
        created=created,
        existed=existed,
        target_url=target,
        problems=problems,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare non-production Postgres restore-drill database")
    parser.add_argument("--env-file", default=os.getenv("CLIENTPLATFORM_ENV_FILE", str(DEFAULT_ENV_FILE)))
    parser.add_argument("--target-db", default=os.getenv("CLIENTPLATFORM_RESTORE_DRILL_DB", "clientplatform_restore_drill"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--print-export", action="store_true", help="Print shell export for CLIENTPLATFORM_RESTORE_DRILL_DATABASE_URL")
    args = parser.parse_args()

    _apply_env(_load_env_file(args.env_file))
    result = prepare_drill_db(target_db=str(args.target_db))
    if args.print_export:
        if not result.ok:
            raise SystemExit("restore drill DB preparation failed: " + "; ".join(result.problems))
        print("export CLIENTPLATFORM_RESTORE_DRILL_DATABASE_URL=" + shlex.quote(result.target_url))
        return 0
    if args.json:
        print(json.dumps(result.to_safe_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(result.to_safe_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
