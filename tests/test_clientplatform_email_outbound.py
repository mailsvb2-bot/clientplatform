from __future__ import annotations

import asyncio
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import patch

from clientplatform.application.email_connections import provision_email_smtp_connection
from clientplatform.application.partner_scoring import score_partner
from clientplatform.domain.email_outbound import EmailPayload, normalize_email_address
from clientplatform.domain.tenancy import PlatformRole
from clientplatform.domain.partners import (
    ContactBasis,
    PartnerCampaignGoal,
    PartnerCandidate,
    PartnerChannel,
    PartnerContentPack,
    PartnerInvariantViolation,
)
from clientplatform.infrastructure import DispatchOutboxRepository
from clientplatform.infrastructure.connection_repository import ConnectionRepository
from clientplatform.infrastructure.partner_repository import PartnerRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.infrastructure.unified_dispatch_outbox import ClaimedProviderDispatch
from clientplatform.transport.email import (
    SmtpCredential,
    SmtpEmailDispatchAdapter,
)
from services.db.schema import (
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_partners,
    clientplatform_programs,
    clientplatform_provider_dispatch,
    clientplatform_tenancy,
)


@contextmanager
def _email_db_context(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except (sqlite3.Error, ValueError, RuntimeError):
        conn.rollback()
        raise


class _FakeEmailClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def probe(self, *, credential: SmtpCredential) -> None:
        self.calls.append((credential.sender_email, "probe", ""))

    async def send(
        self,
        *,
        credential: SmtpCredential,
        recipient: str,
        payload: EmailPayload,
        idempotency_key: str,
    ) -> str:
        self.calls.append((recipient, payload.subject, idempotency_key))
        return "<provider-message@example.test>"


class EmailOutboundFixture:
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
        access = self.tenancy.create_business(owner_user_id=7171, name="Email outbound")
        self.actor = self.tenancy.resolve_context(
            user_id=7171,
            business_id=access.business.id,
        )
        self.partner_repo = PartnerRepository(self.conn)
        self.campaign = self.partner_repo.create_campaign(
            actor=self.actor,
            name="B2B",
            goal=PartnerCampaignGoal(target_count=5, audience_terms=("wellbeing",)),
        )
        connections = ConnectionRepository(self.conn)
        pending = connections.create_connection(
            actor=self.actor,
            platform="email",
            connection_type="email_smtp",
            external_account_id="partners@example.test",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_EMAIL_TEST",
            permissions=("send_email",),
        )
        self.connection = connections.activate_connection(
            actor=self.actor,
            connection_id=pending.id,
        )
        self.outbox = DispatchOutboxRepository(self.conn)

    def candidate(
        self,
        *,
        email: str = "contact@example.org",
        basis: ContactBasis = ContactBasis.PUBLIC_BUSINESS_CONTACT,
        name: str = "Example Partner",
    ):
        provisional = PartnerCandidate(
            id=str(uuid4()),
            business_id=self.actor.business_id,
            campaign_id=self.campaign.id,
            name=name,
            source_url=f"https://example.org/{uuid4().hex}",
            audience_summary="employee wellbeing audience",
            recent_topic="",
            channel=PartnerChannel.EMAIL,
            contact_value=email,
            contact_basis=basis,
            follower_count=1000,
        )
        candidate = self.partner_repo.upsert_candidate(
            actor=self.actor,
            campaign=self.campaign,
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
            score=score_partner(provisional, self.campaign.goal),
        )
        self.partner_repo.save_content_pack(
            actor=self.actor,
            campaign_id=self.campaign.id,
            pack=PartnerContentPack(
                candidate_id=candidate.id,
                subject="Пилот ClientPlatform",
                outreach_message="Персональное деловое предложение.",
                ready_post="Post",
                followup_message="Follow-up",
                collaboration_angle="Pilot",
                cta="Reply",
            ),
        )
        return candidate

    def close(self) -> None:
        self.conn.close()


class ClientPlatformEmailOutboundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = EmailOutboundFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_reprovisioned_smtp_credentials_force_fresh_probe(self) -> None:
        self.assertEqual(self.fx.connection.status.value, "active")
        with (
            patch(
                "clientplatform.application.email_connections.get_db",
                side_effect=lambda: _email_db_context(self.fx.conn),
            ),
            patch(
                "clientplatform.application.email_connections.ConnectionCredentialStore.put",
                return_value=self.fx.connection.credential_reference,
            ),
        ):
            connection = provision_email_smtp_connection(
                actor=self.fx.actor,
                sender_email="partners@example.test",
                smtp_host="smtp.example.test",
                smtp_port=465,
                username="partners@example.test",
                password="replacement-secret",
                security="ssl",
            )
        self.assertEqual(connection.id, self.fx.connection.id)
        self.assertEqual(connection.status.value, "pending")
        row = self.fx.conn.execute(
            "SELECT status,last_success_at FROM connections WHERE id=?",
            (connection.id,),
        ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["last_success_at"])

    def test_address_normalization_rejects_header_injection(self) -> None:
        self.assertEqual(normalize_email_address("Test@Example.ORG"), "test@example.org")
        with self.assertRaises(ValueError):
            normalize_email_address("victim@example.org\nBcc: other@example.org")

    def test_public_business_email_requires_exact_owner_approval(self) -> None:
        candidate = self.fx.candidate()
        with self.assertRaisesRegex(
            PartnerInvariantViolation,
            "requires explicit owner approval",
        ):
            self.fx.outbox.materialize_partner_outreach(
                actor=self.fx.actor,
                candidate_id=candidate.id,
                connection_id=self.fx.connection.id,
            )

        dispatch = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.actor,
            candidate_id=candidate.id,
            connection_id=self.fx.connection.id,
            explicit_owner_approval=True,
        )
        self.assertEqual(dispatch.platform.value, "email")
        payload = EmailPayload.from_json(dispatch.payload_ref)
        self.assertEqual(payload.subject, "Пилот ClientPlatform")
        approval = self.fx.conn.execute(
            """
            SELECT recipient_fingerprint,content_fingerprint,status
            FROM partner_outreach_approvals
            WHERE business_id=? AND dispatch_id=?
            """,
            (self.fx.actor.business_id, dispatch.id),
        ).fetchone()
        self.assertIsNotNone(approval)
        self.assertEqual(len(approval["recipient_fingerprint"]), 64)
        self.assertEqual(len(approval["content_fingerprint"]), 64)
        self.assertEqual(approval["status"], "approved")
        raw = "|".join(str(value) for value in approval)
        self.assertNotIn(candidate.contact_value, raw)
        self.assertNotIn(payload.body, raw)

    def test_public_business_email_approval_is_owner_only(self) -> None:
        candidate = self.fx.candidate(email="owner-only@example.org")
        self.fx.tenancy.grant_member(
            actor=self.fx.actor,
            user_id=7172,
            role=PlatformRole.MARKETER,
        )
        marketer = self.fx.tenancy.resolve_context(
            user_id=7172,
            business_id=self.fx.actor.business_id,
        )
        with self.assertRaisesRegex(
            PartnerInvariantViolation,
            "requires the business owner",
        ):
            self.fx.outbox.materialize_partner_outreach(
                actor=marketer,
                candidate_id=candidate.id,
                connection_id=self.fx.connection.id,
                explicit_owner_approval=True,
            )

    def test_opted_in_email_does_not_need_public_contact_approval(self) -> None:
        candidate = self.fx.candidate(
            email="opted@example.org",
            basis=ContactBasis.OPTED_IN,
        )
        dispatch = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.actor,
            candidate_id=candidate.id,
            connection_id=self.fx.connection.id,
        )
        self.assertEqual(dispatch.platform.value, "email")
        count = self.fx.conn.execute(
            "SELECT COUNT(*) FROM partner_outreach_approvals WHERE business_id=?",
            (self.fx.actor.business_id,),
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_approval_is_revalidated_before_provider_boundary(self) -> None:
        candidate = self.fx.candidate(email="approved@example.org")
        dispatch = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.actor,
            candidate_id=candidate.id,
            connection_id=self.fx.connection.id,
            explicit_owner_approval=True,
            now="2026-08-28T16:00:00+00:00",
        )
        claimed = self.fx.outbox.claim_due(
            limit=10,
            now=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
        )
        item = next(
            row
            for row in claimed
            if isinstance(row, ClaimedProviderDispatch)
            and row.dispatch.id == dispatch.id
        )
        self.assertTrue(self.fx.outbox.partner_dispatch_still_authorized(item))
        self.fx.conn.execute(
            "UPDATE partner_outreach_approvals SET status='revoked',revoked_at=? WHERE dispatch_id=?",
            ("2026-08-28T16:00:01+00:00", dispatch.id),
        )
        self.assertFalse(self.fx.outbox.partner_dispatch_still_authorized(item))
        self.assertTrue(self.fx.outbox.cancel_revoked_leased_partner_outreach(item))
        row = self.fx.conn.execute(
            "SELECT status FROM provider_dispatch_outbox WHERE id=?", (dispatch.id,)
        ).fetchone()
        self.assertEqual(row["status"], "cancelled")

    def test_stale_email_after_provider_boundary_is_quarantined_not_replayed(self) -> None:
        candidate = self.fx.candidate(email="ambiguous@example.org")
        dispatch = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.actor,
            candidate_id=candidate.id,
            connection_id=self.fx.connection.id,
            explicit_owner_approval=True,
            now="2026-08-28T16:00:00+00:00",
        )
        claimed = self.fx.outbox.claim_due(
            limit=10,
            lock_ttl_seconds=60,
            now=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
        )
        item = next(
            row
            for row in claimed
            if isinstance(row, ClaimedProviderDispatch)
            and row.dispatch.id == dispatch.id
        )
        self.assertTrue(self.fx.outbox.mark_provider_non_replay_boundary(item))
        later = datetime(2026, 8, 28, 16, 2, tzinfo=timezone.utc)
        replay = self.fx.outbox.claim_due(limit=10, lock_ttl_seconds=60, now=later)
        self.assertFalse(
            any(
                isinstance(row, ClaimedProviderDispatch)
                and row.dispatch.id == dispatch.id
                for row in replay
            )
        )
        row = self.fx.conn.execute(
            "SELECT status,last_error FROM provider_dispatch_outbox WHERE id=?",
            (dispatch.id,),
        ).fetchone()
        self.assertEqual(row["status"], "dead")
        self.assertEqual(
            row["last_error"],
            "partner_email_delivery_outcome_ambiguous_manual_reconciliation_required",
        )

    def test_email_adapter_uses_payload_snapshot_and_idempotency_key(self) -> None:
        candidate = self.fx.candidate(email="send@example.org")
        dispatch = self.fx.outbox.materialize_partner_outreach(
            actor=self.fx.actor,
            candidate_id=candidate.id,
            connection_id=self.fx.connection.id,
            explicit_owner_approval=True,
            now="2026-08-28T16:00:00+00:00",
        )
        claimed = self.fx.outbox.claim_due(
            limit=10,
            now=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
        )
        item = next(
            row
            for row in claimed
            if isinstance(row, ClaimedProviderDispatch)
            and row.dispatch.id == dispatch.id
        )
        fake = _FakeEmailClient()
        adapter = SmtpEmailDispatchAdapter(fake)
        credential = SmtpCredential(
            host="smtp.example.org",
            port=465,
            username="partners@example.test",
            password="not-a-real-password",
            sender_email="partners@example.test",
            sender_name="ClientPlatform",
        ).to_json()
        provider_id = asyncio.run(adapter.send(item, credential))
        self.assertEqual(provider_id, "<provider-message@example.test>")
        self.assertEqual(fake.calls[0][0], "send@example.org")
        self.assertEqual(fake.calls[0][1], "Пилот ClientPlatform")
        self.assertEqual(fake.calls[0][2], dispatch.idempotency_key)


