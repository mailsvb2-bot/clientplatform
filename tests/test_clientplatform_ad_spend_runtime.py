from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application.ad_spend_control import AdSpendStopReason
from clientplatform.application.ad_spend_runtime import (
    evaluate_runtime_spend_guard,
    provider_report_date,
)
from clientplatform.domain.ad_connections import AdProvider
from clientplatform.domain.ad_spend import (
    AdSpendAuthorization,
    AdSpendAuthorizationStatus,
    AdSpendInvariantViolation,
    ProviderBudgetSnapshot,
)
from clientplatform.runtime.health import clientplatform_ad_runtime_readiness


NOW = datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)


def _snapshot(
    *,
    captured_at: datetime,
    spent_today_minor: int,
    connection_id: str | None = None,
    account_id: str = "account-1",
) -> ProviderBudgetSnapshot:
    return ProviderBudgetSnapshot(
        provider=AdProvider.YANDEX_DIRECT,
        connection_id=connection_id or str(uuid4()),
        external_account_id=account_id,
        external_campaign_id="123456",
        currency="RUB",
        available_budget_minor=50_000,
        spent_today_minor=spent_today_minor,
        campaign_status="TEXT_CAMPAIGN:ON:ACCEPTED:ALLOWED",
        strategy="search=HIGHEST_POSITION;network=NETWORK_DEFAULT",
        launch_eligible=True,
        provider_version="provider-v1",
        captured_at=captured_at,
        valid_until=captured_at + timedelta(minutes=5),
    )


def _authorization(
    snapshot: ProviderBudgetSnapshot,
    *,
    hard_cap_minor: int = 1_000,
    daily_cap_minor: int = 1_000,
    now: datetime = NOW,
) -> AdSpendAuthorization:
    authorization = AdSpendAuthorization.draft(
        authorization_id=str(uuid4()),
        business_id=str(uuid4()),
        publication_job_id=str(uuid4()),
        region_ids=(213,),
        hard_cap_minor=hard_cap_minor,
        daily_cap_minor=daily_cap_minor,
        authorization_expires_at=now + timedelta(minutes=4),
        snapshot=snapshot,
        created_by_member_id=str(uuid4()),
        now=now,
    )
    object.__setattr__(
        authorization,
        "status",
        AdSpendAuthorizationStatus.AUTHORIZED,
    )
    return authorization


class AdSpendRuntimeGuardTests(unittest.TestCase):
    def test_report_date_uses_configured_provider_timezone(self) -> None:
        with patch.dict(
            os.environ,
            {"CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE": "Europe/Amsterdam"},
            clear=False,
        ):
            self.assertEqual(
                provider_report_date(
                    now=datetime(2026, 8, 5, 22, 30, tzinfo=timezone.utc)
                ),
                "2026-08-06",
            )

    def test_report_timezone_is_mandatory(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                AdSpendInvariantViolation,
                "REPORT_TIMEZONE",
            ):
                provider_report_date(now=NOW)

    def test_guard_counts_only_spend_since_owner_consent(self) -> None:
        original = _snapshot(captured_at=NOW, spent_today_minor=100)
        authorization = _authorization(original)
        fresh = _snapshot(
            captured_at=NOW + timedelta(seconds=30),
            spent_today_minor=600,
            connection_id=original.connection_id,
        )
        with patch.dict(
            os.environ,
            {"CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE": "UTC"},
            clear=False,
        ):
            decision = evaluate_runtime_spend_guard(
                authorization=authorization,
                provider_snapshot=fresh,
                now=NOW + timedelta(seconds=30),
            )
        self.assertTrue(decision.allowed)

    def test_guard_stops_when_authorized_hard_cap_is_reached(self) -> None:
        original = _snapshot(captured_at=NOW, spent_today_minor=100)
        authorization = _authorization(original)
        fresh = _snapshot(
            captured_at=NOW + timedelta(seconds=30),
            spent_today_minor=1_100,
            connection_id=original.connection_id,
        )
        with patch.dict(
            os.environ,
            {"CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE": "UTC"},
            clear=False,
        ):
            decision = evaluate_runtime_spend_guard(
                authorization=authorization,
                provider_snapshot=fresh,
                now=NOW + timedelta(seconds=30),
            )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.stop_reason, AdSpendStopReason.HARD_CAP)

    def test_guard_fails_closed_when_provider_day_or_counter_changes(self) -> None:
        consent_time = datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc)
        original = _snapshot(
            captured_at=consent_time,
            spent_today_minor=500,
        )
        authorization = _authorization(original, now=consent_time)
        object.__setattr__(
            authorization,
            "authorization_expires_at",
            datetime(2026, 8, 6, 0, 30, tzinfo=timezone.utc).isoformat(),
        )
        fresh = _snapshot(
            captured_at=datetime(2026, 8, 5, 23, 0, tzinfo=timezone.utc),
            spent_today_minor=10,
            connection_id=original.connection_id,
        )
        with patch.dict(
            os.environ,
            {"CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE": "Europe/Amsterdam"},
            clear=False,
        ):
            decision = evaluate_runtime_spend_guard(
                authorization=authorization,
                provider_snapshot=fresh,
                now=datetime(2026, 8, 5, 23, 0, tzinfo=timezone.utc),
            )
        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.stop_reason,
            AdSpendStopReason.PROVIDER_INELIGIBLE,
        )


class AdSpendRuntimeReadinessTests(unittest.TestCase):
    def test_disabled_runtime_does_not_degrade_readiness(self) -> None:
        ready, errors, flags = clientplatform_ad_runtime_readiness(
            {"clientplatform_ad_runtime_configured": False}
        )
        self.assertTrue(ready)
        self.assertEqual(errors, [])
        self.assertTrue(flags["clientplatform_ad_runtime_ready"])

    def test_configured_runtime_requires_configuration_and_running_worker(self) -> None:
        ready, errors, flags = clientplatform_ad_runtime_readiness(
            {
                "clientplatform_ad_runtime_configured": True,
                "clientplatform_ad_runtime_health_available": True,
                "clientplatform_ad_runtime_configuration_ok": False,
                "clientplatform_ad_runtime_running": False,
            }
        )
        self.assertFalse(ready)
        self.assertIn(
            "clientplatform_ad_runtime:configuration_invalid",
            errors,
        )
        self.assertIn("clientplatform_ad_runtime:not_running", errors)
        self.assertIn("clientplatform_ad_spend_outbox:unavailable", errors)
        self.assertTrue(flags["clientplatform_ad_runtime_degraded"])

    def test_healthy_runtime_is_ready(self) -> None:
        ready, errors, flags = clientplatform_ad_runtime_readiness(
            {
                "clientplatform_ad_runtime_configured": True,
                "clientplatform_ad_runtime_health_available": True,
                "clientplatform_ad_runtime_configuration_ok": True,
                "clientplatform_ad_runtime_running": True,
                "clientplatform_ad_runtime_iterations": 5,
                "clientplatform_ad_runtime_errors": 0,
                "clientplatform_ad_runtime_last_error": "",
                "clientplatform_ad_runtime_last_tick_age_seconds": 1,
                "clientplatform_ad_spend_outbox_available": True,
                "clientplatform_ad_spend_outbox_due": 0,
                "clientplatform_ad_spend_outbox_stale_processing": 0,
                "clientplatform_ad_spend_outbox_recent_failed": 0,
                "clientplatform_ad_spend_outbox_oldest_due_age_seconds": 0,
            }
        )
        self.assertTrue(ready)
        self.assertEqual(errors, [])
        self.assertTrue(flags["clientplatform_ad_runtime_ready"])


if __name__ == "__main__":
    unittest.main()
