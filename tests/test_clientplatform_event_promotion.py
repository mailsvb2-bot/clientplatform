from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from clientplatform.application import event_promotion


def test_event_advertising_url_is_event_scoped_and_attributable() -> None:
    event_id = "33333333-3333-4333-8333-333333333333"
    url = event_promotion.event_advertising_url(
        public_base_url="https://client.example.test/",
        public_slug="AbCdEf0123456789_slug-demo",
        event_id=event_id,
    )

    assert url.startswith(
        "https://client.example.test/e/AbCdEf0123456789_slug-demo?"
    )
    assert "source=ads" in url
    assert f"campaign_ref=event%3A{event_id}" in url
    assert "/clientplatform/acquire" not in url


def test_event_advertising_url_fails_closed_without_https_or_slug() -> None:
    event_id = "33333333-3333-4333-8333-333333333333"
    for base, slug in (
        ("http://client.example.test", "AbCdEf0123456789_slug-demo"),
        ("", "AbCdEf0123456789_slug-demo"),
        ("https://client.example.test", ""),
    ):
        try:
            event_promotion.event_advertising_url(
                public_base_url=base,
                public_slug=slug,
                event_id=event_id,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe event advertising URL must fail closed")


def test_event_promotion_tolerates_only_missing_optional_landing_schema() -> None:
    business_id = str(uuid4())
    event_id = str(uuid4())
    actor = SimpleNamespace(
        business_id=business_id,
        user_id=101,
        membership_id=str(uuid4()),
        assert_can_manage_business=Mock(),
    )
    event = SimpleNamespace(
        id=event_id,
        title="Вебинар",
        public_slug="AbCdEf0123456789_slug-demo",
        provider_label=None,
        provider_key="external",
        join_is_ready=False,
    )

    class FakeConn:
        def execute(self, sql: str, params: tuple[str, str]):
            return SimpleNamespace(
                fetchone=lambda: {
                    "registrations": 0,
                    "registrations_from_ads": 0,
                }
            )

    @contextmanager
    def fake_db():
        yield FakeConn()

    class FakeEventRepository:
        def __init__(self, _conn):
            pass

        def get(self, *, actor, event_id):
            return event

    for missing_error in (
        "no such table: clientplatform_event_landing_profiles",
        'relation "clientplatform_event_landing_profiles" does not exist',
        "undefined table: clientplatform_event_landing_profiles",
    ):
        class MissingLandingRepository:
            def __init__(self, _conn):
                pass

            def get(self, *, actor, event_id):
                raise sqlite3.OperationalError(missing_error)

        with (
            patch.object(event_promotion, "get_db_ro", fake_db),
            patch.object(event_promotion, "EventRepository", FakeEventRepository),
            patch.object(event_promotion, "EventLandingRepository", MissingLandingRepository),
        ):
            snapshot = event_promotion.get_event_promotion_snapshot(
                actor=actor,
                event_id=event_id,
                public_base_url="https://client.example.test",
            )

        assert snapshot.landing_published is False
        assert snapshot.join_ready is False
        assert snapshot.provider_label == "external"

    class BrokenLandingRepository:
        def __init__(self, _conn):
            pass

        def get(self, *, actor, event_id):
            raise sqlite3.OperationalError("database is locked")

    with (
        patch.object(event_promotion, "get_db_ro", fake_db),
        patch.object(event_promotion, "EventRepository", FakeEventRepository),
        patch.object(event_promotion, "EventLandingRepository", BrokenLandingRepository),
    ):
        try:
            event_promotion.get_event_promotion_snapshot(
                actor=actor,
                event_id=event_id,
                public_base_url="https://client.example.test",
            )
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc)
        else:
            raise AssertionError("non-schema landing storage failure must remain visible")


