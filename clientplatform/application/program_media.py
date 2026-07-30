from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from clientplatform.domain.program_media import unwrap_program_media_reference
from clientplatform.domain.programs import ContentKind, normalize_content_ref
from clientplatform.infrastructure.program_media_cleanup import (
    delete_program_media_reference,
)
from clientplatform.infrastructure.program_media_cleanup_repository import (
    ProgramMediaCleanupRepository,
)
from clientplatform.infrastructure.program_media_store import (
    ProgramMediaStore,
    ProgramMediaStoreError,
    StoredProgramMedia,
    program_media_store_config,
)
from services.db import get_db


@dataclass(frozen=True, slots=True)
class ProgramMediaIngestPolicy:
    enabled: bool
    max_bytes: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ProgramMediaCleanupBatchResult:
    claimed: int
    deleted: int
    retained: int
    retried: int
    dead: int


def program_media_ingest_policy() -> ProgramMediaIngestPolicy:
    config = program_media_store_config()
    return ProgramMediaIngestPolicy(
        enabled=config.enabled,
        max_bytes=config.max_bytes,
        timeout_seconds=config.timeout_seconds,
    )


def store_program_media(
    path: Path,
    *,
    business_id: str,
    content_kind: ContentKind,
    content_type: str,
    extension: str,
) -> StoredProgramMedia:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ProgramMediaStoreError("program_media_source_invalid")
    config = program_media_store_config()
    return ProgramMediaStore(config).put_file(
        candidate,
        business_id=business_id,
        content_kind=content_kind,
        content_type=content_type,
        extension=extension,
    )


def is_private_program_media_reference(reference: str) -> bool:
    try:
        normalized = normalize_content_ref(reference)
    except ValueError:
        return False
    return unwrap_program_media_reference(normalized).startswith("s3://")


def stage_program_media_cleanup(
    *,
    business_id: str,
    media_reference: str,
    reason: str,
    delay_seconds: int = 600,
) -> bool:
    if not is_private_program_media_reference(media_reference):
        return False
    with get_db() as conn:
        ProgramMediaCleanupRepository(conn).enqueue(
            business_id=business_id,
            media_reference=media_reference,
            reason=reason,
            delay_seconds=delay_seconds,
        )
    return True


def cancel_program_media_cleanup(*, media_reference: str) -> bool:
    if not is_private_program_media_reference(media_reference):
        return False
    with get_db() as conn:
        return ProgramMediaCleanupRepository(conn).discard(
            media_reference=media_reference
        )


def queue_program_media_cleanup(
    *,
    business_id: str,
    media_reference: str,
    reason: str,
) -> bool:
    return stage_program_media_cleanup(
        business_id=business_id,
        media_reference=media_reference,
        reason=reason,
        delay_seconds=0,
    )


def run_program_media_cleanup_batch(
    *,
    limit: int = 10,
    max_attempts: int = 8,
    lock_ttl_seconds: int = 900,
) -> ProgramMediaCleanupBatchResult:
    with get_db() as conn:
        jobs = ProgramMediaCleanupRepository(conn).claim_due(
            limit=limit,
            lock_ttl_seconds=lock_ttl_seconds,
        )

    deleted = 0
    retained = 0
    retried = 0
    dead = 0
    for job in jobs:
        with get_db() as conn:
            repository = ProgramMediaCleanupRepository(conn)
            if repository.is_referenced(media_reference=job.media_reference):
                repository.complete(job)
                retained += 1
                continue

        try:
            delete_program_media_reference(job.media_reference)
        except ProgramMediaStoreError as exc:
            with get_db() as conn:
                updated = ProgramMediaCleanupRepository(conn).reschedule(
                    job,
                    error=exc.code,
                    max_attempts=max_attempts,
                )
            if updated.status == "dead":
                dead += 1
            else:
                retried += 1
            continue

        with get_db() as conn:
            ProgramMediaCleanupRepository(conn).complete(job)
        deleted += 1

    return ProgramMediaCleanupBatchResult(
        claimed=len(jobs),
        deleted=deleted,
        retained=retained,
        retried=retried,
        dead=dead,
    )


__all__ = [
    "ProgramMediaCleanupBatchResult",
    "ProgramMediaIngestPolicy",
    "ProgramMediaStoreError",
    "StoredProgramMedia",
    "cancel_program_media_cleanup",
    "is_private_program_media_reference",
    "program_media_ingest_policy",
    "queue_program_media_cleanup",
    "run_program_media_cleanup_batch",
    "stage_program_media_cleanup",
    "store_program_media",
]
