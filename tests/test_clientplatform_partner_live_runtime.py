from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from clientplatform.application.partner_runtime import (
    record_partner_reply_if_expected,
)
from clientplatform.application.partner_scoring import score_partner
from clientplatform.domain.bot_gateway import (
    ClaimedIngressEvent,
    IngressEvent,
    IngressEventStatus,
    ManagedBotRoute,
)
from clientplatform.domain.partners import (
    ContactBasis,
    PartnerCampaignGoal,
    PartnerCandidate,
    PartnerCandidateStatus,
    PartnerChannel,
    PartnerContentPack,
    PartnerInvariantViolation,
)
from clientplatform.infrastructure.connection_repository import ConnectionRepository
from clientplatform.infrastructure.partner_repository import PartnerRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.infrastructure.unified_dispatch_outbox import (
    ClaimedProviderDispatch,
    DispatchOutboxRepository,
)
from clientplatform.integrations.partner_discovery import (
    PartnerDiscoveryProviderError,
    PartnerDiscoveryQuery,
)
from clientplatform.integrations.partner_discovery_runtime import (
    VkConnectionPartnerDiscoveryProvider,
)
from clientplatform.runtime.bot_gateway import BotGatewayRuntimeConfig
from clientplatform.runtime.partner_aware_bot_gateway import ManagedBotGatewayRuntime
from services.db.schema import (
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_partners,
    clientplatform_programs,
    clientplatform_provider_dispatch,
    clientplatform_tenancy,
)


class _Credentials:
    def __init__(self, value: str = "secret-value") -> None:
        self.value = value
        self.references: list[str] = []

    def resolve(self, reference: str) -> str:
        self.references.append(reference)
        return self.value


class PartnerLiveRuntimeFixture:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_programs.ensure(self.conn)
        clientplatform_partners.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        clientplatform_provider_dispatch.ensure(self.conn)

        self.tenancy = TenancyRepository(self.conn)
        access = self.tenancy.create_business(owner_user_id=101, name="Partner test")
        self.owner = self.tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.partner_repo = PartnerRepository(self.conn)
        self.campaign = self.partner_repo.create_campaign(
            actor=self.owner,
            name="Partners",
            goal=PartnerCampaignGoal(
                target_count=10,
                audience_terms=("psychology",),
                target_url="https://example.test/offer",
            ),
        )
        self.connections = ConnectionRepository(self.conn)
        connection = self.connections.create_connection(
            actor=self.owner,
            platform="telegram",
            connection_type="telegram_shared_bot",
            external_account_id="partner-bot",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_PARTNER_TEST",
            permissions=("send_messages",),
        )
        self.connection = self.connections.activate_connection(
            actor=self.owner,
            connection_id=connection.id,
        )
        self.outbox = DispatchOutboxRepository(self.conn)

    def candidate(
        self,
        *,
        basis: ContactBasis,
        contact_value: str,
        competitor: bool = False,
    ):
        provisional = PartnerCandidate(
            id=str(uuid4()),
            business_id=self.owner.business_id,
            campaign_id=self.campaign.id,
            name=f"Partner {uuid4().hex[:6]}",
            source_url=f"https://example.test/{uuid4().hex}",
            audience_summary="psychology wellbeing audience",
            recent_topic="stress",
            channel=PartnerChannel.TELEGRAM,
            contact_value=contact_value,
            contact_basis=basis,
            follower_count=5000,
            competitor=competitor,
        )
        score = score_partner(provisional, self.campaign.goal)
        candidate = self.partner_repo.upsert_candidate(
            actor=self.owner,
            campaign=self.campaign,
            name=provisional.name,
            source_url=provisional.source_url,
            audience_summary=provisional.audience_summary,
            recent_topic=provisional.recent_topic,
            channel=provisional.channel,
            contact_value=provisional.contact_value,
            contact_basis=provisional.contact_basis,
            follower_count=provisional.follower_count,
            tags=provisional.tags,
            competitor=competitor,
            score=score,
        )
        self.partner_repo.save_content_pack(
            actor=self.owner,
            campaign_id=self.campaign.id,
            pack=PartnerContentPack(
                candidate_id=candidate.id,
                subject="Collaboration",
                outreach_message="Здравствуйте! Есть аккуратное предложение о сотрудничестве.",
                ready_post="Готовый пост для аудитории партнёра.",
                followup_message="Если тема актуальна, буду рад обсудить детали.",
                collaboration_angle="Полезный совместный материал",
                cta="Ответьте на это сообщение",
            ),
        )
        return candidate

    def close(self) -> None:
        self.conn.close()


class PartnerDispatchRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = PartnerLiveRuntimeFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_public_contact_never_authorizes_automatic_send(self) -> None:
        candidate = self.fx.candidate(
            basis=ContactBasis.PUBLIC_BUSINESS_CONTACT,
            contact_value="700001",
        )
        with self.assertRaises(PartnerInvariantViolation):
            self.fx.outbox.materialize_partner_outreach(
                actor=self.fx.owner,
                candidate_id=candidate.id,
                connection_id=self.fx.connection.id,
            )
        count = self.fx.conn.execute(
            "SELECT COUNT(*) FROM provider_dispatch_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_username_is_not_treated_as_verified_telegram_chat_id(self) -> None:
        candidate = self.fx.candidate(
            basis=ContactBasis.OPTED_IN,
            contact_value="@partner_username",
        )
        with self.assertRaisesRegex(PartnerInvariantViolation, "numeric chat id"):
            self.fx.outbox.materialize_partner_outreach(
                actor=self.fx.owner,
                candidate_id=candidate.id,
                connection_id=self.fx.connection.id,
            )

    def test_opted_in_numeric_chat_is_idempotent_and_sent_marks_contacted(self) -> None:
        candidate = self.fx.candidate(
            basis=ContactBasis.OPTED_IN,
            contact_value="700001",
        )
        first = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.owner,
            candidate_id=candidate.id,
            connection_id=self.fx.connection.id,
        )
        repeated = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.owner,
            candidate_id=candidate.id,
            connection_id=self.fx.connection.id,
        )
        self.assertEqual(first.id, repeated.id)
        claimed = self.fx.outbox.claim_due(limit=10)
        partner = [item for item in claimed if isinstance(item, ClaimedProviderDispatch)]
        self.assertEqual(len(partner), 1)
        sent = self.fx.outbox.mark_sent(
            partner[0],
            provider_message_id="telegram-message-1",
        )
        self.assertEqual(sent.status.value, "sent")
        refreshed = self.fx.partner_repo.get_candidate(
            actor=self.fx.owner,
            candidate_id=candidate.id,
        )
        self.assertEqual(refreshed.status, PartnerCandidateStatus.CONTACTED)

    def test_partner_retry_does_not_mark_contacted_or_attention_until_terminal(self) -> None:
        candidate = self.fx.candidate(
            basis=ContactBasis.EXISTING_RELATIONSHIP,
            contact_value="700002",
        )
        self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.owner,
            candidate_id=candidate.id,
            connection_id=self.fx.connection.id,
        )
        item = next(
            item
            for item in self.fx.outbox.claim_due(limit=10)
            if isinstance(item, ClaimedProviderDispatch)
        )
        retry = self.fx.outbox.reschedule(
            item,
            error="temporary network",
            max_attempts=3,
        )
        self.assertEqual(retry.status.value, "retry")
        refreshed = self.fx.partner_repo.get_candidate(
            actor=self.fx.owner,
            candidate_id=candidate.id,
        )
        self.assertEqual(refreshed.status, PartnerCandidateStatus.READY)
        connection = self.fx.connections.list_connections(actor=self.fx.owner)[0]
        self.assertEqual(connection.status.value, "active")

    def test_authenticated_reply_is_deduplicated_and_marks_candidate_replied(self) -> None:
        candidate = self.fx.candidate(
            basis=ContactBasis.OPTED_IN,
            contact_value="700003",
        )
        self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.owner,
            candidate_id=candidate.id,
            connection_id=self.fx.connection.id,
        )
        item = next(
            item
            for item in self.fx.outbox.claim_due(limit=10)
            if isinstance(item, ClaimedProviderDispatch)
        )
        self.fx.outbox.mark_sent(item, provider_message_id="telegram-message-2")

        @contextmanager
        def _db():
            yield self.fx.conn
            self.fx.conn.commit()

        with patch("clientplatform.application.partner_runtime.get_db", _db):
            first = record_partner_reply_if_expected(
                business_id=self.fx.owner.business_id,
                connection_id=self.fx.connection.id,
                external_subject="700003",
                provider_event_key="90001",
                reply_text="Да, интересно. Давайте обсудим.",
            )
            second = record_partner_reply_if_expected(
                business_id=self.fx.owner.business_id,
                connection_id=self.fx.connection.id,
                external_subject="700003",
                provider_event_key="90001",
                reply_text="duplicate",
            )
        self.assertEqual(first, candidate.id)
        self.assertEqual(second, candidate.id)
        count = self.fx.conn.execute(
            "SELECT COUNT(*) FROM partner_reply_events WHERE candidate_id=?",
            (candidate.id,),
        ).fetchone()[0]
        self.assertEqual(count, 1)
        refreshed = self.fx.partner_repo.get_candidate(
            actor=self.fx.owner,
            candidate_id=candidate.id,
        )
        self.assertEqual(refreshed.status, PartnerCandidateStatus.REPLIED)


