from __future__ import annotations

from pathlib import Path


def test_canonical_booking_prompts_advertise_short_date_input() -> None:
    first_result = Path("handlers/clientplatform_first_result.py").read_text(
        encoding="utf-8"
    )
    wizard = Path("handlers/clientplatform_booking_wizard_ux.py").read_text(
        encoding="utf-8"
    )

    assert "10.08 15:00" in first_result
    assert "10.08.27 15:00" in first_result
    assert "15.08 18:30" in wizard
    assert "15.08.27 18:30" in wizard
