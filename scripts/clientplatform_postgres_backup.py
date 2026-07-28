from __future__ import annotations

"""PostgreSQL backup boundary and disposable restore drill.

The application DSN is used only for pg_dump. Restore drills require a separate
operator-supplied administrative DSN, so the runtime application role never
needs CREATEDB or superuser privileges. Credentials are passed through PG*
environment variables and never written to command arguments or evidence.
"""

import argparse
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

_REQUIRED_TABLES = (
    "businesses",
    "business_members",
    "customers",
    "programs",
    "booking_slots",
    "delivery_dispatch_outbox",
)


def _postgres_database_name(database_url: str, *, clientplatform_only: bool) -> str:
    parsed = urlsplit(database_url)
    name = parsed.path.lstrip("/")
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not name:
        raise ValueError("PostgreSQL URL must contain an explicit host and database name")
    if clientplatform_only and not name.startswith("clientplatform"):
        raise ValueError("refusing to operate on a non-ClientPlatform database")
    return name


def _database_name(database_url: str) -> str:
    return _postgres_database_name(database_url, clientplatform_only=True)


def _pg_environment(
    database_url: str,
    *,
    database: str | None = None,
    clientplatform_only: bool = True,
) -> dict[str, str]:
    parsed = urlsplit(database_url)
    source_name = _postgres_database_name(
        database_url,
        clientplatform_only=clientplatform_only,
    )
    env = os.environ.copy()
    env.update(
        {
            "PGHOST": str(parsed.hostname),
            "PGPORT": str(parsed.port or 5432),
            "PGDATABASE": database or source_name,
            "PGUSER": unquote(parsed.username or ""),
            "PGPASSWORD": unquote(parsed.password or ""),
            "PGCONNECT_TIMEOUT": "10",
        }
    )
    if not env["PGUSER"]:
        raise ValueError("PostgreSQL URL must contain a role")
    return env


def _run(command: list[str], *, env: dict[str, str], capture: bool = False) -> str:
    completed = subprocess.run(  # nosec B603 - fixed PostgreSQL client commands only
        command,
        env=env,
        check=False,
        capture_output=capture,
        text=capture,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{command[0]} failed with exit code {completed.returncode}")
    return completed.stdout.strip() if capture else ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_identifier(value: str) -> str:
    if not value or any(not (char.isalnum() or char == "_") for char in value):
        raise ValueError("unsafe PostgreSQL identifier")
    return value


def _quoted_identifier(value: str) -> str:
    return '"' + _safe_identifier(value).replace('"', '""') + '"'


def _prune(directory: Path, *, retention_days: int, now: float) -> None:
    cutoff = now - retention_days * 86_400
    for path in directory.glob("clientplatform-*.dump"):
        if path.stat().st_mtime >= cutoff:
            continue
        checksum = path.with_suffix(path.suffix + ".sha256")
        metadata = path.with_suffix(path.suffix + ".json")
        path.unlink(missing_ok=True)
        checksum.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)


