from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from clientplatform.domain.ad_connections import (
    AdConnectionInvariantViolation,
    AdProvider,
    AdPublicationStatus,
    new_oauth_state,
    new_pkce_verifier,
    normalize_region_ids,
    pkce_challenge,
)
from clientplatform.domain.promotions import (
    PromotionChannel,
    PromotionCreative,
    stable_creative_id,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.ad_connection_repository import AdConnectionRepository
from clientplatform.infrastructure.ad_credential_vault import InMemoryAdCredentialVault
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.promotion_repository import PromotionRepository
from clientplatform.integrations.yandex_direct import (
    YandexDirectProvider,
    YandexOAuthConfig,
    YandexTokenBundle,
)
from clientplatform.privacy_manifest import validate_clientplatform_privacy_manifest
from services.db.schema import (
    clientplatform_activity,
    clientplatform_ad_connections,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_promotions,
    clientplatform_tenancy,
)


_NOW = datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict[str, str], object]]):
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, *, method, url, headers, body=None, timeout=20.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        status, response_headers, payload = self.responses.pop(0)
        encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return status, response_headers, encoded


class AdDomainAndProviderTests(unittest.TestCase):
    def test_pkce_and_regions_fail_closed(self) -> None:
        verifier = new_pkce_verifier()
        challenge = pkce_challenge(verifier)
        self.assertNotEqual(challenge, verifier)
        self.assertNotIn("=", challenge)
        self.assertEqual(normalize_region_ids("213, 47, 213"), (47, 213))
        with self.assertRaisesRegex(ValueError, "explicit advertising region"):
            normalize_region_ids(())
        with self.assertRaisesRegex(ValueError, "positive integer"):
            normalize_region_ids("0")

    def test_yandex_oauth_uses_pkce_and_never_puts_verifier_in_authorize_url(self) -> None:
        transport = FakeTransport(
            [
                (
                    200,
                    {},
                    {
                        "access_token": "token-value",
                        "token_type": "bearer",
                        "expires_in": 3600,
                        "refresh_token": "refresh-value",
                        "scope": "direct:api",
                    },
                ),
                (200, {}, {"id": "100500", "login": "vasya"}),
            ]
        )
        provider = YandexDirectProvider(
            oauth=YandexOAuthConfig(
                client_id="client-id",
                redirect_uri="https://app.clientplatform.ru/oauth/yandex-direct/callback",
            ),
            transport=transport,
        )
        state = new_oauth_state()
        verifier = new_pkce_verifier()
        authorization_url = provider.authorization_url(state=state, verifier=verifier)
        query = parse_qs(urlparse(authorization_url).query)
        self.assertEqual(query["state"], [state])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], [pkce_challenge(verifier)])
        self.assertNotIn(verifier, authorization_url)

        bundle = provider.exchange_code(code="confirmation-code", verifier=verifier)
        identity = provider.account_identity(access_token=bundle.access_token)
        self.assertEqual(identity.account_id, "100500")
        self.assertEqual(identity.login, "vasya")
        form = parse_qs(transport.calls[0]["body"].decode("ascii"))
        self.assertEqual(form["code_verifier"], [verifier])
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertNotIn("token-value", str(transport.calls[0]["body"]))

    def test_yandex_publication_reconciles_before_creating_remote_objects(self) -> None:
        transport = FakeTransport(
            [
                (200, {}, {"result": {"AdGroups": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 7001}]}}),
                (200, {}, {"result": {"Ads": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 8001}]}}),
            ]
        )
        provider = YandexDirectProvider(
            oauth=YandexOAuthConfig(
                client_id="client-id",
                redirect_uri="https://app.clientplatform.ru/oauth/yandex-direct/callback",
            ),
            transport=transport,
        )
        result = provider.publish_text_ad(
            access_token="secret-token",
            external_campaign_id="6001",
            region_ids=(47,),
            title="Замена раковины",
            text="Свободное время у сантехника. Запишитесь онлайн.",
            href="https://t.me/clientplatform_bot?start=cpa_source",
            idempotency_key="adjob_0123456789abcdef0123456789abcdef",
        )
        self.assertEqual(result.ad_group_id, "7001")
        self.assertEqual(result.ad_id, "8001")
        self.assertEqual(len(transport.calls), 4)
        for call in transport.calls:
            self.assertEqual(call["headers"]["Authorization"], "Bearer secret-token")
            self.assertNotIn("secret-token", str(call["body"]))
        add_group_payload = json.loads(transport.calls[1]["body"])
        self.assertEqual(
            add_group_payload["params"]["AdGroups"][0]["RegionIds"],
            [47],
        )
        add_ad_payload = json.loads(transport.calls[3]["body"])
        self.assertEqual(
            add_ad_payload["params"]["Ads"][0]["TextAd"]["Href"],
            "https://t.me/clientplatform_bot?start=cpa_source",
        )


class AdConnectionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_bookings.ensure(self.conn)
        clientplatform_promotions.ensure(self.conn)
        clientplatform_ad_connections.ensure(self.conn)
        self.vault = InMemoryAdCredentialVault()
        self.tenancy = TenancyRepository(self.conn)
        self.activity = ActivityRepository(self.conn)
        self.bookings = BookingRepository(self.conn)
        self.promotions = PromotionRepository(self.conn)
        self.ads = AdConnectionRepository(self.conn, vault=self.vault)
        access = self.tenancy.create_business(owner_user_id=101, name="Сантехник")
        self.owner = self.tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.activity.upsert_profile(
            actor=self.owner,
            activity_description="Ремонтирую сантехнику",
            timezone_name="Europe/Amsterdam",
            now="2026-08-05T08:00:00+00:00",
        )
        capability = self.activity.enable_capability(
            actor=self.owner,
            connector_key="services",
            now="2026-08-05T08:00:00+00:00",
        )
        offering = self.activity.create_offering(
            actor=self.owner,
            capability_id=capability.id,
            title="Замена раковины",
            description="Сниму старую и установлю новую раковину",
            now="2026-08-05T08:00:00+00:00",
        )
        self.slot = self.bookings.create_slot(
            actor=self.owner,
            offering_id=offering.id,
            local_start="10.08.2026 12:00",
            duration_minutes=60,
            now="2026-08-05T08:00:00+00:00",
        )
        creative = PromotionCreative(
            creative_id=stable_creative_id("sink", "website"),
            headline="Замена раковины",
            primary_text="Свободное время у сантехника. Запишитесь онлайн.",
            description="60 минут",
        )
        self.promotion, _ = self.promotions.create_or_refresh_campaign(
            actor=self.owner,
            slot_id=self.slot.slot.id,
            channel=PromotionChannel.WEBSITE,
            creative=creative,
            now="2026-08-05T08:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _connection(self):
        state = new_oauth_state()
        verifier = new_pkce_verifier()
        session = self.ads.create_oauth_session(
            actor=self.owner,
            provider=AdProvider.YANDEX_DIRECT,
            state=state,
            verifier=verifier,
            now=_NOW,
        )
        consumed, opened_verifier = self.ads.consume_oauth_session(
            state=state,
            now=_NOW,
        )
        self.assertEqual(session.state_hash, consumed.state_hash)
        self.assertEqual(opened_verifier, verifier)
        token_json = YandexTokenBundle(
            access_token="secret-token",
            token_type="bearer",
            expires_in=3600,
            refresh_token="refresh-token",
            scope=("direct:api",),
        ).to_json()
        connection = self.ads.activate_oauth_connection(
            session=consumed,
            external_account_id="100500",
            external_login="vasya",
            token_bundle_json=token_json,
            permissions=("campaigns.read", "adgroups.write", "ads.write"),
            now=_NOW,
        )
        stored = self.conn.execute(
            "SELECT credential_ciphertext FROM ad_connections WHERE id=?",
            (connection.id,),
        ).fetchone()[0]
        self.assertNotIn("secret-token", stored)
        self.assertEqual(
            YandexTokenBundle.from_json(self.ads.token_bundle(connection=connection)).access_token,
            "secret-token",
        )
        return connection

    def test_oauth_state_is_one_time_and_account_connections_are_owner_only(self) -> None:
        state = new_oauth_state()
        verifier = new_pkce_verifier()
        self.ads.create_oauth_session(
            actor=self.owner,
            provider=AdProvider.YANDEX_DIRECT,
            state=state,
            verifier=verifier,
            now=_NOW,
        )
        self.ads.consume_oauth_session(state=state, now=_NOW)
        with self.assertRaises(AdConnectionInvariantViolation):
            self.ads.consume_oauth_session(state=state, now=_NOW)

        marketer = TenantContext(
            business_id=self.owner.business_id,
            user_id=202,
            membership_id=self.owner.membership_id,
            role=PlatformRole.MARKETER,
        )
        with self.assertRaises(TenantPermissionDenied):
            self.ads.create_oauth_session(
                actor=marketer,
                provider=AdProvider.YANDEX_DIRECT,
                state=new_oauth_state(),
                verifier=new_pkce_verifier(),
                now=_NOW,
            )

    def test_publication_outbox_is_idempotent_and_claimed_once(self) -> None:
        connection = self._connection()
        kwargs = dict(
            actor=self.owner,
            promotion_campaign_id=self.promotion.id,
            connection_id=connection.id,
            external_campaign_id="6001",
            external_campaign_name="Локальные услуги",
            region_ids=(47,),
            source_url="https://t.me/clientplatform_bot?start=cpa_source",
            title=self.promotion.creative.headline,
            text=self.promotion.creative.primary_text,
            creative_id=self.promotion.creative.creative_id,
            now=_NOW,
        )
        first = self.ads.create_or_get_job(**kwargs)
        second = self.ads.create_or_get_job(**kwargs)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.status, AdPublicationStatus.DRAFT)

        queued = self.ads.queue_job(actor=self.owner, job_id=first.id, now=_NOW)
        self.assertEqual(queued.status, AdPublicationStatus.QUEUED)
        claimed = self.ads.claim_due_job(now=_NOW)
        self.assertIsNotNone(claimed)
        job, lock_token = claimed
        self.assertEqual(job.status, AdPublicationStatus.PUBLISHING)
        self.assertIsNone(self.ads.claim_due_job(now=_NOW))

        completed = self.ads.complete_job(
            job=job,
            lock_token=lock_token,
            external_ad_group_id="7001",
            external_ad_id="8001",
            now=_NOW,
        )
        self.assertEqual(completed.status, AdPublicationStatus.SUBMITTED)
        self.assertEqual(completed.external_ad_id, "8001")

    def test_privacy_manifest_covers_all_ad_tables(self) -> None:
        report = validate_clientplatform_privacy_manifest(
            self.conn,
            require_complete=False,
        )
        self.assertTrue(report.ok)
        self.assertIn("ad_connections", report.discovered_business_tables)
        self.assertIn("ad_oauth_sessions", report.discovered_business_tables)
        self.assertIn("ad_publication_jobs", report.discovered_business_tables)
        self.assertIn("ad_audit_events", report.discovered_business_tables)


if __name__ == "__main__":
    unittest.main()
