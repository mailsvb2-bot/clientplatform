from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from core.paths import ROOT as PROJECT_ROOT
from services.db import DB_PATH, get_connection
from services.validators.base import ValidationError

log = logging.getLogger(__name__)

EXCLUDED_SCAN_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".env",
    "site-packages",
    "dist-packages",
    "node_modules",
    "build",
    "dist",
    "logs",
}


def _is_excluded_scan_path(path: Path, project_root: Path) -> bool:
    try:
        rel = path.relative_to(project_root)
    except ValueError:
        rel = path
    return bool(set(rel.parts) & EXCLUDED_SCAN_DIR_NAMES)


def validate_no_real_db(*, strict: bool = True) -> None:
    """Never ship a stateful SQLite database in the source/release."""

    candidates = [
        PROJECT_ROOT / "data.db",
        PROJECT_ROOT / "data" / "data.db",
    ]
    found: list[Path] = []
    for path in candidates:
        try:
            exists = path.exists()
        except OSError:
            # An unreadable candidate is still suspicious and must be surfaced by
            # the strict release gate instead of crashing before a useful report.
            exists = True
        if exists and not path.name.endswith(".template"):
            found.append(path)
    if not found:
        return

    details: list[str] = []
    for path in found:
        if "services/validators/" in str(path).replace("\\", "/"):
            continue
        if "services/validators" in path.as_posix():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = -1
        details.append(f"{path} (size={size})")

    if not details:
        return
    if strict:
        raise ValidationError(
            "Release hygiene failed: stateful DB file(s) must not be shipped: "
            + ", ".join(details)
        )
    log.warning(
        "Release hygiene warning: stateful DB file(s) present (remove before release): %s",
        ", ".join(details),
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def validate_db_schema(strict: bool = True) -> None:
    required_tables = {
        "users",
        "plans",
        "plan_price_history",
        "payments",
        "telegram_stars_refunds",
        "yookassa_refunds",
        "selected_plan",
        "mood_sessions",
    }

    with get_connection() as conn:
        existing = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = sorted(required_tables - existing)
        if missing:
            message = f"DB schema missing required tables: {missing}. DB: {DB_PATH}"
            if strict:
                raise ValidationError(message)
            log.warning(message)

        if "payments" in existing:
            columns = _table_columns(conn, "payments")
            required_columns = {
                "id",
                "user_id",
                "payload",
                "amount",
                "currency",
                "created_at",
            }
            missing_columns = sorted(required_columns - columns)
            if missing_columns:
                message = (
                    "DB schema table payments missing columns: "
                    f"{missing_columns}. Existing: {sorted(columns)}"
                )
                if strict:
                    raise ValidationError(message)
                log.warning(message)


def validate_schema_decomposition(strict: bool = True) -> None:
    """Forbid runtime schema ownership and migration-ledger mutations.

    Reading schema metadata is allowed. Runtime files may not import the canonical
    schema builder, execute DDL, or write to the migration ledger. The previous
    scanner rejected any literal mention of the migration table, which falsely
    blocked read-only schema validation such as the privacy manifest.
    """

    base_dir = PROJECT_ROOT
    allow_importers = {"services/schema_core.py", "services/schema_tables.py"}
    import_re = re.compile(
        r"^\s*(from\s+services\.schema_tables\s+import\b|import\s+services\.schema_tables\b)"
    )
    bad_imports: list[str] = []
    for path in base_dir.rglob("*.py"):
        rel = str(path.relative_to(base_dir)).replace("\\", "/")
        if _is_excluded_scan_path(path, base_dir):
            continue
        if rel.startswith("services/validators/") or rel.startswith("tests/"):
            continue
        if rel in allow_importers:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if import_re.search(line):
                bad_imports.append(f"{rel}:{line_number}")
                break

    if bad_imports:
        message = (
            "Forbidden import of schema_tables (only schema_core may import it): "
            + ", ".join(bad_imports[:30])
        )
        if strict:
            raise ValidationError(message)
        log.warning(message)

    allow_sql = {
        "services/schema_core.py",
        "services/schema_tables.py",
        "services/validator.py",
        "services/db/core.py",
        # visual_gateway is a separately built/deployed service with its own
        # isolated VISUAL_GATEWAY_STATE_DIR. Its embedded SQLite schema is not
        # ClientPlatform application DB ownership and remains covered by the
        # dedicated Visual Gateway contract/image-build workflow.
        "visual_gateway/server.py",
    }
    allow_sql_prefixes = ("services/db/schema/",)
    ddl_re = re.compile(
        r"\b(?:CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)\b",
        re.IGNORECASE,
    )
    migration_write_re = re.compile(
        r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+schema_migrations\b",
        re.IGNORECASE,
    )
    bad_sql: list[str] = []
    for path in base_dir.rglob("*.py"):
        rel = str(path.relative_to(base_dir)).replace("\\", "/")
        if _is_excluded_scan_path(path, base_dir):
            continue
        if rel.startswith("services/validators/") or rel.startswith("tests/"):
            continue
        if (
            rel in allow_sql
            or rel.startswith("services/migrations/")
            or rel.startswith(allow_sql_prefixes)
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if ddl_re.search(text) or migration_write_re.search(text):
            bad_sql.append(rel)

    if bad_sql:
        message = f"Forbidden migration/DDL SQL in runtime modules: {sorted(set(bad_sql))}"
        if strict:
            raise ValidationError(message)
        log.warning(message)
