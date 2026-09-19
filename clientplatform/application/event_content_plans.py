from __future__ import annotations

import re
from dataclasses import dataclass

from clientplatform.application.creative_generation import prepare_creative_generation
from clientplatform.domain.event_content import (
    EventContentMode,
    EventContentPreference,
    EventContentStage,
    event_visual_request,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.event_content_repository import (
    EventContentPreferenceRepository,
)
from services.db import get_db, get_db_ro


_MESSAGE_KEY_RE = re.compile(r"[A-Za-z0-9_.:@/-]{1,72}")


@dataclass(frozen=True, slots=True)
class EventContentPlan:
    event_id: str
    warmup: EventContentMode
    event_day: EventContentMode
    post_event: EventContentMode


@dataclass(frozen=True, slots=True)
class EventVisualPreparation:
    event_id: str
    stage: EventContentStage
    mode: EventContentMode
    receipt_id: str
    request_text: str
    prepared_for_requested_visual: bool


def _load_goal_visual_brand(*, actor: TenantContext):
    # Keep this module dependency-light. The publication asset stack imports Pillow,
    # which is intentionally absent from dependency-light Canon jobs. Import it only
    # after the owner has explicitly selected a visual mode.
    from clientplatform.application.creative_studio_publication import load_goal_visual_brand

    return load_goal_visual_brand(actor=actor)


def _freeze_business_visual_payload(
    *,
    request: str,
    kind: str,
    brand_context: str,
    country_code: str,
    binding: dict[str, str] | None = None,
) -> str:
    from clientplatform.application.visual_creatives import freeze_business_visual_payload

    return freeze_business_visual_payload(
        request=request,
        kind=kind,
        brand_context=brand_context,
        country_code=country_code,
        binding=binding,
    )


def set_event_content_mode(
    *,
    actor: TenantContext,
    event_id: str,
    stage: EventContentStage,
    mode: EventContentMode,
) -> EventContentPreference:
    with get_db() as conn:
        return EventContentPreferenceRepository(conn).set(
            actor=actor,
            event_id=event_id,
            stage=stage,
            mode=mode,
        )


def get_event_content_plan(
    *,
    actor: TenantContext,
    event_id: str,
) -> EventContentPlan:
    normalized_event_id = normalize_uuid(event_id, field_name="event_id")
    with get_db_ro() as conn:
        repository = EventContentPreferenceRepository(conn)
        stored = {
            item.stage: item.mode
            for item in repository.list_for_event(
                actor=actor,
                event_id=normalized_event_id,
            )
        }
    return EventContentPlan(
        event_id=normalized_event_id,
        warmup=stored.get(EventContentStage.WARMUP, EventContentMode.TEXT),
        event_day=stored.get(EventContentStage.EVENT_DAY, EventContentMode.TEXT),
        post_event=stored.get(EventContentStage.POST_EVENT, EventContentMode.TEXT),
    )


def _normalize_message_key(value: object) -> str:
    raw = str(value or "").strip()
    if not _MESSAGE_KEY_RE.fullmatch(raw):
        raise ValueError("event visual message key is invalid")
    return raw


def event_visual_idempotency_key(
    *,
    event_id: str,
    stage: EventContentStage,
    message_key: str,
) -> str:
    event = normalize_uuid(event_id, field_name="event_id")
    key = _normalize_message_key(message_key)
    return f"event:{event}:{stage.value}:{key}:image:v1"


def prepare_event_stage_visual(
    *,
    actor: TenantContext,
    event_id: str,
    stage: EventContentStage,
    message_key: str,
    event_title: str,
    message_text: str,
    session_label: str = "",
    country_code: str = "",
) -> EventVisualPreparation | None:
    plan = get_event_content_plan(actor=actor, event_id=event_id)
    mode = {
        EventContentStage.WARMUP: plan.warmup,
        EventContentStage.EVENT_DAY: plan.event_day,
        EventContentStage.POST_EVENT: plan.post_event,
    }[stage]
    if mode is EventContentMode.TEXT:
        return None

    request_text = event_visual_request(
        stage=stage,
        mode=mode,
        event_title=event_title,
        message_text=message_text,
        session_label=session_label,
    )
    brand = _load_goal_visual_brand(actor=actor)
    brand_context = brand.prompt_context()
    visual_kind = "video" if mode is EventContentMode.TEXT_WITH_VIDEO else "image"
    provider_payload_json = _freeze_business_visual_payload(
        request=request_text,
        kind=visual_kind,
        brand_context=brand_context,
        country_code=country_code,
        binding={
            "type": "event_content",
            "event_id": normalize_uuid(event_id, field_name="event_id"),
            "stage": stage.value,
            "slot_key": _normalize_message_key(message_key),
            "kind": visual_kind,
        },
    )
    receipt = prepare_creative_generation(
        actor=actor,
        request_text=request_text,
        brand_context=brand_context,
        country_code=country_code,
        provider_payload_json=provider_payload_json,
    )
    return EventVisualPreparation(
        event_id=normalize_uuid(event_id, field_name="event_id"),
        stage=stage,
        mode=mode,
        receipt_id=receipt.id,
        request_text=request_text,
        prepared_for_requested_visual=receipt.request_text == request_text,
    )


__all__ = [
    "EventContentPlan",
    "EventVisualPreparation",
    "event_visual_idempotency_key",
    "get_event_content_plan",
    "prepare_event_stage_visual",
    "set_event_content_mode",
]
