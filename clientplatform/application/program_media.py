from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from clientplatform.domain.programs import ContentKind
from clientplatform.infrastructure.program_media_store import (
    ProgramMediaStore,
    ProgramMediaStoreError,
    StoredProgramMedia,
    program_media_store_config,
)


@dataclass(frozen=True, slots=True)
class ProgramMediaIngestPolicy:
    enabled: bool
    max_bytes: int
    timeout_seconds: float


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
    config = program_media_store_config()
    return ProgramMediaStore(config).put_file(
        path,
        business_id=business_id,
        content_kind=content_kind,
        content_type=content_type,
        extension=extension,
    )


__all__ = [
    "ProgramMediaIngestPolicy",
    "ProgramMediaStoreError",
    "StoredProgramMedia",
    "program_media_ingest_policy",
    "store_program_media",
]
