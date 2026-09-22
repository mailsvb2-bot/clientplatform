from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import native_member_interactions as ui
from clientplatform.domain.activity import CapabilityStatus, OfferingStatus
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.presentation.booking_schedule_picker import (
    BOOKING_DURATIONS,
    BOOKING_START_TIMES,
)
from handlers import clientplatform_booking_wizard_ux as telegram_booking


def _actor() -> TenantContext:
    return TenantContext(
        business_id=str(uuid4()),
        membership_id=str(uuid4()),
        user_id=101,
        role=PlatformRole.OWNER,
    )


def _offering():
    return SimpleNamespace(
        id=str(uuid4()),
        title="Консультация",
        status=OfferingStatus.ACTIVE,
    )


def _capability():
    return SimpleNamespace(
        id=str(uuid4()),
        connector_key="consultations",
        title="Консультации",
        status=CapabilityStatus.ACTIVE,
    )


def _commands(message) -> list[str]:
    return [button.command for row in message.rows for button in row]


def _button_count(message) -> int:
    return sum(len(row) for row in message.rows)


def test_telegram_and_native_share_booking_time_and_duration_contract() -> None:
    assert telegram_booking._BOOKING_START_TIMES == BOOKING_START_TIMES
    assert telegram_booking._QUICK_DURATIONS == BOOKING_DURATIONS
    assert BOOKING_START_TIMES[0] == "08:00"
    assert BOOKING_START_TIMES[-1] == "22:00"
    assert BOOKING_DURATIONS == (15, 30, 45, 60, 75, 90, 120, 180)


def test_vk_and_max_enter_the_same_button_only_booking_wizard() -> None:
    actor = _actor()
    offering = _offering()
    capability = _capability()
    profile = SimpleNamespace(timezone="Europe/Moscow")

    with (
        patch.object(ui, "resolve_tenant_context", return_value=actor),
        patch.object(ui, "list_business_capabilities", return_value=[capability]),
        patch.object(ui, "list_business_offerings", return_value=[offering]),
        patch.object(ui, "get_business_profile", return_value=profile),
        patch.object(ui, "local_today", return_value=date(2026, 9, 22)),
    ):
        vk = ui.render_native_member_interaction(
            actor=actor,
            raw_text=f"cpm:booking-open-for:{offering.id}",
            interaction_key="vk-booking",
            current_platform=ConnectionPlatform.VK,
        )
        max_ui = ui.render_native_member_interaction(
            actor=actor,
            raw_text=f"cpm:booking-open-for:{offering.id}",
            interaction_key="max-booking",
            current_platform=ConnectionPlatform.MAX,
        )

    assert vk == max_ui
    assert "Выберите месяц" in vk.text
    assert "Напишите дату" not in vk.text
    assert any(command.startswith(f"cpm:booking-dates:{offering.id}:") for command in _commands(vk))
    assert _button_count(vk) <= 10


def test_native_booking_month_date_and_time_pages_stay_within_transport_limit() -> None:
    actor = _actor()
    offering = _offering()
    capability = _capability()
    profile = SimpleNamespace(timezone="Europe/Moscow")

    with (
        patch.object(ui, "list_business_capabilities", return_value=[capability]),
        patch.object(ui, "list_business_offerings", return_value=[offering]),
        patch.object(ui, "get_business_profile", return_value=profile),
        patch.object(ui, "local_today", return_value=date(2026, 9, 22)),
    ):
        months = ui._with_parent_navigation(
            ui._booking_months_message(actor, offering.id, 0),
            ui.ParsedMemberInteraction("booking-open-for", (offering.id,)),
        )
        dates = ui._with_parent_navigation(
            ui._booking_dates_message(actor, offering.id, "202610", 0),
            ui.ParsedMemberInteraction(
                "booking-dates",
                (offering.id, "202610", "0"),
            ),
        )
        times = ui._with_parent_navigation(
            ui._booking_times_message(actor, offering.id, "2026-10-15", 3),
            ui.ParsedMemberInteraction(
                "booking-times",
                (offering.id, "2026-10-15", "3"),
            ),
        )
        durations = ui._with_parent_navigation(
            ui._booking_durations_message(
                actor,
                offering.id,
                "2026-10-15",
                "1900",
            ),
            ui.ParsedMemberInteraction(
                "booking-time",
                (offering.id, "2026-10-15", "1900"),
            ),
        )

    for message in (months, dates, times, durations):
        assert _button_count(message) <= 10

    assert f"cpm:booking-time:{offering.id}:2026-10-15:1900" in _commands(times)
    duration_commands = _commands(durations)
    assert f"cpm:booking-duration:{offering.id}:2026-10-15:1900:60" in duration_commands
    assert f"cpm:booking-duration:{offering.id}:2026-10-15:1900:90" in duration_commands


def test_native_booking_selection_uses_canonical_create_booking_slot() -> None:
    actor = _actor()
    offering = _offering()
    capability = _capability()
    profile = SimpleNamespace(timezone="Europe/Moscow")
    slot = SimpleNamespace(
        offering_title=offering.title,
        local_start="15.10.2026 19:00",
    )

    with (
        patch.object(ui, "list_business_capabilities", return_value=[capability]),
        patch.object(ui, "list_business_offerings", return_value=[offering]),
        patch.object(ui, "get_business_profile", return_value=profile),
        patch.object(ui, "local_today", return_value=date(2026, 9, 22)),
        patch.object(ui, "create_booking_slot", return_value=slot) as create,
    ):
        result = ui._booking_create_from_selection(
            actor,
            offering.id,
            "2026-10-15",
            "1900",
            "90",
        )

    create.assert_called_once_with(
        actor=actor,
        offering_id=offering.id,
        local_start="15.10.2026 19:00",
        duration_minutes=90,
    )
    assert "✅ Время открыто для записи" in result.text


def test_native_booking_rejects_past_date_unknown_time_and_unknown_duration() -> None:
    actor = _actor()
    offering = _offering()
    capability = _capability()
    profile = SimpleNamespace(timezone="Europe/Moscow")

    common = (
        patch.object(ui, "list_business_capabilities", return_value=[capability]),
        patch.object(ui, "list_business_offerings", return_value=[offering]),
        patch.object(ui, "get_business_profile", return_value=profile),
        patch.object(ui, "local_today", return_value=date(2026, 9, 22)),
    )

    with common[0], common[1], common[2], common[3]:
        try:
            ui._booking_create_from_selection(
                actor,
                offering.id,
                "2026-09-21",
                "1900",
                "60",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("past booking date must fail closed")

    with (
        patch.object(ui, "list_business_capabilities", return_value=[capability]),
        patch.object(ui, "list_business_offerings", return_value=[offering]),
        patch.object(ui, "get_business_profile", return_value=profile),
        patch.object(ui, "local_today", return_value=date(2026, 9, 22)),
    ):
        for compact_time, duration in (("0730", "60"), ("1900", "20")):
            try:
                ui._booking_create_from_selection(
                    actor,
                    offering.id,
                    "2026-10-15",
                    compact_time,
                    duration,
                )
            except ValueError:
                continue
            raise AssertionError("unsupported booking preset must fail closed")
