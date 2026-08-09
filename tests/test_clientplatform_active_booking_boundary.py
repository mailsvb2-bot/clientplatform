from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from clientplatform.application import bookings as application_bookings
from clientplatform.domain.bookings import BookingNotFound


@contextmanager
def _fake_db():
    yield object()


def _claim(starts_at: str):
    return SimpleNamespace(
        customer_id="customer-id",
        slot=SimpleNamespace(
            slot=SimpleNamespace(starts_at=starts_at),
        ),
    )


def test_active_customer_booking_rejects_started_appointment() -> None:
    repository = SimpleNamespace(
        get_customer_booking=lambda **_kwargs: _claim("2026-08-09T18:00:00+00:00")
    )
    with (
        patch.object(application_bookings, "get_db_ro", _fake_db),
        patch.object(application_bookings, "assert_external_customer"),
        patch.object(application_bookings, "BookingRepository", return_value=repository),
        pytest.raises(BookingNotFound, match="уже началась"),
    ):
        application_bookings.get_customer_booking(
            telegram_user_id=700001,
            business_id="11111111-1111-1111-1111-111111111111",
            slot_id="22222222-2222-2222-2222-222222222222",
            now="2026-08-09T18:00:00+00:00",
        )


def test_active_customer_booking_keeps_future_appointment() -> None:
    expected = _claim("2026-08-09T18:01:00+00:00")
    repository = SimpleNamespace(get_customer_booking=lambda **_kwargs: expected)
    with (
        patch.object(application_bookings, "get_db_ro", _fake_db),
        patch.object(application_bookings, "assert_external_customer"),
        patch.object(application_bookings, "BookingRepository", return_value=repository),
    ):
        actual = application_bookings.get_customer_booking(
            telegram_user_id=700001,
            business_id="11111111-1111-1111-1111-111111111111",
            slot_id="22222222-2222-2222-2222-222222222222",
            now="2026-08-09T18:00:00+00:00",
        )
    assert actual is expected
