from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from clientplatform.domain.events import (
    Event,
    EventValidationError,
    infer_provider_key,
    new_public_slug,
    normalize_provider_key,
    validate_external_https_url,
)


def test_known_providers_are_hints_not_an_enum() -> None:
    assert infer_provider_key("https://zoom.us/j/123") == "zoom"
    assert infer_provider_key("https://www.youtube.com/live/abc") == "youtube"
    assert infer_provider_key("https://rutube.ru/video/abc") == "rutube"
    assert infer_provider_key("https://stream.example.org/room/42") == "external"
    assert normalize_provider_key(
        "my_future_platform",
        join_url="https://stream.example.org/room/42",
    ) == "my_future_platform"


def test_provider_neutral_url_boundary_is_https_only() -> None:
    with pytest.raises(EventValidationError):
        validate_external_https_url("http://example.org/room")
    with pytest.raises(EventValidationError):
        validate_external_https_url("https://user:secret@example.org/room")


def test_event_accepts_a_provider_that_did_not_exist_when_code_was_written() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    event = Event(
        id=str(uuid4()),
        business_id=str(uuid4()),
        created_by_member_id=str(uuid4()),
        kind="webinar",
        status="draft",
        title="Новый эфир",
        description="provider-neutral",
        starts_at=now + timedelta(days=1),
        ends_at=None,
        timezone_name="Europe/Moscow",
        provider_key="future_stage_2030",
        provider_label="Future Stage",
        join_url="https://future-stage.example/room/123",
        offer_url=None,
        public_slug=new_public_slug(),
        consent_version="test-v1",
        notification_connection_id=None,
        created_at=now,
        updated_at=now,
    )
    assert event.provider_key == "future_stage_2030"
    assert event.join_url.startswith("https://future-stage.example/")
