from __future__ import annotations

import pytest

from clientplatform.presentation import ad_spend_telegram


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", 100),
        ("1,25", 125),
        ("500.00", 50_000),
        (" 12 345,67 ", 1_234_567),
    ],
)
def test_parse_minor_units_is_exact(raw: str, expected: int) -> None:
    assert ad_spend_telegram._parse_minor_units(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "0", "-1", "nan", "inf", "1.001", "not-money"],
)
def test_parse_minor_units_rejects_ambiguous_or_unsafe_values(raw: str) -> None:
    with pytest.raises(ValueError):
        ad_spend_telegram._parse_minor_units(raw)


def test_format_minor_units_keeps_currency_visible() -> None:
    assert ad_spend_telegram._format_minor(12_345, "RUB") == "123,45 RUB"


def test_consent_copy_does_not_claim_that_spend_already_started() -> None:
    source = __import__("inspect").getsource(ad_spend_telegram)
    assert "Показы и расходы не запущены" in source
    assert "Подтверждение создания черновика DRAFT никогда не считается согласием" in source
    assert "идемпотентная очередь запуска и остановки" in source
