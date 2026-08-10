from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from clientplatform.application import dispatch_worker, partner_runtime
from clientplatform.application.partner_scoring import score_partner
from clientplatform.domain.partners import (
    ContactBasis,
    PartnerCampaign,
    PartnerCampaignGoal,
    PartnerCandidate,
    PartnerCandidateStatus,
    PartnerChannel,
    PartnerContentPack,
    PartnerInvariantViolation,
)
from clientplatform.infrastructure import DispatchOutboxRepository
from clientplatform.infrastructure.connection_repository import ConnectionRepository
from clientplatform.infrastructure.partner_repository import PartnerRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.infrastructure.unified_dispatch_outbox import ClaimedProviderDispatch
from services.db.schema import (
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_partners,
    clientplatform_programs,
    clientplatform_provider_dispatch,
    clientplatform_tenancy,
)


class PartnerDispatchRevocationFixture:
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

        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=5151, name="Revocation test")
        self.actor = tenancy.resolve_context(
            user_id=5151,
            business_id=access.business.id,
        )
        self.partner_repo = PartnerRepository(self.conn)
        self.campaign = self.create_campaign("Partners")
        self.connections = ConnectionRepository(self.conn)
        self.connection_a = self._connection("bot-a")
        self.connection_b = self._connection("bot-b")
        self.outbox = DispatchOutboxRepository(self.conn)

    def create_campaign(self, name: str) -> PartnerCampaign:
        return self.partner_repo.create_campaign(
            actor=self.actor,
            name=name,
            goal=PartnerCampaignGoal(
                target_count=2,
                audience_terms=("psychology",),
            ),
        )

    def _connection(self, external_account_id: str):
        created = self.connections.create_connection(
            actor=self.actor,
            platform="telegram",
            connection_type="telegram_shared_bot",
            external_account_id=external_account_id,
            credential_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_"
                + external_account_id.upper().replace("-", "_")
            ),
            permissions=("send_messages",),
        )
        return self.connections.activate_connection(
            actor=self.actor,
            connection_id=created.id,
        )

    def candidate(
        self,
        chat_id: str = "7005151",
        *,
        campaign: PartnerCampaign | None = None,
        name: str = "Revocable Partner",
    ) -> PartnerCandidate:
        selected_campaign = campaign or self.campaign
        provisional = PartnerCandidate(
            id=str(uuid4()),
            business_id=self.actor.business_id,
            campaign_id=selected_campaign.id,
            name=name,
            source_url=f"https://example.test/{uuid4().hex}",
            audience_summary="psychology audience",
            recent_topic="",
            channel=PartnerChannel.TELEGRAM,
            contact_value=chat_id,
            contact_basis=ContactBasis.OPTED_IN,
            follower_count=500,
        )
        candidate = self.partner_repo.upsert_candidate(
            actor=self.actor,
            campaign=selected_campaign,
            name=provisional.name,
            source_url=provisional.source_url,
            audience_summary=provisional.audience_summary,
            recent_topic=provisional.recent_topic,
            channel=provisional.channel,
            contact_value=provisional.contact_value,
            contact_basis=provisional.contact_basis,
            follower_count=provisional.follower_count,
            tags=(),
            competitor=False,
            score=score_partner(provisional, selected_campaign.goal),
        )
        self.partner_repo.save_content_pack(
            actor=self.actor,
            campaign_id=selected_campaign.id,
            pack=PartnerContentPack(
                candidate_id=candidate.id,
                subject="Collaboration",
                outreach_message="Аккуратное партнёрское предложение.",
                ready_post="Post",
                followup_message="Follow-up",
                collaboration_angle="Collaboration",
                cta="Reply",
            ),
        )
        return candidate

    def close(self) -> None:
        self.conn.close()


class PartnerDispatchRevocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = PartnerDispatchRevocationFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_switching_bot_does_not_create_second_first_contact(self) -> None:
        candidate = self.fx.candidate()
        first = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.actor,
            candidate_id=candidate.id,
            connection_id=self.fx.connection_a.id,
        )
        second = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.actor,
            candidate_id=candidate.id,
            connection_id=self.fx.connection_b.id,
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.connection_id, self.fx.connection_a.id)
        self.assertEqual(second.connection_id, self.fx.connection_a.id)
        self.assertNotIn(candidate.contact_value, first.idempotency_key)
        total = self.fx.conn.execute(
            """
            SELECT COUNT(*) FROM provider_dispatch_outbox
            WHERE business_id=? AND source_kind='partner_outreach' AND source_id=?
            """,
            (self.fx.actor.business_id, candidate.id),
        ).fetchone()[0]
        self.assertEqual(total, 1)

    def test_same_recipient_in_another_campaign_cannot_get_second_first_contact(self) -> None:
        first = self.fx.candidate(chat_id="7006001", name="Partner A")
        other_campaign = self.fx.create_campaign("Second campaign")
        second = self.fx.candidate(
            chat_id="7006001",
            campaign=other_campaign,
            name="Partner A rediscovered",
        )
        created = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.actor,
            candidate_id=first.id,
            connection_id=self.fx.connection_a.id,
        )
        with self.assertRaisesRegex(
            PartnerInvariantViolation,
            "already has a first-contact attempt",
        ):
            self.fx.outbox.materialize_partner_outreach(
                actor=self.fx.actor,
                candidate_id=second.id,
                connection_id=self.fx.connection_b.id,
            )
        rows = self.fx.conn.execute(
            """
            SELECT id,source_id,external_subject,idempotency_key
            FROM provider_dispatch_outbox
            WHERE business_id=? AND source_kind='partner_outreach'
              AND external_subject='7006001'
            """,
            (self.fx.actor.business_id,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], created.id)
        self.assertEqual(rows[0]["source_id"], first.id)
        self.assertNotIn("7006001", rows[0]["idempotency_key"])

    def test_corrected_recipient_cancels_old_queue_and_claims_only_new_address(self) -> None:
        candidate = self.fx.candidate(chat_id="7007001")
        old = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.actor,
            candidate_id=candidate.id,
            connection_id=self.fx.connection_a.id,
        )
        self.fx.conn.execute(
            """
            UPDATE partner_candidates
            SET contact_value='7007002',contact_basis='opted_in'
            WHERE id=? AND business_id=?
            """,
            (candidate.id, self.fx.actor.business_id),
        )
        new = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.actor,
            candidate_id=candidate.id,
            connection_id=self.fx.connection_a.id,
        )
        self.assertNotEqual(old.id, new.id)
        self.assertNotEqual(old.idempotency_key, new.idempotency_key)

        claimed = self.fx.outbox.claim_due(limit=10)
        claimed_ids = [
            item.dispatch.id
            for item in claimed
            if isinstance(item, ClaimedProviderDispatch)
        ]
        self.assertEqual(claimed_ids, [new.id])
        rows = {
            row["id"]: row
            for row in self.fx.conn.execute(
                """
                SELECT id,status,last_error,external_subject
                FROM provider_dispatch_outbox
                WHERE id IN (?,?)
                """,
                (old.id, new.id),
            ).fetchall()
        }
        self.assertEqual(rows[old.id]["status"], "cancelled")
        self.assertEqual(
            rows[old.id]["last_error"],
            "partner_authorization_invalid",
        )
        self.assertEqual(rows[old.id]["external_subject"], "7007001")
        self.assertEqual(rows[new.id]["status"], "sending")
        self.assertEqual(rows[new.id]["external_subject"], "7007002")

    def test_do_not_contact_status_cancels_pending_dispatch_in_same_transaction(self) -> None:
        candidate = self.fx.candidate()
        dispatch = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.actor,
            candidate_id=candidate.id,
            connection_id=self.fx.connection_a.id,
        )

        @contextmanager
        def _db():
            yield self.fx.conn
            self.fx.conn.commit()

        with patch("clientplatform.application.partner_runtime.get_db", _db):
            partner_runtime.set_partner_candidate_status(
                actor=self.fx.actor,
                candidate_id=candidate.id,
                status=PartnerCandidateStatus.DO_NOT_CONTACT,
            )

        row = self.fx.conn.execute(
            "SELECT status,last_error FROM provider_dispatch_outbox WHERE id=?",
            (dispatch.id,),
        ).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["last_error"], "partner_contact_revoked")
        refreshed = self.fx.partner_repo.get_candidate(
            actor=self.fx.actor,
            candidate_id=candidate.id,
        )
        self.assertEqual(refreshed.status, PartnerCandidateStatus.DO_NOT_CONTACT)

    def test_claim_revalidates_contact_basis_and_recipient(self) -> None:
        candidate = self.fx.candidate()
        dispatch = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.actor,
            candidate_id=candidate.id,
            connection_id=self.fx.connection_a.id,
        )
        self.fx.conn.execute(
            """
            UPDATE partner_candidates
            SET contact_basis='none',contact_value='7009999'
            WHERE id=? AND business_id=?
            """,
            (candidate.id, self.fx.actor.business_id),
        )
        claimed = self.fx.outbox.claim_due(limit=10)
        partner_claims = [
            item for item in claimed if isinstance(item, ClaimedProviderDispatch)
        ]
        self.assertEqual(partner_claims, [])
        row = self.fx.conn.execute(
            "SELECT status,last_error FROM provider_dispatch_outbox WHERE id=?",
            (dispatch.id,),
        ).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["last_error"], "partner_authorization_invalid")

    def test_revoked_lease_is_cancelled_before_provider_boundary(self) -> None:
        candidate = self.fx.candidate()
        self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.actor,
            candidate_id=candidate.id,
            connection_id=self.fx.connection_a.id,
        )
        claimed = next(
            item
            for item in self.fx.outbox.claim_due(limit=10)
            if isinstance(item, ClaimedProviderDispatch)
        )
        self.assertTrue(self.fx.outbox.partner_dispatch_still_authorized(claimed))
        self.fx.partner_repo.set_candidate_status(
            actor=self.fx.actor,
            candidate_id=candidate.id,
            status=PartnerCandidateStatus.DO_NOT_CONTACT,
        )
        self.assertTrue(
            self.fx.outbox.cancel_revoked_leased_partner_outreach(claimed)
        )
        self.assertFalse(self.fx.outbox.partner_dispatch_still_authorized(claimed))
        row = self.fx.conn.execute(
            "SELECT status,lock_token FROM provider_dispatch_outbox WHERE id=?",
            (claimed.dispatch.id,),
        ).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["lock_token"])


class PartnerDispatchWorkerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoked_partner_claim_never_resolves_secret_or_calls_adapter(self) -> None:
        claimed = SimpleClaimFactory.build()
        credential_provider = SimpleCredentialProvider()
        adapter = AsyncMock()
        adapters = MagicMock()
        adapters.get.return_value = adapter

        fake_repository = MagicMock()
        fake_repository.claim_due.return_value = [claimed]
        fake_repository.cancel_revoked_leased_partner_outreach.return_value = True

        @contextmanager
        def _db():
            yield object()

        with (
            patch.object(dispatch_worker, "get_db", _db),
            patch.object(
                dispatch_worker,
                "DispatchOutboxRepository",
                return_value=fake_repository,
            ),
        ):
            result = await dispatch_worker.run_dispatch_batch(
                credential_provider=credential_provider,
                adapters=adapters,
                limit=1,
            )

        self.assertEqual(result.claimed, 1)
        self.assertEqual(result.sent, 0)
        self.assertEqual(result.retried, 0)
        self.assertEqual(result.dead, 0)
        self.assertEqual(credential_provider.calls, 0)
        adapters.get.assert_not_called()
        adapter.send.assert_not_awaited()


class SimpleCredentialProvider:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, _reference: str) -> str:
        self.calls += 1
        return "fixture-value"


class SimpleClaimFactory:
    @staticmethod
    def build() -> ClaimedProviderDispatch:
        from clientplatform.domain.connections import ConnectionPlatform, DispatchStatus
        from clientplatform.domain.programs import ContentKind
        from clientplatform.infrastructure.unified_dispatch_outbox import ProviderDispatch

        dispatch = ProviderDispatch(
            id=str(uuid4()),
            business_id=str(uuid4()),
            platform=ConnectionPlatform.TELEGRAM,
            source_kind="partner_outreach",
            source_id=str(uuid4()),
            connection_id=str(uuid4()),
            external_subject="7005151",
            payload_kind=ContentKind.TEXT,
            payload_ref="text",
            idempotency_key="partner:test:first-contact",
            status=DispatchStatus.SENDING,
            attempts=0,
            available_at="2026-08-10T00:00:00+00:00",
            created_at="2026-08-10T00:00:00+00:00",
            updated_at="2026-08-10T00:00:00+00:00",
            locked_at="2026-08-10T00:00:00+00:00",
            lock_token="lease-token",
        )
        return ClaimedProviderDispatch(
            dispatch=dispatch,
            external_subject=dispatch.external_subject,
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_TEST",
        )


if __name__ == "__main__":
    unittest.main()