class PartnerVkDiscoveryTests(unittest.TestCase):
    def test_vk_provider_resolves_reference_at_call_time_and_never_returns_secret(self) -> None:
        credentials = _Credentials("super-secret-token")
        captured: dict[str, object] = {}

        def request(url: str, params: dict[str, object]):
            captured["url"] = url
            captured["params"] = dict(params)
            return {
                "response": {
                    "items": [
                        {
                            "id": 123,
                            "name": "Психология и практика",
                            "screen_name": "psychology_practice",
                            "description": "Полезное сообщество о психологии",
                        }
                    ]
                }
            }

        provider = VkConnectionPartnerDiscoveryProvider(
            connection_id=str(uuid4()),
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_VK_TEST",
            credential_provider=credentials,
            request=request,
        )
        found = provider.discover(
            PartnerDiscoveryQuery(terms=("психология",), limit=10)
        )
        self.assertEqual(credentials.references, ["secret://env/CLIENTPLATFORM_SECRET_VK_TEST"])
        self.assertEqual(found[0].channel, PartnerChannel.VK)
        self.assertEqual(found[0].source_url, "https://vk.com/psychology_practice")
        self.assertEqual(captured["params"]["access_token"], "super-secret-token")
        self.assertNotIn("super-secret-token", repr(found))

    def test_vk_provider_error_exposes_only_code_not_secret_or_provider_message(self) -> None:
        secret = "never-leak-this-token"
        provider = VkConnectionPartnerDiscoveryProvider(
            connection_id=str(uuid4()),
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_VK_TEST",
            credential_provider=_Credentials(secret),
            request=lambda _url, _params: {
                "error": {
                    "error_code": 7,
                    "error_msg": f"permission denied token={secret}",
                }
            },
        )
        with self.assertRaises(PartnerDiscoveryProviderError) as caught:
            provider.discover(PartnerDiscoveryQuery(terms=("test",), limit=3))
        self.assertIn("7", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("permission denied", str(caught.exception))


class PartnerAwareGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_partner_reply_is_processed_before_customer_link_or_dispatcher(self) -> None:
        business_id = str(uuid4())
        connection_id = str(uuid4())
        managed_bot_id = str(uuid4())
        payload = {
            "update_id": 90001,
            "message": {
                "message_id": 1,
                "date": 1,
                "chat": {"id": 700003, "type": "private"},
                "from": {"id": 700003, "is_bot": False, "first_name": "Partner"},
                "text": "Да, интересно",
            },
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        event = IngressEvent(
            id=str(uuid4()),
            business_id=business_id,
            managed_bot_id=managed_bot_id,
            provider_update_id="90001",
            payload_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            payload_json=encoded,
            status=IngressEventStatus.PROCESSING,
            attempts=0,
            available_at="2026-08-10T00:00:00+00:00",
            created_at="2026-08-10T00:00:00+00:00",
            updated_at="2026-08-10T00:00:00+00:00",
            locked_at="2026-08-10T00:00:00+00:00",
            lock_token="lease",
        )
        route = ManagedBotRoute(
            managed_bot_id=managed_bot_id,
            business_id=business_id,
            connection_id=connection_id,
            external_bot_id="123456",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_PARTNER_TEST",
            webhook_secret_reference="secret://env/CLIENTPLATFORM_SECRET_PARTNER_WEBHOOK",
        )
        dispatcher = SimpleNamespace(workflow_data={}, feed_webhook_update=Mock())
        config = BotGatewayRuntimeConfig(
            enabled=True,
            path_prefix="/clientplatform/managed-bots",
            batch_size=10,
            interval_seconds=1.0,
            tick_timeout_seconds=30.0,
            lock_ttl_seconds=300,
            max_attempts=5,
            per_minute_limit=120,
            queue_limit=1000,
            max_payload_bytes=262144,
        )
        runtime = ManagedBotGatewayRuntime(dispatcher=dispatcher, config=config)
        with (
            patch(
                "clientplatform.runtime.partner_aware_bot_gateway.record_partner_reply_if_expected",
                return_value=str(uuid4()),
            ) as record_reply,
            patch(
                "clientplatform.runtime.partner_aware_bot_gateway.ensure_telegram_customer_link"
            ) as ensure_customer,
            patch(
                "clientplatform.runtime.partner_aware_bot_gateway.mark_ingress_event_processed"
            ) as mark_processed,
        ):
            await runtime._process_item(ClaimedIngressEvent(event=event, route=route))
        record_reply.assert_called_once()
        ensure_customer.assert_not_called()
        dispatcher.feed_webhook_update.assert_not_called()
        mark_processed.assert_called_once()
        self.assertEqual(runtime._processed, 1)


if __name__ == "__main__":
    unittest.main()
