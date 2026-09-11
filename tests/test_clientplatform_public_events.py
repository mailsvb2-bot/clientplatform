from __future__ import annotations

from datetime import datetime, timedelta, timezone

from clientplatform.domain.events import PublicEvent, new_public_slug
from clientplatform.runtime.public_events import SECURITY_HEADERS, _landing


def test_landing_is_provider_neutral_and_cannot_leak_join_url() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    event = PublicEvent(
        public_slug=new_public_slug(),
        kind="webinar",
        title="Эфир",
        description="Описание",
        starts_at=now + timedelta(days=1),
        ends_at=None,
        timezone_name="Europe/Moscow",
        provider_key="custom_provider",
        provider_label="Моя площадка",
    )
    response = _landing(event, source="yandex_search", campaign_ref="campaign-42")
    assert response.status == 200
    assert "join_url" not in response.text
    assert "zoom" not in response.text.lower()
    assert "yandex_search" in response.text
    assert "campaign-42" in response.text
    assert response.headers["Referrer-Policy"] == SECURITY_HEADERS["Referrer-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"
