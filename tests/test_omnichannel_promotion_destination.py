from __future__ import annotations

import pytest

from clientplatform.domain.promotions import (
    PromotionChannel,
    PromotionInvariantViolation,
    promotion_destination_url,
    rewrite_promotion_source_url,
)


SOURCE = "abcdefghijkl"
REBOUND = "mnopqrstuvwx"


def test_promotion_channel_includes_max() -> None:
    assert PromotionChannel("max") is PromotionChannel.MAX


def test_promotion_destination_is_transport_neutral_https_path() -> None:
    result = promotion_destination_url("https://example.test", SOURCE)

    assert result == "https://example.test/clientplatform/acquire/cpa_abcdefghijkl"
    assert "t.me" not in result
    assert "start=" not in result


def test_promotion_destination_preserves_public_base_path() -> None:
    result = promotion_destination_url("https://example.test/public/", SOURCE)

    assert result == (
        "https://example.test/public/clientplatform/acquire/cpa_abcdefghijkl"
    )


def test_rewrite_promotion_source_url_rebinds_neutral_path() -> None:
    result = rewrite_promotion_source_url(
        "https://example.test/clientplatform/acquire/cpa_abcdefghijkl",
        from_token=SOURCE,
        to_token=REBOUND,
    )

    assert result == "https://example.test/clientplatform/acquire/cpa_mnopqrstuvwx"


def test_rewrite_promotion_source_url_keeps_legacy_telegram_compatibility() -> None:
    result = rewrite_promotion_source_url(
        "https://t.me/example_bot?start=cpa_abcdefghijkl",
        from_token=SOURCE,
        to_token=REBOUND,
    )

    assert result == "https://t.me/example_bot?start=cpa_mnopqrstuvwx"


def test_rewrite_promotion_source_url_rejects_ambiguous_binding() -> None:
    with pytest.raises(PromotionInvariantViolation):
        rewrite_promotion_source_url(
            "https://example.test/clientplatform/acquire/cpa_abcdefghijkl"
            "?start=cpa_abcdefghijkl",
            from_token=SOURCE,
            to_token=REBOUND,
        )
