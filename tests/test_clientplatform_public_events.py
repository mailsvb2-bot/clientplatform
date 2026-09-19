from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import importlib.util
import sqlite3
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


def test_landing_separates_registration_and_optional_marketing_channels() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    event = PublicEvent(
        public_slug=new_public_slug(),
        kind="webinar",
        title="Практический вебинар",
        description="Описание",
        starts_at=now + timedelta(days=2),
        ends_at=None,
        timezone_name="Europe/Moscow",
        provider_key="external",
        provider_label=None,
    )
    body = render_event_landing_body(event, advertiser_label="Бренд")
    assert "Регистрация на мероприятие" in body
    assert "Телефон (необязательно)" in body
    assert "Полезные материалы и предложения (необязательно)" in body
    assert "value=email checked" in body
    assert "value=telegram" in body
    assert "value=vk" in body
    assert "value=max" in body
    assert "E-mail подтверждается этой формой" in body
    assert "только после подтверждения владения аккаунтом" in body
    assert "от «Бренд»" in body


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
class PublicEventBusinessMessengerLinkTests(unittest.TestCase):
    def test_entry_links_use_exact_connected_business_accounts(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE connections(
                id TEXT PRIMARY KEY,business_id TEXT,platform TEXT,
                connection_type TEXT,external_account_id TEXT,status TEXT,
                created_at TEXT
            );
            CREATE TABLE managed_bots(
                connection_id TEXT,business_id TEXT,platform TEXT,
                username TEXT,status TEXT
            );
            """
        )
        business_id = "business-a"
        conn.executemany(
            """
            INSERT INTO connections(
                id,business_id,platform,connection_type,
                external_account_id,status,created_at
            ) VALUES(?,?,?,?,?,'active','2026-09-19T00:00:00+00:00')
            """,
            (
                ("tg", business_id, "telegram", "telegram_managed_bot", "tg-bot"),
                ("vk", business_id, "vk", "vk_community", "123456"),
                ("max", business_id, "max", "max_personal_bot", "998877"),
            ),
        )
        conn.executemany(
            "INSERT INTO managed_bots VALUES(?,?,?,?, 'active')",
            (
                ("tg", business_id, "telegram", "BusinessTelegramBot"),
                ("max", business_id, "max", "BusinessMaxBot"),
            ),
        )
        payload = "ecv_token-value"
        with patch.object(
            public_events_runtime.settings,
            "MAX_BOT_LINK_BASE",
            "https://max.ru/{bot}?start={payload}",
        ):
            telegram = public_events_runtime._registration_channel_entry_url(
                conn,
                business_id=business_id,
                platform="telegram",
                payload=payload,
            )
            vk = public_events_runtime._registration_channel_entry_url(
                conn,
                business_id=business_id,
                platform="vk",
                payload=payload,
            )
            max_url = public_events_runtime._registration_channel_entry_url(
                conn,
                business_id=business_id,
                platform="max",
                payload=payload,
            )
        self.assertEqual(
            telegram,
            "https://t.me/BusinessTelegramBot?start=ecv_token-value",
        )
        self.assertEqual(
            vk,
            "https://vk.com/im?sel=-123456&start=ecv_token-value",
        )
        self.assertEqual(
            max_url,
            "https://max.ru/BusinessMaxBot?start=ecv_token-value",
        )
        conn.close()


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
