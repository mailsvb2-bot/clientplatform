from __future__ import annotations

"""Archive the legacy SQLite artifact after Postgres migration.

The command is dry-run by default. Apply mode requires confirmed Postgres storage
and an operator confirmation that the legacy SQLite file is no longer active.
The source is copied through SQLite's backup API into a verified hidden artifact,
published without overwriting an existing archive, verified again, and only then
removed together with stale WAL/SHM sidecars.
"""

import argparse
import json
import os
import shlex
import sqlite3
import sys
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = Path("/etc/clientplatform/clientplatform.env")
DEFAULT_ARCHIVE_DIRNAME = "legacy_sqlite_archive"
ARCHIVE_CONFIRMATION = "I_CONFIRM_LEGACY_SQLITE_IS_INACTIVE_AND_POSTGRES_IS_PRIMARY"


@dataclass(frozen=True)
class LegacySqliteArchivePlan:
    ok: bool
    action: str
    dry_run: bool
    source_path: str | None
    archive_path: str | None
    active_engine: str
    database_url_configured: bool
    integrity_ok: bool | None
    sqlite_page_count: int | None
    sqlite_table_count: int | None
    archive_verified: bool = False
    source_removed: bool = False
    sidecars_removed: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "dry_run": self.dry_run,
            "source_path": self.source_path,
            "archive_path": self.archive_path,
            "active_engine": self.active_engine,
            "database_url_configured": self.database_url_configured,
            "integrity_ok": self.integrity_ok,
            "sqlite_page_count": self.sqlite_page_count,
            "sqlite_table_count": self.sqlite_table_count,
            "archive_verified": self.archive_verified,
            "source_removed": self.source_removed,
            "sidecars_removed": self.sidecars_removed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class _SourceIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int


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
    return sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)


def _read_sqlite_metadata(path: Path) -> tuple[bool, int | None, int | None, str]:
    try:
        with closing(_readonly_connection(path)) as conn:
            integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = bool(
                integrity_row and str(integrity_row[0]).strip().casefold() == "ok"
            )
            page_row = conn.execute("PRAGMA page_count").fetchone()
            table_row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()
            page_count = (
                int(page_row[0])
                if page_row and page_row[0] is not None
                else None
            )
            table_count = (
                int(table_row[0])
                if table_row and table_row[0] is not None
                else None
            )
    except sqlite3.Error:
        return False, None, None, "sqlite_metadata_read_failed:SQLiteError"
    except OSError:
        return False, None, None, "sqlite_metadata_read_failed:OSError"
    if not integrity_ok:
        return False, page_count, table_count, "sqlite_integrity_check_failed"
    return True, page_count, table_count, ""


