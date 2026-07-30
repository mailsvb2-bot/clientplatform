from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.programs import (
    Program,
    ProgramInvariantViolation,
    ProgramNotFound,
    ProgramRecord,
    ProgramStatus,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.program_repository import ProgramRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProgramDraftRepository:
    """Owner-only lifecycle operations for persistent program drafts."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)
        self._programs = ProgramRepository(conn)

    def _resolve_manager(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_programs()
        return current

    def list_drafts(self, *, actor: TenantContext) -> list[Program]:
        current = self._resolve_manager(actor)
        return [
            program
            for program in self._programs.list_programs(actor=current)
            if program.status == ProgramStatus.DRAFT
        ]

    def get_draft(
        self,
        *,
        actor: TenantContext,
        program_id: str,
    ) -> ProgramRecord:
        current = self._resolve_manager(actor)
        record = self._programs.get_program(
            actor=current,
            program_id=program_id,
        )
        if record.program.status != ProgramStatus.DRAFT:
            raise ProgramInvariantViolation("only a draft program can be edited")
        return record

    def archive_draft(
        self,
        *,
        actor: TenantContext,
        program_id: str,
        now: str | None = None,
    ) -> Program:
        current = self._resolve_manager(actor)
        normalized_program_id = normalize_uuid(
            program_id,
            field_name="program_id",
        )
        timestamp = str(now or _utc_now())

        cursor = self._conn.execute(
            """
            UPDATE programs
            SET updated_at=updated_at
            WHERE id=? AND business_id=? AND status!='archived'
            """,
            (normalized_program_id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ProgramNotFound(
                "program was not found in the active business"
            )

        record = self._programs.get_program(
            actor=current,
            program_id=normalized_program_id,
        )
        if record.program.status != ProgramStatus.DRAFT:
            raise ProgramInvariantViolation("only a draft program can be archived")

        self._conn.execute(
            """
            UPDATE lessons
            SET status='archived', archived_at=?, updated_at=?
            WHERE business_id=? AND program_id=? AND status='active'
            """,
            (
                timestamp,
                timestamp,
                current.business_id,
                normalized_program_id,
            ),
        )
        self._conn.execute(
            """
            UPDATE programs
            SET status='archived', archived_at=?, updated_at=?
            WHERE id=? AND business_id=? AND status='draft'
            """,
            (
                timestamp,
                timestamp,
                normalized_program_id,
                current.business_id,
            ),
        )
        return self._programs.get_program(
            actor=current,
            program_id=normalized_program_id,
        ).program


__all__ = ["ProgramDraftRepository"]
