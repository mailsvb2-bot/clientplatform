from __future__ import annotations

"""Guarded SQLite restore helper.

The CLI is dry-run by default. Applying a restore requires an explicit flag and
an exact confirmation that the application service is stopped. Restore data is
copied to a verified sibling staging file and atomically installed only after a
verified pre-restore safety backup has been created for an existing target.
"""

import argparse
import json
import os
import sqlite3
import sys
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT_PATH = Path(__file__).resolve().parents[1]
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))

from core.paths import DB_PATH, ROOT
from services.db.runtime import is_postgres_enabled, redacted_db_target

RESTORE_CONFIRMATION = "I_CONFIRM_CLIENTPLATFORM_IS_STOPPED"


class RestoreDbError(RuntimeError):
    """Expected restore safety or verification failure."""

    def __init__(
        self,
        code: str,
        *,
        applied: bool = False,
        rollback_performed: bool = False,
    ) -> None:
        normalized = str(code or "restore_failed")
        super().__init__(normalized)
        self.code = normalized
        self.applied = bool(applied)
        self.rollback_performed = bool(rollback_performed)


@dataclass(frozen=True)
class RestoreReport:
    ok: bool
    mode: str
    applied: bool
    source_path: str
    target_path: str
    source_integrity_ok: bool
    staged_integrity_ok: bool
    target_existed_before: bool
    safety_backup_path: str
    target_integrity_ok: bool
    rollback_performed: bool
    sidecars_removed: int
    error_code: str = ""


def _backup_dir() -> Path:
    return ROOT / "backups"


def _latest_backup() -> Path | None:
    backups = sorted(_backup_dir().glob("data_*.db"))
    return backups[-1] if backups else None


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = _resolved(path).as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)


def _integrity_check(path: Path) -> None:
    try:
        with closing(_readonly_connection(path)) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise RestoreDbError("integrity_check_unavailable") from exc
    except OSError as exc:
        raise RestoreDbError("integrity_check_unavailable") from exc
    if not row or str(row[0]).strip().casefold() != "ok":
        raise RestoreDbError("integrity_check_failed")


def _copy_database(source: Path, target: Path) -> None:
    source_resolved = _resolved(source)
    target_resolved = _resolved(target)
    if source_resolved == target_resolved:
        raise RestoreDbError("source_equals_target")
    if not source_resolved.is_file():
        raise RestoreDbError("source_not_found")
    target_resolved.parent.mkdir(parents=True, exist_ok=True)
    target_resolved.unlink(missing_ok=True)
    try:
        with closing(_readonly_connection(source_resolved)) as src:
            with closing(
                sqlite3.connect(
                    str(target_resolved),
                    timeout=30,
                    check_same_thread=False,
                )
            ) as dst:
                src.backup(dst)
                dst.commit()
    except sqlite3.Error as exc:
        target_resolved.unlink(missing_ok=True)
        raise RestoreDbError("database_copy_failed") from exc
    except OSError as exc:
        target_resolved.unlink(missing_ok=True)
        raise RestoreDbError("database_copy_failed") from exc


def _restore(source: Path, target: Path) -> None:
    """Compatibility helper for restore drills targeting disposable paths."""

    _copy_database(source, target)


def _unique_sibling(target: Path, label: str) -> Path:
    return target.parent / f".{target.name}.{label}-{uuid.uuid4().hex}.tmp"


def _unique_safety_backup() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S-%f")
    return _backup_dir() / f"pre_restore_{stamp}_{uuid.uuid4().hex[:8]}.db"


def _target_sidecars(target: Path) -> tuple[Path, Path]:
    return Path(str(target) + "-wal"), Path(str(target) + "-shm")


def _remove_target_sidecars(target: Path) -> int:
    removed = 0
    for sidecar in _target_sidecars(target):
        try:
            if sidecar.exists():
                sidecar.unlink()
                removed += 1
        except OSError as exc:
            raise RestoreDbError("target_sidecar_cleanup_failed") from exc
    return removed