def _archive_target(source: Path, archive_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return archive_dir / (
        f"{source.stem}.{stamp}.{uuid.uuid4().hex[:10]}{source.suffix or '.db'}"
    )


def _partial_target(target: Path) -> Path:
    return target.parent / f".{target.name}.{uuid.uuid4().hex}.partial"


def _source_identity(path: Path) -> _SourceIdentity:
    stat = path.stat()
    return _SourceIdentity(
        device=int(stat.st_dev),
        inode=int(stat.st_ino),
        size=int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
    )


def _source_unchanged(path: Path, expected: _SourceIdentity) -> bool:
    try:
        return _source_identity(path) == expected
    except OSError:
        return False


def _quiesce_source(path: Path) -> None:
    try:
        with closing(
            sqlite3.connect(
                str(path),
                timeout=1,
                check_same_thread=False,
                isolation_level=None,
            )
        ) as conn:
            conn.execute("PRAGMA busy_timeout=1000")
            mode_row = conn.execute("PRAGMA journal_mode").fetchone()
            mode = str(mode_row[0] if mode_row else "").strip().casefold()
            if mode == "wal":
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint and int(checkpoint[0] or 0) != 0:
                    raise RuntimeError("legacy_sqlite_busy")
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("ROLLBACK")
    except RuntimeError:
        raise
    except sqlite3.Error as exc:
        raise RuntimeError("legacy_sqlite_busy") from exc
    except OSError as exc:
        raise RuntimeError("legacy_sqlite_unavailable") from exc


def _copy_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        with closing(_readonly_connection(source)) as src:
            with closing(
                sqlite3.connect(
                    str(target),
                    timeout=30,
                    check_same_thread=False,
                )
            ) as dst:
                src.backup(dst)
                dst.commit()
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError("archive_copy_failed") from exc
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError("archive_copy_failed") from exc


def _publish_without_replace(partial: Path, target: Path) -> None:
    try:
        os.link(partial, target)
        partial.unlink()
    except FileExistsError as exc:
        raise RuntimeError("archive_target_collision") from exc
    except OSError as exc:
        raise RuntimeError("archive_publish_failed") from exc


def _remove_source_sidecars(source: Path) -> int:
    removed = 0
    for sidecar in (Path(str(source) + "-wal"), Path(str(source) + "-shm")):
        try:
            if sidecar.exists():
                sidecar.unlink()
                removed += 1
        except OSError as exc:
            raise RuntimeError("legacy_sidecar_remove_failed") from exc
    return removed


def _result_from_plan(
    plan: LegacySqliteArchivePlan,
    *,
    ok: bool,
    action: str,
    archive_verified: bool = False,
    source_removed: bool = False,
    sidecars_removed: int = 0,
    reason: str = "",
) -> LegacySqliteArchivePlan:
    return LegacySqliteArchivePlan(
        ok=ok,
        action=action,
        dry_run=False,
        source_path=plan.source_path,
        archive_path=plan.archive_path,
        active_engine=plan.active_engine,
        database_url_configured=plan.database_url_configured,
        integrity_ok=plan.integrity_ok,
        sqlite_page_count=plan.sqlite_page_count,
        sqlite_table_count=plan.sqlite_table_count,
        archive_verified=archive_verified,
        source_removed=source_removed,
        sidecars_removed=sidecars_removed,
        reason=reason,
    )


def build_archive_plan(
    *,
    archive_dir: Path | None,
    dry_run: bool,
) -> LegacySqliteArchivePlan:
    from services.storage_legacy_audit import storage_legacy_audit

    audit = storage_legacy_audit()
    if audit.active_engine != "postgres" or not audit.database_url_configured:
        return LegacySqliteArchivePlan(
            ok=False,
            action="refuse",
            dry_run=dry_run,
            source_path=audit.legacy_sqlite_path,
            archive_path=None,
            active_engine=audit.active_engine,
            database_url_configured=audit.database_url_configured,
            integrity_ok=None,
            sqlite_page_count=None,
            sqlite_table_count=None,
            reason="active_storage_is_not_confirmed_postgres",
        )
    if not audit.legacy_sqlite_path:
        return LegacySqliteArchivePlan(
            ok=True,
            action="noop",
            dry_run=dry_run,
            source_path=None,
            archive_path=None,
            active_engine=audit.active_engine,
            database_url_configured=audit.database_url_configured,
            integrity_ok=None,
            sqlite_page_count=None,
            sqlite_table_count=None,
            reason="no_legacy_path_in_postgres_mode",
        )
    source = _resolved(Path(audit.legacy_sqlite_path))
    if not source.exists():
        return LegacySqliteArchivePlan(
            ok=True,
            action="noop",
            dry_run=dry_run,
            source_path=str(source),
            archive_path=None,
            active_engine=audit.active_engine,
            database_url_configured=audit.database_url_configured,
            integrity_ok=None,
            sqlite_page_count=None,
            sqlite_table_count=None,
            reason="legacy_sqlite_not_present",
        )
    if not source.is_file():
        return LegacySqliteArchivePlan(
            ok=False,
            action="refuse",
            dry_run=dry_run,
            source_path=str(source),
            archive_path=None,
            active_engine=audit.active_engine,
            database_url_configured=audit.database_url_configured,
            integrity_ok=False,
            sqlite_page_count=None,
            sqlite_table_count=None,
            reason="legacy_sqlite_not_a_file",
        )

    integrity_ok, page_count, table_count, reason = _read_sqlite_metadata(source)
    if not integrity_ok:
        return LegacySqliteArchivePlan(
            ok=False,
            action="refuse",
            dry_run=dry_run,
            source_path=str(source),
            archive_path=None,
            active_engine=audit.active_engine,
            database_url_configured=audit.database_url_configured,
            integrity_ok=False,
            sqlite_page_count=page_count,
            sqlite_table_count=table_count,
            reason=reason,
        )

    target_dir = _resolved(
        archive_dir or source.parent / DEFAULT_ARCHIVE_DIRNAME
    )
    target = _archive_target(source, target_dir)
    return LegacySqliteArchivePlan(
        ok=True,
        action="archive",
        dry_run=dry_run,
        source_path=str(source),
        archive_path=str(target),
        active_engine=audit.active_engine,
        database_url_configured=audit.database_url_configured,
        integrity_ok=True,
        sqlite_page_count=page_count,
        sqlite_table_count=table_count,
    )


def archive_legacy_sqlite(
    *,
    archive_dir: Path | None = None,
    dry_run: bool = True,
) -> LegacySqliteArchivePlan:
    plan = build_archive_plan(archive_dir=archive_dir, dry_run=dry_run)
    if not plan.ok or plan.action != "archive" or dry_run:
        return plan

    source = Path(str(plan.source_path))
    target = Path(str(plan.archive_path))
    partial = _partial_target(target)
    published = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        _quiesce_source(source)
        identity = _source_identity(source)
        _copy_database(source, partial)
        copied_ok, _pages, _tables, copied_reason = _read_sqlite_metadata(partial)
        if not copied_ok:
            raise RuntimeError(copied_reason or "archive_partial_integrity_failed")
        os.chmod(partial, 0o600)
        _publish_without_replace(partial, target)
        published = True
        archived_ok, _pages, _tables, archived_reason = _read_sqlite_metadata(target)
        if not archived_ok:
            raise RuntimeError(archived_reason or "archive_final_integrity_failed")
        if not _source_unchanged(source, identity):
            raise RuntimeError("legacy_sqlite_changed_during_archive")
        source.unlink()
        sidecars_removed = _remove_source_sidecars(source)
        return _result_from_plan(
            plan,
            ok=True,
            action="archived",
            archive_verified=True,
            source_removed=True,
            sidecars_removed=sidecars_removed,
        )
    except RuntimeError as exc:
        reason = str(exc) if str(exc) else "archive_failed"
        if published and source.exists():
            target.unlink(missing_ok=True)
            published = False
        if published and not source.exists():
            return _result_from_plan(
                plan,
                ok=False,
                action="archive_incomplete",
                archive_verified=True,
                source_removed=True,
                reason=reason,
            )
        return _result_from_plan(
            plan,
            ok=False,
            action="refuse",
            archive_verified=False,
            source_removed=False,
            reason=reason,
        )
    except OSError:
        if published and source.exists():
            target.unlink(missing_ok=True)
        return _result_from_plan(
            plan,
            ok=False,
            action="refuse",
            archive_verified=False,
            source_removed=not source.exists(),
            reason="archive_filesystem_failed",
        )
    finally:
        partial.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive legacy SQLite artifact after confirmed Postgres migration"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create a verified archive and remove the inactive legacy SQLite file",
    )
    parser.add_argument("--confirm-legacy-inactive", default="")
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    parser.add_argument(
        "--env-file",
        default=os.getenv("CLIENTPLATFORM_ENV_FILE", str(DEFAULT_ENV_FILE)),
    )
    parser.add_argument(
        "--archive-dir",
        default=os.getenv("CLIENTPLATFORM_LEGACY_SQLITE_ARCHIVE_DIR", ""),
    )
    args = parser.parse_args()

    env_file = Path(args.env_file) if args.env_file else None
    loaded = _load_env_file(env_file)
    _apply_env(loaded)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    if args.apply and str(args.confirm_legacy_inactive or "") != ARCHIVE_CONFIRMATION:
        result = LegacySqliteArchivePlan(
            ok=False,
            action="refuse",
            dry_run=False,
            source_path=None,
            archive_path=None,
            active_engine="unknown",
            database_url_configured=bool(os.getenv("DATABASE_URL")),
            integrity_ok=None,
            sqlite_page_count=None,
            sqlite_table_count=None,
            reason="legacy_inactive_confirmation_invalid",
        )
    else:
        archive_dir = (
            Path(args.archive_dir)
            if str(args.archive_dir or "").strip()
            else None
        )
        result = archive_legacy_sqlite(
            archive_dir=archive_dir,
            dry_run=not bool(args.apply),
        )

    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"LEGACY_SQLITE_ARCHIVE_{mode} action={result.action} ok={result.ok}")
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