def create_backup(
    *,
    database_url: str,
    backup_dir: Path,
    retention_days: int,
    now: float | None = None,
) -> Path:
    source_database = _database_name(database_url)
    if retention_days < 7 or retention_days > 365:
        raise ValueError("retention_days must be between 7 and 365")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    timestamp = datetime.fromtimestamp(now or time.time(), tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"clientplatform-{timestamp}.dump"
    partial = target.with_suffix(".dump.partial")
    env = _pg_environment(database_url)
    _run(
        [
            "pg_dump",
            "--format=custom",
            "--compress=9",
            "--no-owner",
            "--no-acl",
            "--file",
            str(partial),
        ],
        env=env,
    )
    partial.chmod(0o600)
    partial.replace(target)
    checksum = _sha256(target)
    checksum_path = target.with_suffix(target.suffix + ".sha256")
    checksum_path.write_text(f"{checksum}  {target.name}\n", encoding="utf-8")
    checksum_path.chmod(0o600)
    metadata_path = target.with_suffix(target.suffix + ".json")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "created_at": timestamp,
                "source_database": source_database,
                "dump_file": target.name,
                "sha256": checksum,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    metadata_path.chmod(0o600)
    _prune(backup_dir, retention_days=retention_days, now=now or time.time())
    return target


def verify_restore(
    *,
    database_url: str,
    admin_database_url: str,
    dump_path: Path,
    evidence_dir: Path,
    now: float | None = None,
) -> Path:
    source_database = _database_name(database_url)
    _postgres_database_name(admin_database_url, clientplatform_only=False)
    expected_checksum_path = dump_path.with_suffix(dump_path.suffix + ".sha256")
    expected_checksum = expected_checksum_path.read_text(encoding="utf-8").split()[0]
    actual_checksum = _sha256(dump_path)
    if actual_checksum != expected_checksum:
        raise ValueError("backup checksum mismatch")

    suffix = datetime.fromtimestamp(now or time.time(), tz=timezone.utc).strftime("%Y%m%d%H%M%S")
    restore_database = _safe_identifier(f"clientplatform_restore_{suffix}_{os.getpid()}")
    admin_env = _pg_environment(
        admin_database_url,
        database="postgres",
        clientplatform_only=False,
    )
    restore_env = _pg_environment(
        admin_database_url,
        database=restore_database,
        clientplatform_only=False,
    )
    quoted = _quoted_identifier(restore_database)
    _run(
        [
            "psql",
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            f"CREATE DATABASE {quoted}",
        ],
        env=admin_env,
    )
    try:
        _run(
            [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "--dbname",
                restore_database,
                str(dump_path),
            ],
            env=restore_env,
        )
        for table in _REQUIRED_TABLES:
            result = _run(
                [
                    "psql",
                    "--no-psqlrc",
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    f"SELECT to_regclass('public.{table}') IS NOT NULL",
                ],
                env=restore_env,
                capture=True,
            )
            if result != "t":
                raise RuntimeError(f"restore drill missing required table: {table}")

        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.chmod(0o700)
        evidence = evidence_dir / f"restore-{suffix}.json"
        evidence.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "verified_at": datetime.fromtimestamp(
                        now or time.time(), tz=timezone.utc
                    ).isoformat(timespec="seconds"),
                    "source_database": source_database,
                    "restore_database": restore_database,
                    "dump_file": dump_path.name,
                    "sha256": actual_checksum,
                    "required_tables": list(_REQUIRED_TABLES),
                    "ok": True,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        evidence.chmod(0o600)
        return evidence
    finally:
        _run(
            [
                "psql",
                "--no-psqlrc",
                "--set",
                "ON_ERROR_STOP=1",
                "--command",
                f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)",
            ],
            env=admin_env,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup")
    backup.add_argument("--backup-dir", type=Path)
    backup.add_argument("--retention-days", type=int)

    restore = subparsers.add_parser("restore-drill")
    restore.add_argument("dump", type=Path)
    restore.add_argument("--evidence-dir", type=Path)

    args = parser.parse_args()
    database_url = str(os.getenv("DATABASE_URL") or "").strip()
    if args.command == "backup":
        target = create_backup(
            database_url=database_url,
            backup_dir=args.backup_dir
            or Path(os.environ["CLIENTPLATFORM_BACKUP_DIR"]),
            retention_days=args.retention_days
            or int(os.getenv("CLIENTPLATFORM_BACKUP_RETENTION_DAYS") or "30"),
        )
        print(f"CLIENTPLATFORM_BACKUP_OK:{target}")
        return 0

    admin_database_url = str(
        os.getenv("CLIENTPLATFORM_RESTORE_ADMIN_DATABASE_URL") or ""
    ).strip()
    if not admin_database_url:
        raise SystemExit(
            "CLIENTPLATFORM_RESTORE_DRILL_FAILED: "
            "CLIENTPLATFORM_RESTORE_ADMIN_DATABASE_URL is required"
        )
    evidence = verify_restore(
        database_url=database_url,
        admin_database_url=admin_database_url,
        dump_path=args.dump,
        evidence_dir=args.evidence_dir
        or Path(os.environ["CLIENTPLATFORM_RESTORE_EVIDENCE_DIR"]),
    )
    print(f"CLIENTPLATFORM_RESTORE_DRILL_OK:{evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
