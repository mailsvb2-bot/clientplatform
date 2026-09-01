from __future__ import annotations

import argparse
import os
# Reviewed: operator restore-drill script invokes fixed Postgres tools against validated non-production target.
import subprocess  # nosec B404
from pathlib import Path
from urllib.parse import unquote, urlparse


REQUIRED_TABLES = (
    "users",
    "payments",
    "jobs",
    "subscriptions",
)
DEFAULT_BACKUP_DIR = Path(os.getenv("CLIENTPLATFORM_POSTGRES_BACKUP_DIR", "/var/backups/clientplatform/postgres"))
SUPPORTED_SUFFIXES = (".dump", ".sql", ".sql.gz")
FORBIDDEN_DRILL_DB_NAMES = {"postgres", "template0", "template1"}


def _database_name_from_url(value: str) -> str:
    parsed = urlparse(value.strip())
    return unquote((parsed.path or "").lstrip("/")).strip()


def _target_url() -> str:
    value = os.getenv("CLIENTPLATFORM_RESTORE_DRILL_DATABASE_URL") or os.getenv("RESTORE_DATABASE_URL") or ""
    target = value.strip()
    if not target:
        raise SystemExit("CLIENTPLATFORM_RESTORE_DRILL_DATABASE_URL is required. Never point it to production.")

    production_url = (os.getenv("DATABASE_URL") or "").strip()
    if production_url and target == production_url:
        raise SystemExit("Restore drill target equals DATABASE_URL; refusing to touch production database")

    target_db = _database_name_from_url(target)
    production_db = _database_name_from_url(production_url) if production_url else ""
    if not target_db:
        raise SystemExit("Restore drill target database name is empty; refusing to continue")
    if target_db in FORBIDDEN_DRILL_DB_NAMES:
        raise SystemExit(f"Restore drill target database name is forbidden: {target_db}")
    if production_db and target_db == production_db:
        raise SystemExit(
            "Restore drill target database name matches production database; refusing to touch production database"
        )
    if "drill" not in target_db and "restore" not in target_db and "test" not in target_db:
        raise SystemExit(
            "Restore drill target database name must clearly be non-production "
            "and include one of: drill, restore, test"
        )
    return target


def _is_supported_backup(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in SUPPORTED_SUFFIXES)


def latest_backup(*, backup_dir: Path = DEFAULT_BACKUP_DIR) -> Path:
    files = sorted(
        (path for path in backup_dir.iterdir() if path.is_file() and _is_supported_backup(path)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ) if backup_dir.exists() else []
    if not files:
        raise SystemExit(f"No Postgres backups found in {backup_dir}; expected one of: {', '.join(SUPPORTED_SUFFIXES)}")
    return files[0]


def _format_failure(cmd: list[str], output: str, returncode: int) -> str:
    return (output.strip() or f"command {' '.join(cmd)} exited with {returncode}")


def _run(cmd: list[str], *, input_text: str | None = None) -> str:
    # Reviewed: fixed Postgres tooling command lists, no shell; target URL is guarded non-production.
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, input=input_text)  # nosec B603
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SystemExit(_format_failure(cmd, out, proc.returncode))
    return out.strip()


def _run_pipeline(producer_cmd: list[str], consumer_cmd: list[str]) -> str:
    # Reviewed: fixed gzip/psql restore-drill pipeline, no shell; target URL is guarded non-production.
    producer = subprocess.Popen(producer_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # nosec B603
    assert producer.stdout is not None
    consumer = subprocess.Popen(  # nosec B603
        consumer_cmd,
        stdin=producer.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    producer.stdout.close()
    consumer_stdout, consumer_stderr = consumer.communicate()
    producer_stderr = producer.stderr.read() if producer.stderr else b""
    producer_returncode = producer.wait()
    output = b"".join(part for part in (consumer_stdout, consumer_stderr, producer_stderr) if part).decode("utf-8", errors="replace")
    if producer_returncode != 0:
        raise SystemExit(_format_failure(producer_cmd, output, producer_returncode))
    if consumer.returncode != 0:
        raise SystemExit(_format_failure(consumer_cmd, output, consumer.returncode))
    return output.strip()


def _reset_target_database(target: str) -> None:
    reset_sql = "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"
    _run(["psql", target, "--set", "ON_ERROR_STOP=1", "--command", reset_sql])


def _restore_backup(*, dump_path: Path, target: str) -> None:
    lower_name = dump_path.name.lower()
    if lower_name.endswith(".dump"):
        _run(["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges", "--dbname", target, str(dump_path)])
        return
    if lower_name.endswith(".sql.gz"):
        _reset_target_database(target)
        _run_pipeline(
            ["gzip", "-dc", str(dump_path)],
            ["psql", target, "--set", "ON_ERROR_STOP=1"],
        )
        return
    if lower_name.endswith(".sql"):
        _reset_target_database(target)
        _run(["psql", target, "--set", "ON_ERROR_STOP=1", "--file", str(dump_path)])
        return
    raise SystemExit(f"Unsupported backup format: {dump_path}")


def restore_drill(*, dump_path: Path) -> None:
    if not dump_path.exists():
        raise SystemExit(f"Backup file not found: {dump_path}")
    target = _target_url()
    _restore_backup(dump_path=dump_path, target=target)
    table_sql = "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()"
    out = _run(["psql", target, "--tuples-only", "--no-align", "--command", table_sql])
    tables = {line.strip() for line in out.splitlines() if line.strip()}
    missing = [table for table in REQUIRED_TABLES if table not in tables]
    if missing:
        raise SystemExit("RESTORE_DRILL_FAILED missing tables: " + ", ".join(missing))
    print("POSTGRES_RESTORE_DRILL_OK tables=" + ",".join(sorted(tables)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a pg_dump backup into a non-production drill database and verify core tables")
    parser.add_argument("dump_path", nargs="?", help="Path to a .dump, .sql, or .sql.gz file. Use --latest to restore the newest backup.")
    parser.add_argument("--latest", action="store_true", help="Restore the newest backup from CLIENTPLATFORM_POSTGRES_BACKUP_DIR")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    args = parser.parse_args()
    dump = latest_backup(backup_dir=Path(args.backup_dir)) if args.latest else Path(args.dump_path or "")
    if not str(dump):
        raise SystemExit("dump_path is required unless --latest is used")
    restore_drill(dump_path=dump)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
