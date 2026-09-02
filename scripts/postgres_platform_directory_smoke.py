from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clientplatform.application import platform_directory as directory
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import db
from services.db.runtime import CONFIG
from services.schema import init_db


def _enabled(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _guard() -> None:
    if not _enabled("POSTGRES_PLATFORM_DIRECTORY_SMOKE"):
        raise SystemExit("POSTGRES_PLATFORM_DIRECTORY_SMOKE_FAILED explicit guard is required")
    if (os.getenv("APP_ENV") or "").strip().lower() in {"prod", "production"}:
        raise SystemExit("POSTGRES_PLATFORM_DIRECTORY_SMOKE_FAILED refuses production")
    if not CONFIG.uses_postgres:
        raise SystemExit("POSTGRES_PLATFORM_DIRECTORY_SMOKE_FAILED engine is not Postgres")


def _exercise_directory() -> None:
    suffix = uuid.uuid4().hex[:10]
    operator_user_id = 9_004_000_000 + (uuid.uuid4().int % 100_000_000)
    shared_user_id = 8_004_000_000 + (uuid.uuid4().int % 100_000_000)
    base = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    business_ids: list[str] = []

    with db() as conn:
        tenancy = TenancyRepository(conn)
        first = tenancy.create_business(
            owner_user_id=shared_user_id + 1,
            name=f"Pg % Studio {suffix}",
            now=base.isoformat(),
        )
        second = tenancy.create_business(
            owner_user_id=shared_user_id + 2,
            name=f"Pg Percent Studio {suffix}",
            now=(base + timedelta(minutes=1)).isoformat(),
        )
        business_ids.extend((first.business.id, second.business.id))
        for access in (first, second):
            actor = tenancy.resolve_context(
                user_id=access.business.created_by_user_id,
                business_id=access.business.id,
            )
            tenancy.grant_member(actor=actor, user_id=shared_user_id, role="support")
        for index in range(25):
            tenancy.create_business(
                owner_user_id=shared_user_id + 100 + index,
                name=f"Pg Directory {suffix} {index:02d}",
                now=(base + timedelta(minutes=10 + index)).isoformat(),
            )
        before_memberships = int(conn.execute("SELECT COUNT(*) AS n FROM business_members").fetchone()["n"])
    original_auth = directory.is_platform_admin
    directory.is_platform_admin = lambda user_id: user_id == operator_user_id
    try:
        exact = directory.search_platform_directory(
            operator_user_id,
            query_kind="business_id",
            query=business_ids[0],
            now_utc=base + timedelta(hours=1),
        )
        by_user = directory.search_platform_directory(
            operator_user_id,
            query_kind="user_id",
            query=shared_user_id,
            now_utc=base + timedelta(hours=1, seconds=1),
        )
        literal = directory.search_platform_directory(
            operator_user_id,
            query_kind="business_name",
            query=f"Pg % Studio {suffix}",
            limit=20,
            now_utc=base + timedelta(hours=1, seconds=2),
        )
    finally:
        directory.is_platform_admin = original_auth

    if [item.business_id for item in exact.matches] != [business_ids[0]]:
        raise SystemExit("POSTGRES_PLATFORM_DIRECTORY_SMOKE_FAILED exact business lookup")
    if [item.business_id for item in by_user.matches] != business_ids:
        raise SystemExit("POSTGRES_PLATFORM_DIRECTORY_SMOKE_FAILED user lookup ordering")
    if [item.business_id for item in literal.matches] != [business_ids[0]]:
        raise SystemExit("POSTGRES_PLATFORM_DIRECTORY_SMOKE_FAILED literal LIKE escaping")
    directory.is_platform_admin = lambda user_id: user_id == operator_user_id
    try:
        capped = directory.search_platform_directory(
            operator_user_id,
            query_kind="business_name",
            query=f"Pg Directory {suffix}",
            limit=20,
            now_utc=base + timedelta(hours=1, seconds=3),
        )
    finally:
        directory.is_platform_admin = original_auth
    if len(capped.matches) != 20:
        raise SystemExit("POSTGRES_PLATFORM_DIRECTORY_SMOKE_FAILED hard cap")
    if [item.business_name for item in capped.matches] != [f"Pg Directory {suffix} {index:02d}" for index in range(20)]:
        raise SystemExit("POSTGRES_PLATFORM_DIRECTORY_SMOKE_FAILED deterministic ordering")

    with db() as conn:
        after_memberships = int(conn.execute("SELECT COUNT(*) AS n FROM business_members").fetchone()["n"])
        audit_count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM clientplatform_platform_operator_audit_events WHERE operator_user_id=?",
                (operator_user_id,),
            ).fetchone()["n"]
        )
    if after_memberships != before_memberships:
        raise SystemExit("POSTGRES_PLATFORM_DIRECTORY_SMOKE_FAILED membership mutation")
    if audit_count != 4:
        raise SystemExit("POSTGRES_PLATFORM_DIRECTORY_SMOKE_FAILED audit evidence")


def main() -> int:
    _guard()
    init_db()
    _exercise_directory()
    print("POSTGRES_PLATFORM_DIRECTORY_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
