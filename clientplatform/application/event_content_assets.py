from __future__ import annotations

import sqlite3
from pathlib import Path

try:
    from psycopg import Error as PostgresError
except ImportError:  # pragma: no cover - dependency-light boundary
    class PostgresError(Exception):
        pass

from clientplatform.application.program_media import (
    ProgramMediaCleanupQueueError,
    ProgramMediaStoreError,
    queue_program_media_cleanup,
    store_program_media,
)
from clientplatform.domain.event_content import EventContentAsset, EventContentStage
from clientplatform.domain.programs import ContentKind
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.event_content_repository import EventContentAssetRepository
from services.db import get_db, get_db_ro


class EventContentAssetError(RuntimeError):
    """Sanitized failure to persist or materialize an event visual asset."""


def get_event_content_asset(
    *,
    actor: TenantContext,
    event_id: str,
    stage: EventContentStage,
    slot_key: str,
) -> EventContentAsset | None:
    try:
        with get_db_ro() as conn:
            return EventContentAssetRepository(conn).get(
                actor=actor,
                event_id=event_id,
                stage=stage,
                slot_key=slot_key,
            )
    except sqlite3.Error:
        # Optional visual projection must not break legacy/partial-schema reads.
        return None


def _queue_replaced(reference: str, *, business_id: str, reason: str) -> None:
    try:
        queue_program_media_cleanup(
            business_id=business_id,
            media_reference=reference,
            reason=reason,
        )
    except (ProgramMediaCleanupQueueError, RuntimeError):
        # Replacement is already durable. Cleanup remains bounded best effort
        # and must not turn the successful owner action into a visible failure.
        return


def set_event_content_asset_reference(
    *,
    actor: TenantContext,
    event_id: str,
    stage: EventContentStage,
    slot_key: str,
    kind: ContentKind | str,
    media_reference: str,
    source: str,
    source_ref: str = "",
) -> EventContentAsset:
    actor.assert_can_manage_business()
    selected_kind = kind if isinstance(kind, ContentKind) else ContentKind(str(kind))
    if selected_kind not in {ContentKind.IMAGE, ContentKind.VIDEO}:
        raise ValueError("event content asset must be image or video")
    try:
        with get_db_ro() as conn:
            previous = EventContentAssetRepository(conn).get(
                actor=actor,
                event_id=event_id,
                stage=stage,
                slot_key=slot_key,
            )
        with get_db() as conn:
            stored = EventContentAssetRepository(conn).upsert(
                actor=actor,
                event_id=event_id,
                stage=stage,
                slot_key=slot_key,
                kind=selected_kind.value,
                media_reference=media_reference,
                source=source,
                source_ref=source_ref,
            )
    except (sqlite3.Error, PostgresError, OSError) as exc:
        raise EventContentAssetError("event_content_asset_persist_failed") from exc
    if previous is not None and previous.media_reference != stored.media_reference:
        _queue_replaced(
            previous.media_reference,
            business_id=actor.business_id,
            reason="superseded_event_content_asset",
        )
    return stored


def store_generated_event_content_asset(
    *,
    actor: TenantContext,
    event_id: str,
    stage: EventContentStage,
    slot_key: str,
    kind: ContentKind | str,
    path: Path,
    content_type: str,
    extension: str,
    source_ref: str,
) -> EventContentAsset:
    actor.assert_can_manage_business()
    selected_kind = kind if isinstance(kind, ContentKind) else ContentKind(str(kind))
    if selected_kind not in {ContentKind.IMAGE, ContentKind.VIDEO}:
        raise ValueError("generated event content asset must be image or video")
    current = get_event_content_asset(
        actor=actor,
        event_id=event_id,
        stage=stage,
        slot_key=slot_key,
    )
    if (
        current is not None
        and current.source == "generated"
        and current.source_ref == str(source_ref)
        and current.kind == selected_kind.value
    ):
        return current
    try:
        stored_media = store_program_media(
            path,
            business_id=actor.business_id,
            content_kind=selected_kind,
            content_type=content_type,
            extension=extension,
        )
    except ProgramMediaStoreError as exc:
        raise EventContentAssetError("event_content_asset_store_failed") from exc
    try:
        return set_event_content_asset_reference(
            actor=actor,
            event_id=event_id,
            stage=stage,
            slot_key=slot_key,
            kind=selected_kind,
            media_reference=stored_media.reference,
            source="generated",
            source_ref=str(source_ref),
        )
    except (EventContentAssetError, ValueError, RuntimeError):
        _queue_replaced(
            stored_media.reference,
            business_id=actor.business_id,
            reason="failed_event_content_asset_binding",
        )
        raise


__all__ = [
    "EventContentAssetError",
    "get_event_content_asset",
    "set_event_content_asset_reference",
    "store_generated_event_content_asset",
]
