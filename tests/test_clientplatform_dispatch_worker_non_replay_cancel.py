from __future__ import annotations

from clientplatform.application.dispatch_worker import (
    _claims_releasable_after_cancel,
)


def test_cancel_before_provider_boundary_releases_current_and_remaining_claims() -> None:
    claims = ["current", "next", "later"]
    assert _claims_releasable_after_cancel(
        claims,
        current_index=0,
        non_replay_boundary_crossed=False,
    ) == claims


def test_cancel_after_non_replay_boundary_keeps_current_claim_for_quarantine() -> None:
    claims = ["current", "next", "later"]
    assert _claims_releasable_after_cancel(
        claims,
        current_index=0,
        non_replay_boundary_crossed=True,
    ) == ["next", "later"]


def test_cancel_after_non_replay_boundary_on_last_claim_releases_nothing() -> None:
    claims = ["first", "current"]
    assert _claims_releasable_after_cancel(
        claims,
        current_index=1,
        non_replay_boundary_crossed=True,
    ) == []