def test_event_promotion_snapshot_keeps_event_funnel_separate_from_booking_slots() -> None:
    business_id = str(uuid4())
    event_id = str(uuid4())
    actor = SimpleNamespace(
        business_id=business_id,
        user_id=101,
        membership_id=str(uuid4()),
        assert_can_manage_business=Mock(),
    )
    event = SimpleNamespace(
        id=event_id,
        title="Вебинар",
        public_slug="AbCdEf0123456789_slug-demo",
        provider_label="Webinar.ru",
        provider_key="external",
        join_is_ready=True,
    )
    landing = SimpleNamespace(is_published=True)

    class FakeConn:
        def execute(self, sql: str, params: tuple[str, str]):
            assert "clientplatform_event_registrations" in sql
            assert params == (business_id, event_id)
            return SimpleNamespace(
                fetchone=lambda: {
                    "registrations": 9,
                    "registrations_from_ads": 4,
                }
            )

    @contextmanager
    def fake_db():
        yield FakeConn()

    class FakeEventRepository:
        def __init__(self, _conn):
            pass

        def get(self, *, actor, event_id):
            return event

    class FakeLandingRepository:
        def __init__(self, _conn):
            pass

        def get(self, *, actor, event_id):
            return landing

    with (
        patch.object(event_promotion, "get_db_ro", fake_db),
        patch.object(event_promotion, "EventRepository", FakeEventRepository),
        patch.object(event_promotion, "EventLandingRepository", FakeLandingRepository),
    ):
        snapshot = event_promotion.get_event_promotion_snapshot(
            actor=actor,
            event_id=event_id,
            public_base_url="https://client.example.test",
        )

    actor.assert_can_manage_business.assert_called_once()
    assert snapshot.event_id == event_id
    assert snapshot.title == "Вебинар"
    assert snapshot.provider_label == "Webinar.ru"
    assert snapshot.join_ready is True
    assert snapshot.landing_published is True
    assert snapshot.registrations == 9
    assert snapshot.registrations_from_ads == 4
    assert "source=ads" in snapshot.advertising_url
    assert "campaign_ref=event%3A" in snapshot.advertising_url


def test_event_promotion_snapshot_handles_empty_registration_row_and_provider_fallback() -> None:
    business_id = str(uuid4())
    event_id = str(uuid4())
    actor = SimpleNamespace(
        business_id=business_id,
        user_id=101,
        membership_id=str(uuid4()),
        assert_can_manage_business=Mock(),
    )
    event = SimpleNamespace(
        id=event_id,
        title="Вебинар без провайдера",
        public_slug="AbCdEf0123456789_empty",
        provider_label=None,
        provider_key=None,
        join_is_ready=False,
    )

    class FakeConn:
        def execute(self, sql: str, params: tuple[str, str]):
            assert params == (business_id, event_id)
            return SimpleNamespace(fetchone=lambda: None)

    @contextmanager
    def fake_db():
        yield FakeConn()

    class FakeEventRepository:
        def __init__(self, _conn):
            pass

        def get(self, *, actor, event_id):
            return event

    class FakeLandingRepository:
        def __init__(self, _conn):
            pass

        def get(self, *, actor, event_id):
            return None

    with (
        patch.object(event_promotion, "get_db_ro", fake_db),
        patch.object(event_promotion, "EventRepository", FakeEventRepository),
        patch.object(event_promotion, "EventLandingRepository", FakeLandingRepository),
    ):
        snapshot = event_promotion.get_event_promotion_snapshot(
            actor=actor,
            event_id=event_id,
            public_base_url="https://client.example.test",
        )

    assert snapshot.provider_label == "площадка"
    assert snapshot.registrations == 0
    assert snapshot.registrations_from_ads == 0
    assert snapshot.landing_published is False


def test_event_promotion_snapshot_accepts_positional_registration_row() -> None:
    business_id = str(uuid4())
    event_id = str(uuid4())
    actor = SimpleNamespace(
        business_id=business_id,
        user_id=101,
        membership_id=str(uuid4()),
        assert_can_manage_business=Mock(),
    )
    event = SimpleNamespace(
        id=event_id,
        title="Вебинар",
        public_slug="AbCdEf0123456789_tuple",
        provider_label=None,
        provider_key="external",
        join_is_ready=True,
    )

    class FakeConn:
        def execute(self, sql: str, params: tuple[str, str]):
            assert params == (business_id, event_id)
            return SimpleNamespace(fetchone=lambda: (7, None))

    @contextmanager
    def fake_db():
        yield FakeConn()

    class FakeEventRepository:
        def __init__(self, _conn):
            pass

        def get(self, *, actor, event_id):
            return event

    class FakeLandingRepository:
        def __init__(self, _conn):
            pass

        def get(self, *, actor, event_id):
            return SimpleNamespace(is_published=False)

    with (
        patch.object(event_promotion, "get_db_ro", fake_db),
        patch.object(event_promotion, "EventRepository", FakeEventRepository),
        patch.object(event_promotion, "EventLandingRepository", FakeLandingRepository),
    ):
        snapshot = event_promotion.get_event_promotion_snapshot(
            actor=actor,
            event_id=event_id,
            public_base_url="https://client.example.test",
        )

    assert snapshot.provider_label == "external"
    assert snapshot.registrations == 7
    assert snapshot.registrations_from_ads == 0
    assert snapshot.landing_published is False
