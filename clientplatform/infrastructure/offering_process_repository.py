from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from clientplatform.domain.offering_process import (
    BusinessOfferingProcess,
    OfferingProcessError,
    OfferingProcessNotFound,
    OfferingProcessState,
    default_ai_profile,
    default_process_mechanics,
    encode_process_mapping,
)
from clientplatform.domain.tenancy import normalize_uuid

_PROCESS_RETENTION_DAYS = 7


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _normalize_timestamp(value: str | None) -> str:
    raw = str(value or _utc_now()).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OfferingProcessError("offering process timestamp is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _retention_deadline(now: str) -> str:
    parsed = datetime.fromisoformat(now)
    return (parsed + timedelta(days=_PROCESS_RETENTION_DAYS)).isoformat(timespec="seconds")


def _process_from_row(row: Any) -> BusinessOfferingProcess:
    return BusinessOfferingProcess(
        offering_id=str(_value(row, "offering_id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        state=OfferingProcessState(str(_value(row, "state", 2))),
        revision=int(_value(row, "revision", 3)),
        mechanics_json=str(_value(row, "mechanics_json", 4)),
        ai_profile_json=str(_value(row, "ai_profile_json", 5)),
        created_by_member_id=str(_value(row, "created_by_member_id", 6)),
        created_at=str(_value(row, "created_at", 7)),
        updated_at=str(_value(row, "updated_at", 8)),
        frozen_at=(
            None if _value(row, "frozen_at", 9) is None else str(_value(row, "frozen_at", 9))
        ),
        purge_after=(
            None if _value(row, "purge_after", 10) is None else str(_value(row, "purge_after", 10))
        ),
        purged_at=(
            None if _value(row, "purged_at", 11) is None else str(_value(row, "purged_at", 11))
        ),
    )


class OfferingProcessRepository:
    """Storage for rebuildable mechanics attached 1:1 to BusinessOffering."""

    def __init__(self, conn: Any):
        self._conn = conn

    @staticmethod
    def retention_days() -> int:
        return _PROCESS_RETENTION_DAYS

    def get(self, *, business_id: str, offering_id: str) -> BusinessOfferingProcess:
        normalized_business_id = normalize_uuid(business_id, field_name="business_id")
        normalized_offering_id = normalize_uuid(offering_id, field_name="offering_id")
        row = self._conn.execute(
            """
            SELECT offering_id, business_id, state, revision, mechanics_json,
                   ai_profile_json, created_by_member_id, created_at, updated_at,
                   frozen_at, purge_after, purged_at
            FROM business_offering_processes
            WHERE offering_id=? AND business_id=?
            LIMIT 1
            """,
            (normalized_offering_id, normalized_business_id),
        ).fetchone()
        if row is None:
            raise OfferingProcessNotFound("business offering process was not found")
        return _process_from_row(row)

    def ensure(
        self,
        *,
        business_id: str,
        offering_id: str,
        created_by_member_id: str,
        now: str | None = None,
    ) -> BusinessOfferingProcess:
        normalized_business_id = normalize_uuid(business_id, field_name="business_id")
        normalized_offering_id = normalize_uuid(offering_id, field_name="offering_id")
        normalized_member_id = normalize_uuid(
            created_by_member_id,
            field_name="created_by_member_id",
        )
        try:
            return self.get(
                business_id=normalized_business_id,
                offering_id=normalized_offering_id,
            )
        except OfferingProcessNotFound:
            pass
        timestamp = _normalize_timestamp(now)
        mechanics_json = encode_process_mapping(
            default_process_mechanics(),
            field_name="offering process mechanics",
        )
        ai_profile_json = encode_process_mapping(
            default_ai_profile(),
            field_name="offering AI profile",
        )
        self._conn.execute(
            """
            INSERT INTO business_offering_processes(
                offering_id, business_id, state, revision, mechanics_json,
                ai_profile_json, created_by_member_id, created_at, updated_at,
                frozen_at, purge_after, purged_at
            ) VALUES(?, ?, 'active', 1, ?, ?, ?, ?, ?, NULL, NULL, NULL)
            ON CONFLICT(offering_id, business_id) DO NOTHING
            """,
            (
                normalized_offering_id,
                normalized_business_id,
                mechanics_json,
                ai_profile_json,
                normalized_member_id,
                timestamp,
                timestamp,
            ),
        )
        return self.get(
            business_id=normalized_business_id,
            offering_id=normalized_offering_id,
        )

    def configure(
        self,
        *,
        business_id: str,
        offering_id: str,
        mechanics: Mapping[str, object],
        ai_profile: Mapping[str, object],
        now: str | None = None,
    ) -> BusinessOfferingProcess:
        current = self.get(business_id=business_id, offering_id=offering_id)
        if current.state != OfferingProcessState.ACTIVE:
            raise OfferingProcessError("only an active offering process can be configured")
        timestamp = _normalize_timestamp(now)
        mechanics_json = encode_process_mapping(
            mechanics,
            field_name="offering process mechanics",
        )
        ai_profile_json = encode_process_mapping(
            ai_profile,
            field_name="offering AI profile",
        )
        cursor = self._conn.execute(
            """
            UPDATE business_offering_processes
            SET mechanics_json=?, ai_profile_json=?, revision=revision+1, updated_at=?
            WHERE offering_id=? AND business_id=? AND state='active' AND revision=?
            """,
            (
                mechanics_json,
                ai_profile_json,
                timestamp,
                current.offering_id,
                current.business_id,
                current.revision,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise OfferingProcessError("offering process changed concurrently; refresh and retry")
        return self.get(business_id=current.business_id, offering_id=current.offering_id)

    def freeze(
        self,
        *,
        business_id: str,
        offering_id: str,
        now: str | None = None,
    ) -> BusinessOfferingProcess:
        current = self.get(business_id=business_id, offering_id=offering_id)
        if current.state != OfferingProcessState.ACTIVE:
            return current
        timestamp = _normalize_timestamp(now)
        purge_after = _retention_deadline(timestamp)
        cursor = self._conn.execute(
            """
            UPDATE business_offering_processes
            SET state='frozen', revision=revision+1, frozen_at=?, purge_after=?,
                purged_at=NULL, updated_at=?
            WHERE offering_id=? AND business_id=? AND state='active' AND revision=?
            """,
            (
                timestamp,
                purge_after,
                timestamp,
                current.offering_id,
                current.business_id,
                current.revision,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            latest = self.get(business_id=current.business_id, offering_id=current.offering_id)
            if latest.state != OfferingProcessState.ACTIVE:
                return latest
            raise OfferingProcessError("offering process changed concurrently; refresh and retry")
        return self.get(business_id=current.business_id, offering_id=current.offering_id)

    def restore_or_rebuild(
        self,
        *,
        business_id: str,
        offering_id: str,
        created_by_member_id: str,
        now: str | None = None,
    ) -> tuple[BusinessOfferingProcess, bool]:
        timestamp = _normalize_timestamp(now)
        try:
            current = self.get(business_id=business_id, offering_id=offering_id)
        except OfferingProcessNotFound:
            created = self.ensure(
                business_id=business_id,
                offering_id=offering_id,
                created_by_member_id=created_by_member_id,
                now=timestamp,
            )
            return created, True
        if current.state == OfferingProcessState.ACTIVE:
            return current, False

        rebuild = current.state == OfferingProcessState.PURGED
        if current.state == OfferingProcessState.FROZEN:
            if current.purge_after is None:
                raise OfferingProcessError("frozen offering process has no purge deadline")
            rebuild = timestamp >= _normalize_timestamp(current.purge_after)

        mechanics_json = current.mechanics_json
        ai_profile_json = current.ai_profile_json
        if rebuild:
            mechanics_json = encode_process_mapping(
                default_process_mechanics(),
                field_name="offering process mechanics",
            )
            ai_profile_json = encode_process_mapping(
                default_ai_profile(),
                field_name="offering AI profile",
            )
        cursor = self._conn.execute(
            """
            UPDATE business_offering_processes
            SET state='active', revision=revision+1, mechanics_json=?, ai_profile_json=?,
                frozen_at=NULL, purge_after=NULL, purged_at=NULL, updated_at=?
            WHERE offering_id=? AND business_id=? AND state=? AND revision=?
            """,
            (
                mechanics_json,
                ai_profile_json,
                timestamp,
                current.offering_id,
                current.business_id,
                current.state.value,
                current.revision,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            latest = self.get(business_id=current.business_id, offering_id=current.offering_id)
            if latest.state == OfferingProcessState.ACTIVE:
                return latest, False
            raise OfferingProcessError("offering process changed concurrently; refresh and retry")
        return (
            self.get(business_id=current.business_id, offering_id=current.offering_id),
            rebuild,
        )

    def purge_due(self, *, now: str | None = None, limit: int = 100) -> int:
        selected_limit = int(limit)
        if selected_limit < 1 or selected_limit > 1000:
            raise ValueError("offering process purge limit must be between 1 and 1000")
        timestamp = _normalize_timestamp(now)
        rows = self._conn.execute(
            """
            SELECT offering_id, business_id
            FROM business_offering_processes
            WHERE state='frozen' AND purge_after<=?
            ORDER BY purge_after, business_id, offering_id
            LIMIT ?
            """,
            (timestamp, selected_limit),
        ).fetchall()
        purged = 0
        for row in rows:
            offering_id = str(_value(row, "offering_id", 0))
            business_id = str(_value(row, "business_id", 1))
            cursor = self._conn.execute(
                """
                UPDATE business_offering_processes
                SET state='purged', revision=revision+1,
                    mechanics_json='{}', ai_profile_json='{}',
                    purged_at=?, updated_at=?
                WHERE offering_id=? AND business_id=?
                  AND state='frozen' AND purge_after<=?
                  AND EXISTS(
                      SELECT 1 FROM business_offerings bo
                      WHERE bo.id=business_offering_processes.offering_id
                        AND bo.business_id=business_offering_processes.business_id
                        AND bo.status='archived'
                  )
                """,
                (timestamp, timestamp, offering_id, business_id, timestamp),
            )
            purged += int(getattr(cursor, "rowcount", 0) or 0)
        return purged


__all__ = ["OfferingProcessRepository"]
