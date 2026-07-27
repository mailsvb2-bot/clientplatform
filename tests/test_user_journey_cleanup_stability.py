from __future__ import annotations

from typing import Any

import pytest

from scripts import probe_user_journey_e2e as probe


def test_cleanup_retries_after_concurrent_row_reappears(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup_results = iter((23, 2))
    residual_results = iter((0, 1, 0, 0))
    sleeps: list[float] = []
    cleanup_calls = 0

    def cleanup(**kwargs: Any) -> int:
        nonlocal cleanup_calls
        del kwargs
        cleanup_calls += 1
        return next(cleanup_results)

    def residual(**kwargs: Any) -> int:
        del kwargs
        return next(residual_results)

    monkeypatch.setattr(probe, "_cleanup_probe_rows", cleanup)
    monkeypatch.setattr(probe, "_residual_rows", residual)
    monkeypatch.setattr(probe.time, "sleep", sleeps.append)

    touched, remaining = probe._cleanup_probe_rows_until_stable(
        db=object(),
        assert_synthetic_user_id=lambda user_id: None,
        user_id=-975891434,
        payment_id="synthetic-probe-user-journey-test",
        max_attempts=3,
        settle_seconds=0.01,
    )

    assert touched == 25
    assert remaining == 0
    assert cleanup_calls == 2
    assert sleeps


def test_cleanup_reports_persistent_residual_after_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup_calls = 0

    def cleanup(**kwargs: Any) -> int:
        nonlocal cleanup_calls
        del kwargs
        cleanup_calls += 1
        return 1

    monkeypatch.setattr(probe, "_cleanup_probe_rows", cleanup)
    monkeypatch.setattr(probe, "_residual_rows", lambda **kwargs: 1)
    monkeypatch.setattr(probe.time, "sleep", lambda seconds: None)

    touched, remaining = probe._cleanup_probe_rows_until_stable(
        db=object(),
        assert_synthetic_user_id=lambda user_id: None,
        user_id=-975891434,
        payment_id="synthetic-probe-user-journey-test",
        max_attempts=3,
        settle_seconds=0.01,
    )

    assert touched == 3
    assert remaining == 1
    assert cleanup_calls == 3