class ClientPlatformEmailOutboundMigrationTests(unittest.TestCase):
    def test_sqlite_migration_upgrades_existing_checks_without_rebuilding_business_data(self) -> None:
        import os
        import tempfile
        from services.migrations.clientplatform_email_outbound_v1 import apply as apply_email_migration

        fd, path = tempfile.mkstemp(prefix="cp-email-outbound-", suffix=".sqlite3")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            clientplatform_tenancy.ensure(conn)
            clientplatform_customers.ensure(conn)
            clientplatform_programs.ensure(conn)
            clientplatform_partners.ensure(conn)
            clientplatform_connections.ensure(conn)
            clientplatform_provider_dispatch.ensure(conn)
            tenancy = TenancyRepository(conn)
            access = tenancy.create_business(owner_user_id=8181, name="Migration tenant")
            actor = tenancy.resolve_context(
                user_id=8181, business_id=access.business.id
            )
            schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
            reverse = {
                "connections": (
                    ("CHECK(platform IN ('telegram', 'vk', 'max', 'email'))", "CHECK(platform IN ('telegram', 'vk', 'max'))"),
                    ("'vk_community', 'max_shared_bot', 'max_personal_bot',\n                'email_smtp'", "'vk_community', 'max_shared_bot', 'max_personal_bot'"),
                    ("OR (platform='max' AND connection_type IN (\n                    'max_shared_bot', 'max_personal_bot'\n                ))\n                OR (platform='email' AND connection_type='email_smtp')", "OR (platform='max' AND connection_type IN (\n                    'max_shared_bot', 'max_personal_bot'\n                ))"),
                ),
                "connection_credentials": (
                    ("CHECK(platform IN ('vk', 'max', 'email'))", "CHECK(platform IN ('vk', 'max'))"),
                    ("CHECK(purpose IN ('provider_token', 'webhook_secret', 'confirmation_code', 'smtp_credentials'))", "CHECK(purpose IN ('provider_token', 'webhook_secret', 'confirmation_code'))"),
                ),
                "provider_dispatch_outbox": (("CHECK(platform IN ('telegram', 'vk', 'max', 'email'))", "CHECK(platform IN ('telegram', 'vk', 'max'))"),),
                "partner_reply_events": (("CHECK(platform IN ('telegram', 'vk', 'max', 'email'))", "CHECK(platform IN ('telegram', 'vk', 'max'))"),),
            }
            conn.execute("PRAGMA writable_schema=ON")
            try:
                for table, replacements in reverse.items():
                    row = conn.execute(
                        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
                        (table,),
                    ).fetchone()
                    sql = str(row[0])
                    for old, new in replacements:
                        self.assertIn(old, sql)
                        sql = sql.replace(old, new)
                    conn.execute(
                        "UPDATE sqlite_schema SET sql=? WHERE type='table' AND name=?",
                        (sql, table),
                    )
                conn.execute(f"PRAGMA schema_version={schema_version + 1}")
            finally:
                conn.execute("PRAGMA writable_schema=OFF")
            conn.commit()
            conn.close()

            legacy = sqlite3.connect(path)
            legacy.row_factory = sqlite3.Row
            legacy.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                legacy.execute(
                    """
                    INSERT INTO connections(
                        id,business_id,platform,connection_type,external_account_id,
                        credential_reference,permissions_json,status,created_by_member_id,
                        created_at,updated_at
                    ) VALUES(?,?,'email','email_smtp','before@example.test',
                             'secret://env/CLIENTPLATFORM_SECRET_EMAIL_BEFORE','[]','pending',?,?,?)
                    """,
                    (str(uuid4()), actor.business_id, actor.membership_id,
                     "2026-08-28T16:00:00+00:00", "2026-08-28T16:00:00+00:00"),
                )
            legacy.rollback()
            apply_email_migration(legacy)
            created = ConnectionRepository(legacy).create_connection(
                actor=actor,
                platform="email",
                connection_type="email_smtp",
                external_account_id="after@example.test",
                credential_reference="secret://env/CLIENTPLATFORM_SECRET_EMAIL_AFTER",
                permissions=("send_email",),
            )
            self.assertEqual(created.platform.value, "email")
            approval_table = legacy.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='partner_outreach_approvals'"
            ).fetchone()
            self.assertIsNotNone(approval_table)
            self.assertEqual(
                legacy.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            legacy.close()
        finally:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    unittest.main()
