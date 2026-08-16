from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from clientplatform.application import ad_connections as app
from clientplatform.domain.ad_connections import AdProvider
from clientplatform.domain.managed_ad_campaigns import (
    ManagedAdCampaign,
    ManagedAdCampaignStatus,
    managed_campaign_name,
    managed_campaign_provisioning_key,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexDirectProvider,
    YandexOAuthConfig,
    YandexTokenBundle,
)


class RecordingTransport:
    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, *, method, url, headers, body=None, timeout=20.0):
        payload = json.loads(body.decode("utf-8")) if body else None
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "payload": payload,
                "timeout": timeout,
            }
        )
        response = self.responses.pop(0)
        return 200, {}, json.dumps(response).encode("utf-8")


def provider(transport: RecordingTransport) -> YandexDirectProvider:
    return YandexDirectProvider(
        oauth=YandexOAuthConfig(
            client_id="client-id",
            redirect_uri="https://oauth.yandex.ru/verification_code",
        ),
        transport=transport,
    )


class YandexManagedProviderContractTests(unittest.TestCase):
    def test_managed_campaign_add_has_required_start_date_and_serving_off(self) -> None:
        transport = RecordingTransport({"result": {"AddResults": [{"Id": 7001}]}})
        direct = provider(transport)
        name = "ClientPlatform · cpmc_0123456789abcdef0123456789abcdef"

        campaign_id = direct.create_disabled_managed_campaign(
            access_token="token",
            campaign_name=name,
            start_date="2026-08-16",
        )

        self.assertEqual(campaign_id, "7001")
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://api.direct.yandex.com/json/v501/campaigns")
        item = call["payload"]["params"]["Campaigns"][0]
        self.assertEqual(item["Name"], name)
        self.assertEqual(item["StartDate"], "2026-08-16")
        strategy = item["UnifiedCampaign"]["BiddingStrategy"]
        self.assertEqual(strategy["Search"], {"BiddingStrategyType": "SERVING_OFF"})
        self.assertEqual(strategy["Network"], {"BiddingStrategyType": "SERVING_OFF"})

    def test_managed_text_ad_has_required_mobile_flag(self) -> None:
        transport = RecordingTransport({"result": {"AddResults": [{"Id": 9001}]}})
        direct = provider(transport)

        ad_id = direct._add_managed_ad(
            access_token="token",
            ad_group_id=8001,
            title="Консультация",
            text="Свободное время",
            href="https://example.test/offer",
        )

        self.assertEqual(ad_id, 9001)
        text_ad = transport.calls[0]["payload"]["params"]["Ads"][0]["TextAd"]
        self.assertEqual(text_ad["Mobile"], "NO")

    def test_managed_campaign_start_date_rejects_non_iso_input(self) -> None:
        transport = RecordingTransport({"result": {"AddResults": [{"Id": 7001}]}})
        direct = provider(transport)
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            direct.create_disabled_managed_campaign(
                access_token="token",
                campaign_name="ClientPlatform · cpmc_0123456789abcdef0123456789abcdef",
                start_date="16.08.2026",
            )
        self.assertEqual(transport.calls, [])


class ManagedProvisioningLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.business_id = str(uuid4())
        self.promotion_id = str(uuid4())
        self.connection_id = str(uuid4())
        self.member_id = str(uuid4())
        self.managed_id = str(uuid4())
        self.key = managed_campaign_provisioning_key(
            business_id=self.business_id,
            promotion_campaign_id=self.promotion_id,
            connection_id=self.connection_id,
        )

    def managed(self, *, status: ManagedAdCampaignStatus, updated_at: str) -> ManagedAdCampaign:
        return ManagedAdCampaign(
            id=self.managed_id,
            business_id=self.business_id,
            promotion_campaign_id=self.promotion_id,
            connection_id=self.connection_id,
            provider=AdProvider.YANDEX_DIRECT,
            provisioning_key=self.key,
            external_campaign_name=managed_campaign_name(self.key),
            status=status,
            last_error_code="provider_8000" if status == ManagedAdCampaignStatus.FAILED else None,
            created_by_member_id=self.member_id,
            created_at=updated_at,
            updated_at=updated_at,
        )

    @staticmethod
    def connection(*, status: str, updated_at: str) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE ad_managed_campaigns(
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL,
                status TEXT NOT NULL,
                last_error_code TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        return conn

    def insert(self, conn: sqlite3.Connection, managed: ManagedAdCampaign) -> None:
        conn.execute(
            """
            INSERT INTO ad_managed_campaigns(id, business_id, status, last_error_code, updated_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                managed.id,
                managed.business_id,
                managed.status.value,
                managed.last_error_code,
                managed.updated_at,
            ),
        )

    def test_failed_binding_is_claimed_once_with_compare_and_swap(self) -> None:
        now = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)
        managed = self.managed(
            status=ManagedAdCampaignStatus.FAILED,
            updated_at=(now - timedelta(seconds=5)).isoformat(timespec="seconds"),
        )
        conn = self.connection(status="failed", updated_at=managed.updated_at)
        self.insert(conn, managed)

        self.assertTrue(app._claim_managed_creation(conn, managed=managed, now=now))
        self.assertFalse(app._claim_managed_creation(conn, managed=managed, now=now))
        row = conn.execute(
            "SELECT status, last_error_code FROM ad_managed_campaigns WHERE id=?",
            (managed.id,),
        ).fetchone()
        self.assertEqual(row, ("provisioning", None))

    def test_fresh_provisioning_binding_is_not_stolen(self) -> None:
        now = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)
        managed = self.managed(
            status=ManagedAdCampaignStatus.PROVISIONING,
            updated_at=(now - timedelta(seconds=30)).isoformat(timespec="seconds"),
        )
        conn = self.connection(status="provisioning", updated_at=managed.updated_at)
        self.insert(conn, managed)

        self.assertFalse(app._claim_managed_creation(conn, managed=managed, now=now))

    def test_stale_provisioning_binding_can_be_recovered_once(self) -> None:
        now = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)
        managed = self.managed(
            status=ManagedAdCampaignStatus.PROVISIONING,
            updated_at=(now - timedelta(minutes=5)).isoformat(timespec="seconds"),
        )
        conn = self.connection(status="provisioning", updated_at=managed.updated_at)
        self.insert(conn, managed)

        self.assertTrue(app._claim_managed_creation(conn, managed=managed, now=now))
        self.assertFalse(app._claim_managed_creation(conn, managed=managed, now=now))


class ManagedProvisioningRefreshFailureTests(unittest.TestCase):
    def test_refresh_failure_releases_new_reservation_as_failed(self) -> None:
        business_id = str(uuid4())
        promotion_id = str(uuid4())
        connection_id = str(uuid4())
        member_id = str(uuid4())
        actor = TenantContext(
            business_id=business_id,
            user_id=101,
            membership_id=member_id,
            role=PlatformRole.OWNER,
        )
        key = managed_campaign_provisioning_key(
            business_id=business_id,
            promotion_campaign_id=promotion_id,
            connection_id=connection_id,
        )
        managed = ManagedAdCampaign(
            id=str(uuid4()),
            business_id=business_id,
            promotion_campaign_id=promotion_id,
            connection_id=connection_id,
            provider=AdProvider.YANDEX_DIRECT,
            provisioning_key=key,
            external_campaign_name=managed_campaign_name(key),
            status=ManagedAdCampaignStatus.PROVISIONING,
            created_by_member_id=member_id,
            created_at="2026-08-16T15:00:00+00:00",
            updated_at="2026-08-16T15:00:00+00:00",
        )
        connection = SimpleNamespace(
            id=connection_id,
            provider=AdProvider.YANDEX_DIRECT,
        )
        token = YandexTokenBundle(
            access_token="expired",
            token_type="bearer",
            expires_in=3600,
            refresh_token="refresh",
            scope=(),
        )
        selected_provider = MagicMock()
        selected_provider.find_managed_campaign.side_effect = YandexDirectError("provider_53")
        selected_provider.refresh.side_effect = YandexDirectError(
            "provider_transport_unavailable",
            retryable=True,
        )

        fake_conn = MagicMock()
        fake_cm = MagicMock()
        fake_cm.__enter__.return_value = fake_conn
        fake_cm.__exit__.return_value = False

        with (
            patch.object(app, "get_db", return_value=fake_cm),
            patch.object(app.TenancyRepository, "resolve_context", return_value=actor),
            patch.object(
                app.PromotionRepository,
                "get_campaign",
                return_value=SimpleNamespace(id=promotion_id),
            ),
            patch.object(
                app.AdConnectionRepository,
                "get_connection",
                return_value=connection,
            ),
            patch.object(app, "_reserve_managed_campaign", return_value=(managed, True)),
            patch.object(
                app.AdWorkerStore,
                "load_active",
                return_value=(connection, token.to_json()),
            ),
            patch.object(app, "_mark_managed_failure") as mark_failure,
        ):
            with self.assertRaises(YandexDirectError):
                app.ensure_yandex_managed_campaign(
                    actor=actor,
                    promotion_campaign_id=promotion_id,
                    connection_id=connection_id,
                    vault=MagicMock(),
                    provider=selected_provider,
                )

        mark_failure.assert_called_once_with(
            managed=managed,
            error_code="provider_transport_unavailable",
            uncertain=False,
        )
        selected_provider.create_disabled_managed_campaign.assert_not_called()


if __name__ == "__main__":
    unittest.main()
