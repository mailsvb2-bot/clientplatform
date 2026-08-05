from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from clientplatform.application import ad_spend as application
from clientplatform.domain.ad_connections import (
    AdConnection,
    AdConnectionStatus,
    AdProvider,
)
from clientplatform.domain.ad_spend import AdSpendInvariantViolation
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.infrastructure.ad_credential_vault import InMemoryAdCredentialVault
from clientplatform.infrastructure.ad_spend_preparation_repository import (
    AdSpendPreparationTarget,
)
from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexTokenBundle,
)
from clientplatform.integrations.yandex_direct_budget import (
    YandexCampaignBudgetReadout,
    YandexDailySpendReadout,
)


_NOW = datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)


def _id() -> str:
    return str(uuid4())


def _owner() -> TenantContext:
    return TenantContext(
        business_id=_id(),
        user_id=101,
        membership_id=_id(),
        role=PlatformRole.OWNER,
    )


def _target(owner: TenantContext) -> AdSpendPreparationTarget:
    return AdSpendPreparationTarget(
        business_id=owner.business_id,
        publication_job_id=_id(),
        connection_id=_id(),
        external_account_id="100500",
        external_login="vasya",
        external_campaign_id="6001",
        region_ids=(47, 213),
    )


def _connection(owner: TenantContext, target: AdSpendPreparationTarget) -> AdConnection:
    stamp = _NOW.isoformat()
    return AdConnection(
        id=target.connection_id,
        business_id=owner.business_id,
        provider=AdProvider.YANDEX_DIRECT,
        external_account_id=target.external_account_id,
        external_login=target.external_login,
        permissions=("campaigns.read", "adgroups.write", "ads.write"),
        status=AdConnectionStatus.ACTIVE,
        created_by_member_id=owner.membership_id,
        created_at=stamp,
        updated_at=stamp,
        last_success_at=stamp,
    )


def _campaign(**overrides) -> YandexCampaignBudgetReadout:
    values = {
        "campaign_id": "6001",
        "currency": "RUB",
        "funds_mode": "CAMPAIGN_FUNDS",
        "available_budget_micros": 500_000_000,
        "total_spend_micros": None,
        "daily_budget_micros": 100_000_000,
        "campaign_type": "TEXT_CAMPAIGN",
        "state": "ON",
        "status": "ACCEPTED",
        "status_payment": "ALLOWED",
        "search_strategy": "HIGHEST_POSITION",
        "network_strategy": "NETWORK_DEFAULT",
        "captured_at": _NOW,
        "provider_version": "ycamp_" + "1" * 64,
    }
    values.update(overrides)
    return YandexCampaignBudgetReadout(**values)


def _daily(**overrides) -> YandexDailySpendReadout:
    values = {
        "campaign_id": "6001",
        "currency": "RUB",
        "report_date": "2026-08-05",
        "spend_micros": 12_340_000,
        "captured_at": _NOW,
        "provider_version": "yreport_" + "2" * 64,
    }
    values.update(overrides)
    return YandexDailySpendReadout(**values)


@contextmanager
def _db_context(connection):
    yield connection


class FakeBudgetProvider:
    def __init__(self, *, campaign=None, daily=None):
        self.campaign = campaign or _campaign()
        self.daily = daily or _daily()
        self.campaign_calls: list[dict[str, object]] = []
        self.daily_calls: list[dict[str, object]] = []
        self.refresh_calls: list[YandexTokenBundle] = []

    def campaign_budget_readout(self, **kwargs):
        self.campaign_calls.append(dict(kwargs))
        return self.campaign

    def daily_spend_readout(self, **kwargs):
        self.daily_calls.append(dict(kwargs))
        return self.daily

    def refresh(self, *, bundle: YandexTokenBundle) -> YandexTokenBundle:
        self.refresh_calls.append(bundle)
        return YandexTokenBundle(
            access_token="refreshed-token",
            token_type="bearer",
            expires_in=3600,
            refresh_token="refresh-token-2",
            scope=("direct:api",),
        )


class PrepareAdSpendAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner = _owner()
        self.target = _target(self.owner)
        self.connection = _connection(self.owner, self.target)
        self.bundle = YandexTokenBundle(
            access_token="access-token",
            token_type="bearer",
            expires_in=3600,
            refresh_token="refresh-token",
            scope=("direct:api",),
        )
        self.vault = InMemoryAdCredentialVault()
        self.read_conn = object()
        self.write_conn = object()
        self.preparation_repository = Mock()
        self.preparation_repository.load_submitted_target.return_value = (
            self.owner,
            self.target,
        )
        self.worker_store = Mock()
        self.worker_store.load_active.return_value = (
            self.connection,
            self.bundle.to_json(),
        )
        self.authorization = SimpleNamespace(id=_id(), status="draft")
        self.spend_repository = Mock()
        self.spend_repository.create_or_get_draft.return_value = self.authorization

    def _patches(self):
        return (
            patch.object(
                application,
                "get_db_ro",
                side_effect=lambda: _db_context(self.read_conn),
            ),
            patch.object(
                application,
                "get_db",
                side_effect=lambda: _db_context(self.write_conn),
            ),
            patch.object(
                application,
                "AdSpendPreparationRepository",
                return_value=self.preparation_repository,
            ),
            patch.object(
                application,
                "AdWorkerStore",
                return_value=self.worker_store,
            ),
            patch.object(
                application,
                "AdSpendRepository",
                return_value=self.spend_repository,
            ),
        )

    def test_success_uses_fresh_evidence_and_persists_bound_draft(self) -> None:
        provider = FakeBudgetProvider()
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = application.prepare_ad_spend_authorization(
                actor=self.owner,
                publication_job_id=self.target.publication_job_id,
                hard_cap_minor=20_000,
                daily_cap_minor=5_000,
                authorization_expires_at=_NOW + timedelta(minutes=2),
                provider_report_date="2026-08-05",
                now=_NOW,
                vault=self.vault,
                provider=provider,  # type: ignore[arg-type]
            )

        self.assertIs(result.authorization, self.authorization)
        self.assertEqual(result.snapshot.available_budget_minor, 50_000)
        self.assertEqual(result.snapshot.spent_today_minor, 1_234)
        self.assertEqual(len(provider.campaign_calls), 1)
        self.assertEqual(len(provider.daily_calls), 1)
        self.assertEqual(
            provider.campaign_calls[0]["external_campaign_id"],
            self.target.external_campaign_id,
        )
        self.assertEqual(
            provider.daily_calls[0]["client_login"],
            self.target.external_login,
        )
        self.assertEqual(provider.daily_calls[0]["report_date"], "2026-08-05")
        kwargs = self.spend_repository.create_or_get_draft.call_args.kwargs
        self.assertIs(kwargs["actor"], self.owner)
        self.assertEqual(kwargs["publication_job_id"], self.target.publication_job_id)
        self.assertEqual(kwargs["region_ids"], self.target.region_ids)
        self.assertEqual(kwargs["hard_cap_minor"], 20_000)
        self.assertEqual(kwargs["daily_cap_minor"], 5_000)
        self.assertIs(kwargs["snapshot"], result.snapshot)

    def test_pending_report_creates_no_local_authorization(self) -> None:
        provider = FakeBudgetProvider()

        def pending(**_kwargs):
            raise YandexDirectError("daily_spend_report_pending", retryable=True)

        provider.daily_spend_readout = pending  # type: ignore[method-assign]
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaises(YandexDirectError) as raised:
                application.prepare_ad_spend_authorization(
                    actor=self.owner,
                    publication_job_id=self.target.publication_job_id,
                    hard_cap_minor=20_000,
                    daily_cap_minor=5_000,
                    authorization_expires_at=_NOW + timedelta(minutes=2),
                    provider_report_date="2026-08-05",
                    now=_NOW,
                    vault=self.vault,
                    provider=provider,  # type: ignore[arg-type]
                )
        self.assertEqual(raised.exception.code, "daily_spend_report_pending")
        self.spend_repository.create_or_get_draft.assert_not_called()

    def test_auth_failure_refreshes_once_and_restarts_both_reads(self) -> None:
        provider = FakeBudgetProvider()
        calls = 0

        def campaign_readout(**kwargs):
            nonlocal calls
            calls += 1
            provider.campaign_calls.append(dict(kwargs))
            if calls == 1:
                raise YandexDirectError("provider_http_401")
            return provider.campaign

        provider.campaign_budget_readout = campaign_readout  # type: ignore[method-assign]
        refresh_store = Mock()
        patches = self._patches()
        worker_constructor = patch.object(
            application,
            "AdWorkerStore",
            side_effect=[self.worker_store, refresh_store],
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            worker_constructor,
            patches[4],
        ):
            result = application.prepare_ad_spend_authorization(
                actor=self.owner,
                publication_job_id=self.target.publication_job_id,
                hard_cap_minor=20_000,
                daily_cap_minor=5_000,
                authorization_expires_at=_NOW + timedelta(minutes=2),
                provider_report_date="2026-08-05",
                now=_NOW,
                vault=self.vault,
                provider=provider,  # type: ignore[arg-type]
            )

        self.assertIs(result.authorization, self.authorization)
        self.assertEqual(len(provider.refresh_calls), 1)
        self.assertEqual(len(provider.campaign_calls), 2)
        self.assertEqual(len(provider.daily_calls), 1)
        self.assertEqual(
            provider.daily_calls[0]["access_token"],
            "refreshed-token",
        )
        refresh_store.replace_token_bundle.assert_called_once()
        saved = refresh_store.replace_token_bundle.call_args.kwargs
        self.assertIs(saved["connection"], self.connection)
        self.assertIn("refreshed-token", saved["token_bundle_json"])

    def test_invalid_validity_or_report_day_fails_before_db_and_provider(self) -> None:
        provider = FakeBudgetProvider()
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaisesRegex(AdSpendInvariantViolation, "1 and 300"):
                application.prepare_ad_spend_authorization(
                    actor=self.owner,
                    publication_job_id=self.target.publication_job_id,
                    hard_cap_minor=20_000,
                    daily_cap_minor=5_000,
                    authorization_expires_at=_NOW + timedelta(minutes=6),
                    provider_report_date="2026-08-05",
                    now=_NOW,
                    vault=self.vault,
                    provider=provider,  # type: ignore[arg-type]
                )
            with self.assertRaisesRegex(AdSpendInvariantViolation, "account-day"):
                application.prepare_ad_spend_authorization(
                    actor=self.owner,
                    publication_job_id=self.target.publication_job_id,
                    hard_cap_minor=20_000,
                    daily_cap_minor=5_000,
                    authorization_expires_at=_NOW + timedelta(minutes=2),
                    provider_report_date="2026-08-01",
                    now=_NOW,
                    vault=self.vault,
                    provider=provider,  # type: ignore[arg-type]
                )
        self.preparation_repository.load_submitted_target.assert_not_called()
        self.assertEqual(provider.campaign_calls, [])

    def test_connection_account_change_blocks_provider_reads(self) -> None:
        changed = _connection(self.owner, self.target)
        object.__setattr__(changed, "external_account_id", "another-account")
        self.worker_store.load_active.return_value = (changed, self.bundle.to_json())
        provider = FakeBudgetProvider()
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaisesRegex(AdSpendInvariantViolation, "account changed"):
                application.prepare_ad_spend_authorization(
                    actor=self.owner,
                    publication_job_id=self.target.publication_job_id,
                    hard_cap_minor=20_000,
                    daily_cap_minor=5_000,
                    authorization_expires_at=_NOW + timedelta(minutes=2),
                    provider_report_date="2026-08-05",
                    now=_NOW,
                    vault=self.vault,
                    provider=provider,  # type: ignore[arg-type]
                )
        self.assertEqual(provider.campaign_calls, [])
        self.spend_repository.create_or_get_draft.assert_not_called()


if __name__ == "__main__":
    unittest.main()
