from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.program_draft_repository import ProgramDraftRepository
from clientplatform.infrastructure.program_repository import ProgramRepository
from services.db import get_connection, get_db
from services.db.core import PostgresCompatConnection
from services.db.runtime import CONFIG
from services.schema import init_db


class _SynchronizedConnection(PostgresCompatConnection):
    """Release both workers immediately before the shared draft row lock."""

    def __init__(self, delegate: Any, *, lock_gate: threading.Barrier) -> None:
        self._delegate = delegate
        self._lock_gate = lock_gate
        self._lock_seen = False

    def execute(self, sql: str, params: Any = ()) -> Any:
        compact = " ".join(str(sql).lower().split())
        if not self._lock_seen and (
            "update programs" in compact
            and "set updated_at=updated_at" in compact
        ):
            self._lock_gate.wait(timeout=15)
            self._lock_seen = True
        return self._delegate.execute(sql, params)


def _run_pair(workers: tuple[Callable[[], str], Callable[[], str]]) -> list[str]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for worker in workers]
        return [future.result(timeout=30) for future in futures]


def _cleanup_business(business_id: str) -> None:
    with get_db() as conn:
        for table in (
            "lesson_progress",
            "lesson_deliveries",
            "enrollments",
            "lessons",
            "programs",
            "business_members",
        ):
            conn.execute(f"DELETE FROM {table} WHERE business_id=?", (business_id,))
        conn.execute("DELETE FROM businesses WHERE id=?", (business_id,))


def main() -> int:
    if not CONFIG.uses_postgres:
        raise SystemExit(
            "POSTGRES_PROGRAM_DRAFT_LESSON_CONCURRENCY_FAILED: "
            "CLIENTPLATFORM_DB_ENGINE=postgres is required"
        )
    if not (os.getenv("DATABASE_URL") or "").strip():
        raise SystemExit(
            "POSTGRES_PROGRAM_DRAFT_LESSON_CONCURRENCY_FAILED: DATABASE_URL is required"
        )
    if (os.getenv("POSTGRES_REUSE_CONNECTIONS") or "").strip() != "0":
        raise SystemExit(
            "POSTGRES_PROGRAM_DRAFT_LESSON_CONCURRENCY_FAILED: "
            "POSTGRES_REUSE_CONNECTIONS=0 is required to prove independent connections"
        )

    init_db()
    suffix = uuid.uuid4().hex[:12]
    owner_user_id = 9_500_000_000 + int(suffix[:6], 16)
    business_id = ""
    try:
        with get_db() as conn:
            tenancy = TenancyRepository(conn)
            programs = ProgramRepository(conn)
            business = tenancy.create_business(
                owner_user_id=owner_user_id,
                name=f"Draft lesson concurrency {suffix}",
            )
            business_id = business.business.id
            owner = tenancy.resolve_context(
                user_id=owner_user_id,
                business_id=business_id,
            )
            program = programs.create_program(
                actor=owner,
                title="Concurrent draft",
            )
            lessons = [
                programs.add_lesson(
                    actor=owner,
                    program_id=program.id,
                    title=f"Lesson {index}",
                    content_kind="text",
                    content_ref=f"Material {index}",
                )
                for index in range(1, 4)
            ]

        lock_gate = threading.Barrier(2)

        def move_third_up() -> str:
            with get_connection() as raw:
                conn = _SynchronizedConnection(raw, lock_gate=lock_gate)
                ProgramDraftRepository(conn).move_lesson(
                    actor=owner,
                    lesson_id=lessons[2].id,
                    direction="up",
                    now="2026-07-30T14:10:00+00:00",
                )
            return "moved"

        def archive_second() -> str:
            with get_connection() as raw:
                conn = _SynchronizedConnection(raw, lock_gate=lock_gate)
                ProgramDraftRepository(conn).archive_lesson(
                    actor=owner,
                    lesson_id=lessons[1].id,
                    now="2026-07-30T14:10:01+00:00",
                )
            return "archived"

        results = _run_pair((move_third_up, archive_second))

        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT id, position, status
                FROM lessons
                WHERE business_id=? AND program_id=?
                ORDER BY position, id
                """,
                (business_id, program.id),
            ).fetchall()
            program_status = str(
                conn.execute(
                    "SELECT status FROM programs WHERE id=? AND business_id=?",
                    (program.id, business_id),
                ).fetchone()["status"]
            )

        active = [row for row in rows if str(row["status"]) == "active"]
        archived = [row for row in rows if str(row["status"]) == "archived"]
        assert sorted(results) == ["archived", "moved"], results
        assert program_status == "draft", program_status
        assert [int(row["position"]) for row in active] == [1, 2], active
        assert {str(row["id"]) for row in active} == {
            lessons[0].id,
            lessons[2].id,
        }, active
        assert len(archived) == 1, archived
        assert str(archived[0]["id"]) == lessons[1].id, archived
        assert int(archived[0]["position"]) > 2, archived
        assert len({int(row["position"]) for row in rows}) == len(rows), rows

        print(
            json.dumps(
                {
                    "ok": True,
                    "probe": "clientplatform_postgres_program_draft_lesson_concurrency",
                    "connections": 2,
                    "operations": results,
                    "active_positions": [int(row["position"]) for row in active],
                    "archived_lessons": len(archived),
                },
                sort_keys=True,
            )
        )
        print("POSTGRES_PROGRAM_DRAFT_LESSON_CONCURRENCY_OK")
        return 0
    finally:
        if business_id:
            _cleanup_business(business_id)


if __name__ == "__main__":
    raise SystemExit(main())
