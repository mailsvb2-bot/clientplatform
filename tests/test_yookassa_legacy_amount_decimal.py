from __future__ import annotations

import pytest

from services.payments import yookassa_checkout as checkout


def test_legacy_amount_uses_decimal_half_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAYMENT_AMOUNT_RUB", "2.675")

    amount, _description = checkout._legacy_amount_description("subscription")

    assert amount == "2.68"


@pytest.mark.parametrize("raw", ["0", "-1", "NaN", "Infinity"])
def test_legacy_amount_rejects_non_positive_or_non_finite(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("PAYMENT_AMOUNT_RUB", raw)

    with pytest.raises(checkout.YooKassaCheckoutError):
        checkout._legacy_amount_description("subscription")
