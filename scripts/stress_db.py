from __future__ import annotations

"""Run a bounded SQLite concurrency diagnostic without touching app storage by default.

The default target is a unique temporary database that is removed after the run.
Any operator-supplied path requires explicit authorization before the script can
create a file, change SQLite journal mode, create the stress table or insert rows.
Configured application database paths require an additional exact confirmation.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Thread
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = SCRIPT_DIR / "stress_db_schema.sql"
CLEANUP_SCHEMA_PATH = SCRIPT_DIR / "stress_db_cleanup.sql"
CONFIGURED_DB_CONFIRMATION = "I_UNDERSTAND_THIS_MODIFIES_THE_CONFIGURED_DATABASE"
_SQLITE_URL_RE = re.compile(r"^sqlite(?:\+[^:]+)?:///([^?]+)", re.IGNORECASE)
_JOURNAL_MODES = {"delete", "truncate", "persist", "memory", "wal", "off"}
_REQUIRED_STRESS_COLUMNS = {"run_id", "worker", "n"}


class DbStressSafetyError(RuntimeError):
    """Expected fail-closed target authorization error."""

    def __init__(self, code: str) -> None:
        normalized = str(code or "stress_db_target_rejected")
        super().__init__(normalized)
        self.code = normalized


@dataclass(frozen=True)
class DbStressReport:
    ok: bool
    db_path: str
    target_kind: str
    workers: int
    iterations: int
    expected_rows: int
    actual_rows: int
    residual_rows: int
    elapsed_sec: float
    db_existed_before: bool
    table_existed_before: bool
    table_removed: bool
    journal_mode_restored: bool
    cleanup_status: str
    errors: tuple[str, ...]


def _resolved_path(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve(strict=False)


def _sqlite_url_path(raw: str) -> Path | None:
    match = _SQLITE_URL_RE.match(str(raw or "").strip())
    if match is None:
        return None
    value = unquote(match.group(1)).strip()
    if not value or value == ":memory:":
        return None
    return _resolved_path(Path(value))


def _configured_db_paths() -> tuple[Path, ...]:
    candidates: set[Path] = {_resolved_path(ROOT / "data" / "data.db")}
    configured_path = (os.getenv("CLIENTPLATFORM_DB_PATH") or "").strip()
    if configured_path:
        candidates.add(_resolved_path(Path(configured_path)))
    database_url_path = _sqlite_url_path(os.getenv("DATABASE_URL") or "")
    if database_url_path is not None:
        candidates.add(database_url_path)
    return tuple(sorted(candidates, key=str))


def _authorize_custom_target(
    path: Path,
    *,
    allow_custom: bool,
    allow_existing: bool,
    allow_configured: bool,
    configured_confirmation: str,
) -> Path:
    resolved = _resolved_path(path)
    if not allow_custom:
        raise DbStressSafetyError("custom_db_path_requires_allow_custom")
    if resolved.exists() and not resolved.is_file():
        raise DbStressSafetyError("custom_db_path_must_be_a_file")
    if resolved.exists() and not allow_existing:
        raise DbStressSafetyError("existing_db_path_requires_allow_existing")
    if resolved in _configured_db_paths():
        if not allow_configured:
            raise DbStressSafetyError("configured_db_path_requires_allow_configured")
        if configured_confirmation != CONFIGURED_DB_CONFIRMATION:
            raise DbStressSafetyError("configured_db_path_confirmation_invalid")
    return resolved


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), timeout=30, check_same_thread=False)


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stress_events' LIMIT 1"
    ).fetchone()
    return bool(row)


def _stress_table_compatible(conn: sqlite3.Connection) -> bool:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(stress_events)").fetchall()
        if len(row) > 1
    }
    return _REQUIRED_STRESS_COLUMNS.issubset(columns)


def _restore_journal_mode(path: Path, previous_mode: str) -> bool:
    normalized = str(previous_mode or "").strip().lower()
    if normalized not in _JOURNAL_MODES:
        return False
    with closing(_connect(path)) as conn:
        row = conn.execute(
            f"PRAGMA journal_mode={normalized.upper()}"  # nosec B608 - normalized from whitelist
        ).fetchone()
        restored = str(row[0] if row else "").strip().lower()
        conn.commit()
    return restored == normalized


def _init(path: Path) -> tuple[bool, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_journal_mode = "delete"
    try:
        with closing(_connect(path)) as conn:
            journal_row = conn.execute("PRAGMA journal_mode").fetchone()
            previous_journal_mode = str(
                journal_row[0] if journal_row else "delete"
            ).strip().lower()
            table_existed_before = _table_exists(conn)
            if table_existed_before and not _stress_table_compatible(conn):
                raise DbStressSafetyError("existing_stress_table_schema_incompatible")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.commit()
        return table_existed_before, previous_journal_mode
    except (DbStressSafetyError, sqlite3.Error, OSError):
        try:
            _restore_journal_mode(path, previous_journal_mode)
        except (sqlite3.Error, OSError):
            pass
        raise


def _worker(
    path: Path,
    *,
    run_id: str,
    worker_id: int,
    iterations: int,
    errors: list[str],
) -> None:
    try:
        conn = _connect(path)
        try:
            for n in range(iterations):
                conn.execute(
                    "INSERT INTO stress_events(run_id, worker, n) VALUES(?,?,?)",
                    (run_id, int(worker_id), int(n)),
                )
                if n % 50 == 0:
                    conn.commit()
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        errors.append(f"worker={worker_id}:{type(exc).__name__}")
    except OSError as exc:
        errors.append(f"worker={worker_id}:{type(exc).__name__}")


def _cleanup_run_rows(
    path: Path,
    *,
    run_id: str,
    table_existed_before: bool,
    keep_rows: bool,
    errors: list[str],
) -> tuple[bool, int]:
    table_removed = False
    try:
        with closing(_connect(path)) as conn:
            if keep_rows:
                row = conn.execute(
                    "SELECT COUNT(*) FROM stress_events WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                return False, int(row[0]) if row else 0

            if _table_exists(conn):
                conn.execute("DELETE FROM stress_events WHERE run_id=?", (run_id,))
                if not table_existed_before:
                    conn.executescript(CLEANUP_SCHEMA_PATH.read_text(encoding="utf-8"))
                    table_removed = True
                conn.commit()

            if table_removed or not _table_exists(conn):
                residual = 0
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM stress_events WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                residual = int(row[0]) if row else 0
    except (sqlite3.Error, OSError) as exc:
        errors.append(f"cleanup:{type(exc).__name__}")
        residual = -1
    return table_removed, residual


def run(
    path: Path,
    *,
    workers: int,
    iterations: int,
    keep_rows: bool,
    target_kind: str = "custom",
) -> DbStressReport:
    resolved = _resolved_path(path)
    db_existed_before = resolved.exists()
    run_id = f"stress-{uuid.uuid4().hex[:12]}"
    table_existed_before, previous_journal_mode = _init(resolved)
    errors: list[str] = []
    started = time.monotonic()
    actual = 0
    table_removed = False
    residual_rows = -1
    journal_mode_restored = False

    try:
        threads = [
            Thread(
                target=_worker,
                args=(resolved,),
                kwargs={
                    "run_id": run_id,
                    "worker_id": worker_id,
                    "iterations": iterations,
                    "errors": errors,
                },
            )
            for worker_id in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        with closing(_connect(resolved)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM stress_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            actual = int(row[0]) if row else 0
    finally:
        table_removed, residual_rows = _cleanup_run_rows(
            resolved,
            run_id=run_id,
            table_existed_before=table_existed_before,
            keep_rows=keep_rows,
            errors=errors,
        )
        try:
            journal_mode_restored = _restore_journal_mode(
                resolved,
                previous_journal_mode,
            )
        except (sqlite3.Error, OSError):
            journal_mode_restored = False
        if not journal_mode_restored:
            errors.append("cleanup:journal_mode_restore_failed")

    elapsed = time.monotonic() - started
    expected = int(workers) * int(iterations)
    if keep_rows:
        cleanup_status = "rows_kept"
    elif table_removed:
        cleanup_status = "run_rows_and_created_table_removed"
    else:
        cleanup_status = "run_rows_removed"
    residual_ok = residual_rows == actual if keep_rows else residual_rows == 0
    return DbStressReport(
        ok=not errors and actual == expected and residual_ok and journal_mode_restored,
        db_path=str(resolved),
        target_kind=str(target_kind),
        workers=int(workers),
        iterations=int(iterations),
        expected_rows=expected,
        actual_rows=actual,
        residual_rows=residual_rows,
        elapsed_sec=round(elapsed, 3),
        db_existed_before=db_existed_before,
        table_existed_before=table_existed_before,
        table_removed=table_removed,
        journal_mode_restored=journal_mode_restored,
        cleanup_status=cleanup_status,
        errors=tuple(errors),
    )


def _error_payload(code: str, *, mutated: bool = False) -> dict[str, object]:
    return {
        "ok": False,
        "target_kind": "rejected" if not mutated else "failed",
        "mutated": bool(mutated),
        "error_code": str(code or "stress_db_failed"),
    }


def _bounded_positive(value: int, *, maximum: int) -> int:
    return max(1, min(int(value), int(maximum)))


def _print_runtime_failure(exc: BaseException) -> int:
    print(
        json.dumps(
            _error_payload(
                f"stress_run_failed:{type(exc).__name__}",
                mutated=True,
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a guarded SQLite concurrency diagnostic"
    )
    parser.add_argument(
        "--db-path",
        default="",
        help="Custom SQLite target; blocked without explicit authorization",
    )
    parser.add_argument("--allow-custom-db-path", action="store_true")
    parser.add_argument("--allow-existing-db-path", action="store_true")
    parser.add_argument("--allow-configured-db-path", action="store_true")
    parser.add_argument("--confirm-configured-db-path", default="")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=250)
    parser.add_argument("--keep-rows", action="store_true")
    parser.add_argument("--keep-temporary-db", action="store_true")
    args = parser.parse_args()

    temporary_dir: Path | None = None
    explicit_path = bool(str(args.db_path or "").strip())
    try:
        if explicit_path:
            target = _authorize_custom_target(
                Path(str(args.db_path)),
                allow_custom=bool(args.allow_custom_db_path),
                allow_existing=bool(args.allow_existing_db_path),
                allow_configured=bool(args.allow_configured_db_path),
                configured_confirmation=str(args.confirm_configured_db_path or ""),
            )
            target_kind = "custom"
        else:
            temporary_dir = Path(tempfile.mkdtemp(prefix="clientplatform_stress_"))
            target = temporary_dir / "stress.db"
            target_kind = "temporary"

        report = run(
            target,
            workers=_bounded_positive(args.workers, maximum=64),
            iterations=_bounded_positive(args.iterations, maximum=100_000),
            keep_rows=bool(args.keep_rows),
            target_kind=target_kind,
        )
    except DbStressSafetyError as exc:
        print(
            json.dumps(
                _error_payload(exc.code, mutated=False),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    except sqlite3.Error as exc:
        return _print_runtime_failure(exc)
    except OSError as exc:
        return _print_runtime_failure(exc)
    except ValueError as exc:
        return _print_runtime_failure(exc)
    except TypeError as exc:
        return _print_runtime_failure(exc)
    finally:
        if temporary_dir is not None and not bool(args.keep_temporary_db):
            shutil.rmtree(temporary_dir, ignore_errors=True)

    if temporary_dir is not None:
        suffix = ":temporary_db_kept" if args.keep_temporary_db else ":temporary_db_removed"
        report = replace(report, cleanup_status=report.cleanup_status + suffix)
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
