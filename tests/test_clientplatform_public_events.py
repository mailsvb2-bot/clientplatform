from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import importlib.util
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from clientplatform.application.event_public_surface import (
    SECURITY_HEADERS,
    render_event_landing_body,
)
from clientplatform.domain.events import PublicEvent, new_public_slug

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None
if _AIOHTTP_AVAILABLE:
    from clientplatform.runtime import public_events as public_events_runtime


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


class _Form(dict):
    def getall(self, key: str, default=None):
        value = self.get(key)
        if value is None:
            return [] if default is None else default
        return value if isinstance(value, list) else [value]


class _Request:
    content_length = 256
    match_info = {"slug": "existing-event"}

    async def post(self):
        return _Form(
            name="Victim",
            email="victim@example.test",
            consent="yes",
            marketing_consent="yes",
            marketing_channel=["email"],
            marketing_consent_hash="a" * 64,
        )


@unittest.skipUnless(_AIOHTTP_AVAILABLE, "aiohttp runtime dependency is not installed")
class PublicEventCommercialConsentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_registration_cannot_regrant_marketing_consent_by_email(self) -> None:
        registration = SimpleNamespace(
            business_id="11111111-1111-4111-8111-111111111111",
            event_id="22222222-2222-4222-8222-222222222222",
            id="33333333-3333-4333-8333-333333333333",
        )
        result = SimpleNamespace(
            created=False,
            registration=registration,
            notifications=SimpleNamespace(enabled=False),
        )
        grant = Mock()
        with (
            patch.object(public_events_runtime, "get_db", return_value=nullcontext(object())),
            patch.object(
                public_events_runtime,
                "register_public_attendee_in_transaction",
                return_value=result,
            ),
            patch.object(
                public_events_runtime,
                "grant_event_commercial_consent_in_transaction",
                grant,
            ),
        ):
            response = await public_events_runtime.public_event_register(_Request())
        self.assertEqual(response.status, 200)
        self.assertFalse(grant.called)
        self.assertIn(
            "Повторная регистрация не изменяет рекламное согласие",
            response.text,
        )
