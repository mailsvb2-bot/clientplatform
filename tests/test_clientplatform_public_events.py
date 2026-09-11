from __future__ import annotations

from datetime import datetime, timedelta, timezone

from clientplatform.application.event_public_surface import (
    SECURITY_HEADERS,
    render_event_landing_body,
)
from clientplatform.domain.events import PublicEvent, new_public_slug


def test_landing_is_provider_neutral_and_cannot_leak_join_url() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    event = PublicEvent(
        public_slug=new_public_slug(),
        kind="webinar",
        title="Эфир <безопасный>",
        description="Описание <script>alert(1)</script>",
        starts_at=now + timedelta(days=1),
        ends_at=None,
        timezone_name="Europe/Moscow",
        provider_key="custom_provider",
        provider_label="Моя площадка",
    )
    body = render_event_landing_body(
        event,
        source='yandex_search" onfocus="alert(1)',
        campaign_ref="campaign-42<script>",
    )
    assert "join_url" not in body
    assert "zoom" not in body.lower()
    assert "yandex_search&quot; onfocus=&quot;alert(1)" in body
    assert "campaign-42&lt;script&gt;" in body
    assert "Эфир &lt;безопасный&gt;" in body
    assert "<script>" not in body
    assert SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"
    assert SECURITY_HEADERS["X-Frame-Options"] == "DENY"


def test_tracking_fields_are_bounded_before_rendering() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    event = PublicEvent(
        public_slug=new_public_slug(),
        kind="webinar",
        title="Эфир",
        description="",
        starts_at=now + timedelta(days=1),
        ends_at=None,
        timezone_name="Europe/Moscow",
        provider_key="external",
        provider_label=None,
    )
    body = render_event_landing_body(event, source="s" * 500, campaign_ref="c" * 500)
    assert f"value='{'s' * 160}'" in body
    assert "s" * 161 not in body
    assert f"value='{'c' * 240}'" in body
    assert "c" * 241 not in body
