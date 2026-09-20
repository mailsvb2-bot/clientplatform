from __future__ import annotations

from pathlib import Path


def test_legacy_first_result_keeps_short_date_copy_but_booking_wizard_is_button_only() -> None:
    first_result = Path("handlers/clientplatform_first_result.py").read_text(
        encoding="utf-8"
    )
    wizard = Path("handlers/clientplatform_booking_wizard_ux.py").read_text(
        encoding="utf-8"
    )

    assert "10.08 15:00" in first_result
    assert "10.08.27 15:00" in first_result

    assert "Дата и время выбираются кнопками." in wizard
    assert "Длительность выбирается кнопкой." in wizard
    assert "15.08 18:30" not in wizard
    assert "15.08.27 18:30" not in wizard