def _assert_target_quiescent(target: Path) -> None:
    if not target.exists():
        return
    try:
        with closing(
            sqlite3.connect(
                str(target),
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
                    raise RestoreDbError("target_database_busy")
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("ROLLBACK")
    except RestoreDbError:
        raise
    except sqlite3.Error as exc:
        raise RestoreDbError("target_database_busy") from exc
    except OSError as exc:
        raise RestoreDbError("target_database_unavailable") from exc


def _rollback_target(
    *,
    target: Path,
    target_existed_before: bool,
    safety_backup: Path | None,
) -> bool:
    rollback_stage: Path | None = None
    try:
        if target_existed_before:
            if safety_backup is None or not safety_backup.exists():
                return False
            rollback_stage = _unique_sibling(target, "rollback")
            _copy_database(safety_backup, rollback_stage)
            _integrity_check(rollback_stage)
            _remove_target_sidecars(target)
            os.replace(rollback_stage, target)
            _integrity_check(target)
            return True
        target.unlink(missing_ok=True)
        _remove_target_sidecars(target)
        return not target.exists()
    except RestoreDbError:
        return False
    except OSError:
        return False
    finally:
        if rollback_stage is not None:
            rollback_stage.unlink(missing_ok=True)


def _apply_restore(source: Path, target: Path) -> RestoreReport:
    source_resolved = _resolved(source)
    target_resolved = _resolved(target)
    if source_resolved == target_resolved:
        raise RestoreDbError("source_equals_target")

    _integrity_check(source_resolved)
    target_existed_before = target_resolved.exists()
    stage = _unique_sibling(target_resolved, "restore")
    safety_backup: Path | None = None
    sidecars_removed = 0
    replaced = False

    try:
        _copy_database(source_resolved, stage)
        _integrity_check(stage)

        if target_existed_before:
            _assert_target_quiescent(target_resolved)
            safety_backup = _unique_safety_backup()
            safety_backup.parent.mkdir(parents=True, exist_ok=True)
            _copy_database(target_resolved, safety_backup)
            _integrity_check(safety_backup)

        sidecars_removed = _remove_target_sidecars(target_resolved)
        target_resolved.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, target_resolved)
        replaced = True
        _integrity_check(target_resolved)

        return RestoreReport(
            ok=True,
            mode="apply",
            applied=True,
            source_path=str(source_resolved),
            target_path=str(target_resolved),
            source_integrity_ok=True,
            staged_integrity_ok=True,
            target_existed_before=target_existed_before,
            safety_backup_path=str(safety_backup) if safety_backup is not None else "",
            target_integrity_ok=True,
            rollback_performed=False,
            sidecars_removed=sidecars_removed,
        )
    except RestoreDbError as exc:
        if not replaced:
            raise
        rolled_back = _rollback_target(
            target=target_resolved,
            target_existed_before=target_existed_before,
            safety_backup=safety_backup,
        )
        code = "restore_failed_rolled_back" if rolled_back else "restore_failed_rollback_failed"
        raise RestoreDbError(
            code,
            applied=True,
            rollback_performed=rolled_back,
        ) from exc
    except OSError as exc:
        if not replaced:
            raise RestoreDbError("atomic_replace_failed") from exc
        rolled_back = _rollback_target(
            target=target_resolved,
            target_existed_before=target_existed_before,
            safety_backup=safety_backup,
        )
        code = "restore_failed_rolled_back" if rolled_back else "restore_failed_rollback_failed"
        raise RestoreDbError(
            code,
            applied=True,
            rollback_performed=rolled_back,
        ) from exc
    finally:
        stage.unlink(missing_ok=True)


def _dry_run_report(source: Path, target: Path) -> RestoreReport:
    source_resolved = _resolved(source)
    target_resolved = _resolved(target)
    if source_resolved == target_resolved:
        raise RestoreDbError("source_equals_target")
    _integrity_check(source_resolved)
    return RestoreReport(
        ok=True,
        mode="dry_run",
        applied=False,
        source_path=str(source_resolved),
        target_path=str(target_resolved),
        source_integrity_ok=True,
        staged_integrity_ok=False,
        target_existed_before=target_resolved.exists(),
        safety_backup_path="",
        target_integrity_ok=False,
        rollback_performed=False,
        sidecars_removed=0,
    )


def _error_report(
    *,
    mode: str,
    source: Path | None,
    target: Path,
    error: RestoreDbError,
) -> dict[str, object]:
    return {
        "ok": False,
        "mode": mode,
        "applied": error.applied,
        "source_path": str(_resolved(source)) if source is not None else "",
        "target_path": str(_resolved(target)),
        "rollback_performed": error.rollback_performed,
        "error_code": error.code,
    }


def main(argv: list[str] | None = None) -> int:
    if is_postgres_enabled():
        raise SystemExit(
            "REFUSE: CLIENTPLATFORM_DB_ENGINE=postgres uses pg_dump/psql restore, not SQLite restore_db.py. "
            f"Target={redacted_db_target()}"
        )

    parser = argparse.ArgumentParser(description="Restore ClientPlatform SQLite DB from backup")
    parser.add_argument("--from-path", dest="from_path", default="", help="Path to backup .db file")
    parser.add_argument("--apply", action="store_true", help="Apply the verified restore")
    parser.add_argument("--confirm-service-stopped", default="")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Compatibility option; verification is always mandatory",
    )
    args = parser.parse_args(argv)

    source = Path(args.from_path).expanduser() if args.from_path else _latest_backup()
    target = Path(DB_PATH)
    mode = "apply" if args.apply else "dry_run"
    if source is None or not source.exists():
        error = RestoreDbError("source_not_found")
        print(json.dumps(_error_report(mode=mode, source=source, target=target, error=error), ensure_ascii=False, sort_keys=True))
        return 2

    try:
        if not args.apply:
            report = _dry_run_report(source, target)
        else:
            if str(args.confirm_service_stopped or "") != RESTORE_CONFIRMATION:
                raise RestoreDbError("service_stop_confirmation_invalid")
            report = _apply_restore(source, target)
    except RestoreDbError as exc:
        print(
            json.dumps(
                _error_report(mode=mode, source=source, target=target, error=exc),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    except sqlite3.Error as exc:
        error = RestoreDbError("restore_runtime_sqlite_error")
        print(json.dumps(_error_report(mode=mode, source=source, target=target, error=error), ensure_ascii=False, sort_keys=True))
        return 2
    except OSError as exc:
        error = RestoreDbError("restore_runtime_os_error")
        print(json.dumps(_error_report(mode=mode, source=source, target=target, error=error), ensure_ascii=False, sort_keys=True))
        return 2

    print(json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
