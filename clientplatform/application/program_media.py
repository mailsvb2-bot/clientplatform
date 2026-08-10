from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

try:
    from psycopg import Error as PostgresError
except ImportError:  # pragma: no cover - dependency-light boundary
    class PostgresError(Exception):
        """Fallback type used when the optional Postgres driver is absent."""


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


_UNCERTAIN_UPLOAD_CLEANUP_DELAY_SECONDS = 600


class ProgramMediaCleanupQueueError(RuntimeError):
    """Sanitized failure to durably schedule an uncertain media object."""

    def __init__(self, code: str = "program_media_cleanup_enqueue_failed") -> None:
        normalized = str(code or "program_media_cleanup_enqueue_failed").strip()[:120]
        super().__init__(normalized)
        self.code = normalized


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
    try:
        return ProgramMediaStore(config).put_file(
            candidate,
            business_id=business_id,
            content_kind=content_kind,
            content_type=content_type,
            extension=extension,
        )
    except ProgramMediaStoreError as exc:
        cleanup_reference = exc.cleanup_reference
        if cleanup_reference:
            # A PUT transport timeout is ambiguous: the client may have stopped
            # receiving while the gateway continues and commits the object. An
            # immediate DELETE can therefore race that completion, observe 404,
            # and permanently lose the orphan reference. The configured upload
            # timeout is capped at 120s; 600s gives the uncertain PUT a wide
            # completion window before cleanup begins. Post-PUT verification
            # failures remain immediate because upload completion is known.
            delay_seconds = (
                _UNCERTAIN_UPLOAD_CLEANUP_DELAY_SECONDS
                if exc.code == "program_media_upload_transport_failure"
                else 0
            )
            try:
                scheduled = queue_program_media_cleanup(
                    business_id=business_id,
                    media_reference=cleanup_reference,
                    reason="failed_program_media_ingest",
                    delay_seconds=delay_seconds,
                )
            except ProgramMediaCleanupQueueError:
                raise ProgramMediaStoreError(
                    "program_media_cleanup_enqueue_failed",
                    retryable=True,
                    cleanup_reference=cleanup_reference,
                ) from None
            if not scheduled:
                raise ProgramMediaStoreError(
                    "program_media_cleanup_enqueue_failed",
                    retryable=True,
                    cleanup_reference=cleanup_reference,
                ) from None
        raise


def is_private_program_media_reference(reference: str) -> bool:
    try:
        normalized = normalize_content_ref(reference)
    except ValueError:
        return False
    return unwrap_program_media_reference(normalized).startswith("s3://")


def _cleanup_enabled() -> bool:
    return program_media_store_config().enabled


def delete_uncommitted_program_media(*, media_reference: str) -> bool:
    if not _cleanup_enabled() or not is_private_program_media_reference(media_reference):
        return False
    return delete_program_media_reference(media_reference)


def stage_program_media_cleanup(
    *,
    business_id: str,
    media_reference: str,
    reason: str,
    delay_seconds: int = 600,
) -> bool:
    try:
        enabled = _cleanup_enabled()
    except ProgramMediaStoreError:
        raise ProgramMediaCleanupQueueError() from None
    if not enabled or not is_private_program_media_reference(media_reference):
        return False
    try:
        with get_db() as conn:
            ProgramMediaCleanupRepository(conn).enqueue(
                business_id=business_id,
                media_reference=media_reference,
                reason=reason,
                delay_seconds=delay_seconds,
            )
    except sqlite3.Error:
        raise ProgramMediaCleanupQueueError() from None
    except PostgresError:
        raise ProgramMediaCleanupQueueError() from None
    except OSError:
        raise ProgramMediaCleanupQueueError() from None
    except RuntimeError:
        raise ProgramMediaCleanupQueueError() from None
    return True


def cancel_program_media_cleanup(*, media_reference: str) -> bool:
    try:
        enabled = _cleanup_enabled()
    except ProgramMediaStoreError:
        raise ProgramMediaCleanupQueueError() from None
    if not enabled or not is_private_program_media_reference(media_reference):
        return False
    try:
        with get_db() as conn:
            return ProgramMediaCleanupRepository(conn).discard(
                media_reference=media_reference
            )
    except sqlite3.Error:
        raise ProgramMediaCleanupQueueError() from None
    except PostgresError:
        raise ProgramMediaCleanupQueueError() from None
    except OSError:
        raise ProgramMediaCleanupQueueError() from None
    except RuntimeError:
        raise ProgramMediaCleanupQueueError() from None


def queue_program_media_cleanup(
    *,
    business_id: str,
    media_reference: str,
    reason: str,
    delay_seconds: int = 0,
) -> bool:
    try:
        enabled = _cleanup_enabled()
    except ProgramMediaStoreError:
        raise ProgramMediaCleanupQueueError() from None
    if not enabled or not is_private_program_media_reference(media_reference):
        return False
    try:
        with get_db() as conn:
            repository = ProgramMediaCleanupRepository(conn)
            repository.discard(media_reference=media_reference)
            repository.enqueue(
                business_id=business_id,
                media_reference=media_reference,
                reason=reason,
                delay_seconds=max(0, int(delay_seconds)),
            )
    except sqlite3.Error:
        raise ProgramMediaCleanupQueueError() from None
    except PostgresError:
        raise ProgramMediaCleanupQueueError() from None
    except OSError:
        raise ProgramMediaCleanupQueueError() from None
    except RuntimeError:
        raise ProgramMediaCleanupQueueError() from None
    return True


def run_program_media_cleanup_batch(
    *,
    limit: int = 10,
    max_attempts: int = 8,
    lock_ttl_seconds: int = 900,
) -> ProgramMediaCleanupBatchResult:
    if not _cleanup_enabled():
        return ProgramMediaCleanupBatchResult(
            claimed=0,
            deleted=0,
            retained=0,
            retried=0,
            dead=0,
        )
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
    "ProgramMediaCleanupQueueError",
    "ProgramMediaIngestPolicy",
    "ProgramMediaStoreError",
    "StoredProgramMedia",
    "cancel_program_media_cleanup",
    "delete_uncommitted_program_media",
    "is_private_program_media_reference",
    "program_media_ingest_policy",
    "queue_program_media_cleanup",
    "run_program_media_cleanup_batch",
    "stage_program_media_cleanup",
    "store_program_media",
]
