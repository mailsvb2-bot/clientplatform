from __future__ import annotations

from contextlib import nullcontext
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import unittest

from clientplatform.application import cockpit_events
from clientplatform.application.cockpit import cockpit_navigation
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.privacy_manifest import TENANT_POLICIES

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "22222222-2222-4222-8222-222222222222"


def _actor(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(business_id=_BUSINESS, user_id=101, membership_id=_MEMBER, role=role)


def test_owner_navigation_exposes_native_events_section() -> None:
    items = {item.id: item for item in cockpit_navigation(_actor())}
    assert items["events"].status == "available"
    assert "вебинар" in items["events"].title.lower()


def test_event_funnel_is_restricted_from_support_and_non_finance_roles() -> None:
    for role in (PlatformRole.SUPPORT, PlatformRole.MARKETER, PlatformRole.ANALYST, PlatformRole.CONTENT_MANAGER):
        items = {item.id: item for item in cockpit_navigation(_actor(role))}
        assert items["events"].status == "restricted"
    assert {item.id: item for item in cockpit_navigation(_actor(PlatformRole.MANAGER))}["events"].status == "available"


def test_event_creation_request_id_is_replay_safe() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE clientplatform_event_owner_requests(
        business_id TEXT NOT NULL, request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
        event_id TEXT, created_at TEXT NOT NULL, PRIMARY KEY(business_id,request_id))""")
    request_id = str(uuid4())
    actor = _actor()
    created = SimpleNamespace(event_id="33333333-3333-4333-8333-333333333333")
    with (
        patch.object(cockpit_events, "_resolve_actor", return_value=(actor, "Практика")),
        patch.object(cockpit_events, "_public_base_url", return_value="https://example.test"),
        patch.object(cockpit_events, "get_business_profile", return_value=SimpleNamespace(timezone="Europe/Moscow")),
        patch.object(cockpit_events, "parse_local_booking_start", return_value="2026-09-15T16:00:00+00:00"),
        patch.object(cockpit_events, "atomic_db", return_value=nullcontext(conn)),
        patch.object(cockpit_events, "create_and_publish_online_event_in_transaction", return_value=created) as create,
    ):
        first = cockpit_events.create_cockpit_event(
            telegram_user_id=101, requested_business_id=_BUSINESS, request_id=request_id,
            title="Вебинар", starts_at_local="15.09.2026 19:00",
            join_url="https://stage.example/room", offer_url="https://shop.example/offer",
            description="Описание", enable_email_notifications=True,
        )
        second = cockpit_events.create_cockpit_event(
            telegram_user_id=101, requested_business_id=_BUSINESS, request_id=request_id,
            title="Вебинар", starts_at_local="15.09.2026 19:00",
            join_url="https://stage.example/room", offer_url="https://shop.example/offer",
            description="Описание", enable_email_notifications=True,
        )
    assert first == second == created.event_id
    create.assert_called_once()


def test_event_creation_rejects_reused_request_id_for_different_payload() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE clientplatform_event_owner_requests(
        business_id TEXT NOT NULL, request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
        event_id TEXT, created_at TEXT NOT NULL, PRIMARY KEY(business_id,request_id))""")
    request_id = str(uuid4())
    conn.execute("INSERT INTO clientplatform_event_owner_requests VALUES(?,?,?,?,?)", (_BUSINESS, request_id, "0" * 64, "33333333-3333-4333-8333-333333333333", "2026-09-12T00:00:00+00:00"))
    actor = _actor()
    with (
        patch.object(cockpit_events, "_resolve_actor", return_value=(actor, "Практика")),
        patch.object(cockpit_events, "_public_base_url", return_value="https://example.test"),
        patch.object(cockpit_events, "get_business_profile", return_value=SimpleNamespace(timezone="Europe/Moscow")),
        patch.object(cockpit_events, "parse_local_booking_start", return_value="2026-09-15T16:00:00+00:00"),
        patch.object(cockpit_events, "atomic_db", return_value=nullcontext(conn)),
    ):
        with unittest.TestCase().assertRaisesRegex(ValueError, "request_id"):
            cockpit_events.create_cockpit_event(
                telegram_user_id=101, requested_business_id=_BUSINESS, request_id=request_id,
                title="Другое название", starts_at_local="15.09.2026 19:00",
                join_url="https://stage.example/room", offer_url=None,
                description="", enable_email_notifications=True,
            )


def test_event_tables_have_explicit_privacy_dispositions() -> None:
    assert TENANT_POLICIES["clientplatform_event_commercial_consent_events"].disposition == "erase"
    assert TENANT_POLICIES["clientplatform_event_commercial_channel_state"].disposition == "erase"
    assert TENANT_POLICIES["clientplatform_event_owner_requests"].disposition == "retain"
