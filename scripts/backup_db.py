from __future__ import annotations

"""Create a verified SQLite backup without publishing partial artifacts."""

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


class BackupDbError(RuntimeError):
    """Expected backup source, copy or verification failure."""

    def __init__(self, code: str) -> None:
        normalized = str(code or "backup_failed")
        super().__init__(normalized)
        self.code = normalized


@dataclass(frozen=True)
class BackupReport:
    ok: bool
    source_path: str
    backup_path: str
    source_quick_check_ok: bool
    backup_integrity_ok: bool
    size_bytes: int
    pruned: int
    keep: int
    error_code: str = ""


def _backup_dir() -> Path:
    return ROOT / "backups"


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = _resolved(path).as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)


def _check_database(path: Path, *, quick: bool) -> None:
    pragma = "PRAGMA quick_check" if quick else "PRAGMA integrity_check"
    try:
        with closing(_readonly_connection(path)) as conn:
            row = conn.execute(pragma).fetchone()
    except sqlite3.Error as exc:
        code = "source_quick_check_unavailable" if quick else "backup_integrity_check_unavailable"
        raise BackupDbError(code) from exc
    except OSError as exc:
        code = "source_quick_check_unavailable" if quick else "backup_integrity_check_unavailable"
        raise BackupDbError(code) from exc
    if not row or str(row[0]).strip().casefold() != "ok":
        code = "source_quick_check_failed" if quick else "backup_integrity_check_failed"
        raise BackupDbError(code)


def _new_backup_target(backup_dir: Path) -> Path:
    for _attempt in range(10):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S-%f")
        candidate = backup_dir / f"data_{stamp}_{uuid.uuid4().hex[:8]}.db"
        if not candidate.exists():
            return candidate
    raise BackupDbError("backup_target_collision")


def _partial_path(target: Path) -> Path:
    return target.parent / f".{target.name}.{uuid.uuid4().hex}.partial"


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
        raise BackupDbError("backup_copy_failed") from exc
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise BackupDbError("backup_copy_failed") from exc


def _backup_keep() -> int:
    raw = (os.getenv("BACKUP_KEEP") or "14").strip()
    try:
        value = int(raw)
    except ValueError:
        return 14
    return max(0, min(value, 10_000))


def _prune_old_backups(backup_dir: Path, keep: int) -> int:
    if keep <= 0:
        return 0
    backups = sorted(backup_dir.glob("data_*.db"))
    if len(backups) <= keep:
        return 0
    removed = 0
    for path in backups[: len(backups) - keep]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _error_payload(source: Path, code: str) -> dict[str, object]:
    return {
        "ok": False,
        "source_path": str(_resolved(source)),
        "backup_path": "",
        "published": False,
        "error_code": str(code or "backup_failed"),
    }


def main() -> int:
    if is_postgres_enabled():
        print(
            "SKIP: CLIENTPLATFORM_DB_ENGINE=postgres uses pg_dump backups, not SQLite backup_db.py. "
            f"Target={redacted_db_target()}"
        )
        return 0

    source = _resolved(Path(DB_PATH))
    if not source.is_file():
        print(
            json.dumps(
                _error_payload(source, "source_not_found"),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    backup_dir = _backup_dir()
    target: Path | None = None
    partial: Path | None = None
    published = False
    try:
        _check_database(source, quick=True)
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = _new_backup_target(backup_dir)
        partial = _partial_path(target)
        _copy_database(source, partial)
        _check_database(partial, quick=False)
        os.replace(partial, target)
        published = True
        _check_database(target, quick=False)
        size_bytes = int(target.stat().st_size)
        keep = _backup_keep()
        removed = _prune_old_backups(backup_dir, keep)
        report = BackupReport(
            ok=True,
            source_path=str(source),
            backup_path=str(target),
            source_quick_check_ok=True,
            backup_integrity_ok=True,
            size_bytes=size_bytes,
            pruned=removed,
            keep=keep,
        )
    except BackupDbError as exc:
        if published and target is not None:
            target.unlink(missing_ok=True)
        print(
            json.dumps(
                _error_payload(source, exc.code),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    except OSError:
        if published and target is not None:
            target.unlink(missing_ok=True)
        print(
            json.dumps(
                _error_payload(source, "backup_runtime_os_error"),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)

    print(json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
