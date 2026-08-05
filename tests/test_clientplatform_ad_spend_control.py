from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from clientplatform.application.ad_spend_control import (
    AdSpendStopReason,
    evaluate_ad_spend_guard,
    grant_ad_spend_consent,
)
from clientplatform.domain.ad_connections import AdProvider
from clientplatform.domain.ad_spend import (
    AdSpendAuthorization,
    AdSpendAuthorizationStatus,
    AdSpendInvariantViolation,
    ProviderBudgetSnapshot,
)


NOW = datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)


def _snapshot(
    *,
    spent_today: int = 10,
    eligible: bool = True,
    captured_at: datetime = NOW,
    connection_id: str | None = None,
) -> ProviderBudgetSnapshot:
    return ProviderBudgetSnapshot(
        provider=AdProvider.YANDEX_DIRECT,
        connection_id=connection_id or str(uuid4()),
        external_account_id="account-1",
        external_campaign_id="123456",
        currency="RUB",
        available_budget_minor=50_000,
        spent_today_minor=spent_today,
        campaign_status="ON/ACCEPTED/ALLOWED",
        strategy="HIGHEST_POSITION/HIGHEST_POSITION",
        launch_eligible=eligible,
        provider_version="provider-v1",
        captured_at=captured_at,
        valid_until=captured_at + timedelta(minutes=5),
    )


def _authorization(
    snapshot: ProviderBudgetSnapshot,
    *,
    status: AdSpendAuthorizationStatus = AdSpendAuthorizationStatus.AUTHORIZED,
) -> AdSpendAuthorization:
    draft = AdSpendAuthorization.draft(
        authorization_id=str(uuid4()),
        business_id=str(uuid4()),
        publication_job_id=str(uuid4()),
        region_ids=(213,),
        hard_cap_minor=10_000,
        daily_cap_minor=2_000,
        authorization_expires_at=NOW + timedelta(minutes=4),
        snapshot=snapshot,
        created_by_member_id=str(uuid4()),
        now=NOW,
    )
    if status != AdSpendAuthorizationStatus.DRAFT:
        object.__setattr__(draft, "status", status)
    return draft


def test_guard_allows_only_fresh_matching_authorized_state() -> None:
    snapshot = _snapshot()
    authorization = _authorization(snapshot)
    decision = evaluate_ad_spend_guard(
        authorization=authorization,
        provider_snapshot=snapshot,
        total_spent_minor=100,
        now=NOW + timedelta(seconds=30),
    )
    assert decision.allowed is True
    assert decision.stop_reason is None


@pytest.mark.parametrize(
    ("spent_today", "total_spent", "reason"),
    [
        (2_000, 100, AdSpendStopReason.DAILY_CAP),
        (10, 10_000, AdSpendStopReason.HARD_CAP),
    ],
)
def test_guard_stops_at_server_side_caps(
    spent_today: int,
    total_spent: int,
    reason: AdSpendStopReason,
) -> None:
    snapshot = _snapshot(spent_today=spent_today)
    authorization = _authorization(snapshot)
    decision = evaluate_ad_spend_guard(
        authorization=authorization,
        provider_snapshot=snapshot,
        total_spent_minor=total_spent,
        now=NOW + timedelta(seconds=30),
    )
    assert decision.allowed is False
    assert decision.stop_reason == reason


def test_guard_fails_closed_for_stale_provider_evidence() -> None:
    fresh = _snapshot()
    authorization = _authorization(fresh)
    stale = _snapshot(
        captured_at=NOW - timedelta(minutes=10),
        connection_id=fresh.connection_id,
    )
    decision = evaluate_ad_spend_guard(
        authorization=authorization,
        provider_snapshot=stale,
        total_spent_minor=0,
        now=NOW,
    )
    assert decision.allowed is False
    assert decision.stop_reason == AdSpendStopReason.SNAPSHOT_STALE


def test_guard_rejects_provider_identity_mismatch() -> None:
    original = _snapshot()
    authorization = _authorization(original)
    changed = _snapshot(connection_id=str(uuid4()))
    decision = evaluate_ad_spend_guard(
        authorization=authorization,
        provider_snapshot=changed,
        total_spent_minor=0,
        now=NOW + timedelta(seconds=30),
    )
    assert decision.stop_reason == AdSpendStopReason.PROVIDER_INELIGIBLE


def test_grant_requires_exact_terms_and_snapshot_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    authorization = _authorization(
        snapshot,
        status=AdSpendAuthorizationStatus.DRAFT,
    )
    object.__setattr__(
        authorization,
        "status",
        AdSpendAuthorizationStatus.AWAITING_CONSENT,
    )

    class Repository:
        def __init__(self, _conn: object) -> None:
            pass

        def get(self, **_kwargs: object) -> AdSpendAuthorization:
            return authorization

    class Context:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        "clientplatform.application.ad_spend_control.get_db",
        lambda: Context(),
    )
    monkeypatch.setattr(
        "clientplatform.application.ad_spend_control.AdSpendRepository",
        Repository,
    )

    with pytest.raises(AdSpendInvariantViolation, match="terms changed"):
        grant_ad_spend_consent(
            actor=object(),  # type: ignore[arg-type]
            authorization_id=authorization.id,
            expected_terms_hash="wrong",
            expected_snapshot_hash=authorization.snapshot.snapshot_hash,
            now=NOW,
        )
