from __future__ import annotations

import importlib.util
import json
import sqlite3
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import native_member_interactions as native_member_ui
from clientplatform.application.admin_ops import PublicationCalendarProjection, PublicationRecord
from clientplatform.application.native_member_interactions import (
    NativeMemberBridgeRejected,
    NativeMemberResolution,
    parse_native_member_interaction,
    resolve_native_member,
)
from clientplatform.domain.connections import ConnectionPlatform, DispatchStatus
from clientplatform.domain.customer_interactions import CustomerInteractionMessage
from clientplatform.domain.messenger_channels import MessengerIngressRoute
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.infrastructure import DispatchOutboxRepository
from clientplatform.infrastructure.unified_dispatch_outbox import ClaimedProviderDispatch
from services.messenger.bridge import BridgeResolution

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None


def _route(platform: ConnectionPlatform = ConnectionPlatform.VK) -> MessengerIngressRoute:
    return MessengerIngressRoute(
        id=str(uuid4()),
        business_id=str(uuid4()),
        connection_id=str(uuid4()),
        platform=platform,
        external_route_id="424242" if platform == ConnectionPlatform.VK else "551001",
        webhook_secret_reference="secret://env/TEST_WEBHOOK",
        confirmation_code_reference=(
            "secret://env/TEST_CONFIRMATION"
            if platform == ConnectionPlatform.VK
            else None
        ),
        status="active",
        created_by_member_id=str(uuid4()),
        created_at="2026-08-21T00:00:00+00:00",
        updated_at="2026-08-21T00:00:00+00:00",
    )


def _actor(route: MessengerIngressRoute, user_id: int = 101) -> TenantContext:
    return TenantContext(
        business_id=route.business_id,
        user_id=user_id,
        membership_id=str(uuid4()),
        role=PlatformRole.OWNER,
    )


class NativeMemberResolutionTests(unittest.TestCase):
    def test_existing_account_member_is_resolved_before_customer_path(self) -> None:
        route = _route()
        actor = _actor(route)
        with (
            patch(
                "clientplatform.application.native_member_interactions.resolve_account_for_identity",
                return_value=actor.user_id,
            ),
            patch(
                "clientplatform.application.native_member_interactions._member_context",
                return_value=actor,
            ),
        ):
            resolved = resolve_native_member(
                route=route,
                external_subject="700001",
                raw_text="клиенты",
                display_name="Владелец",
            )
        self.assertEqual(actor.user_id, resolved.account_id)
        self.assertEqual(actor, resolved.actor)
        self.assertFalse(resolved.linked)

    def test_bridge_does_not_rewrite_existing_customer_into_member(self) -> None:
        route = _route()
        with (
            patch(
                "clientplatform.application.native_member_interactions.resolve_account_for_identity",
                return_value=None,
            ),
            patch(
                "clientplatform.application.native_member_interactions._active_customer_identity_exists",
                return_value=True,
            ),
            patch(
                "clientplatform.application.native_member_interactions.consume_bridge_token_and_link"
            ) as consume,
        ):
            with self.assertRaises(NativeMemberBridgeRejected):
                resolve_native_member(
                    route=route,
                    external_subject="700002",
                    raw_text="/start bridge_member-token",
                )
        consume.assert_not_called()

    def test_bridge_preflights_membership_before_atomic_consume(self) -> None:
        route = _route(ConnectionPlatform.MAX)
        actor = _actor(route)
        preview = BridgeResolution(
            canonical_user_id=actor.user_id,
            token="member-token",
            consumed=False,
            target_platform="max",
        )
        consumed = replace(preview, consumed=True)
        with (
            patch(
                "clientplatform.application.native_member_interactions.resolve_account_for_identity",
                return_value=None,
            ),
            patch(
                "clientplatform.application.native_member_interactions._active_customer_identity_exists",
                return_value=False,
            ),
            patch(
                "clientplatform.application.native_member_interactions.resolve_bridge_token",
                return_value=preview,
            ),
            patch(
                "clientplatform.application.native_member_interactions._member_context",
                return_value=actor,
            ),
            patch(
                "clientplatform.application.native_member_interactions.consume_bridge_token_and_link",
                return_value=consumed,
            ) as consume,
        ):
            resolved = resolve_native_member(
                route=route,
                external_subject="900001",
                raw_text="start bridge_member-token",
            )
        self.assertTrue(resolved.linked)
        consume.assert_called_once_with(
            "member-token",
            platform="max",
            external_user_id="900001",
            display_name=None,
        )

    def test_unknown_member_text_returns_safe_menu_action(self) -> None:
        self.assertEqual("menu", parse_native_member_interaction("что тут можно?").action)

    def test_publications_render_canonical_calendar_without_placeholder(self) -> None:
        route = _route(ConnectionPlatform.MAX)
        actor = _actor(route)
        publication = PublicationRecord(
            id=str(uuid4()),
            business_id=actor.business_id,
            channel="vk",
            title="План на завтра",
            body="Текст",
            status="scheduled",
            created_at="2026-08-27T08:00:00+00:00",
            updated_at="2026-08-27T08:00:00+00:00",
            scheduled_at="2026-08-28T09:00:00+00:00",
            published_at=None,
            failed_at=None,
            failure_reason=None,
        )
        with (
            patch(
                "clientplatform.application.native_member_interactions.get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Moscow"),
            ),
            patch(
                "clientplatform.application.native_member_interactions.get_publication_calendar_projection",
                return_value=PublicationCalendarProjection(
                    entries=(publication,),
                    actionable_drafts=(),
                    draft_count=3,
                    scheduled_count=21,
                    published_count=7,
                    failed_count=2,
                    cancelled_count=1,
                ),
            ) as calendar,
        ):
            message = native_member_ui._growth_report_message(actor, "publications")

        calendar.assert_called_once_with(actor=actor)
        self.assertIn("Запланировано: 21", message.text)
        self.assertIn("Черновики: 3", message.text)
        self.assertIn("28.08.2026 12:00 · ВКонтакте · Запланировано", message.text)
        self.assertNotIn("ещё не подключ", message.text.casefold())
        commands = {button.command for row in message.rows for button in row}
        self.assertIn(f"cpm:publication-schedule:{publication.id}", commands)
        self.assertIn(f"cpm:publication-cancel:{publication.id}", commands)

    def test_native_publication_schedule_and_cancel_use_canonical_mutations(self) -> None:
        route = _route(ConnectionPlatform.VK)
        actor = _actor(route)
        publication_id = str(uuid4())
        parsed = parse_native_member_interaction(
            f"публикация {publication_id} 29.08.2026 12:00"
        )
        self.assertEqual("publication-schedule-text", parsed.action)
        self.assertEqual((publication_id, "29.08.2026 12:00"), parsed.args)

        scheduled = PublicationRecord(
            id=publication_id,
            business_id=actor.business_id,
            channel="max",
            title="План",
            body="Текст",
            status="scheduled",
            created_at="2026-08-28T08:00:00+00:00",
            updated_at="2026-08-28T08:00:00+00:00",
            scheduled_at="2026-08-29T09:00:00+00:00",
            published_at=None,
            failed_at=None,
            failure_reason=None,
        )
        with (
            patch(
                "clientplatform.application.native_member_interactions.schedule_publication",
                return_value=scheduled,
            ) as schedule,
            patch(
                "clientplatform.application.native_member_interactions.get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Moscow"),
            ),
        ):
            result = native_member_ui._publication_schedule_result(
                actor, publication_id, "29.08.2026 12:00"
            )
        schedule.assert_called_once_with(
            actor=actor,
            publication_id=publication_id,
            local_time="29.08.2026 12:00",
        )
        self.assertIn("✅ Публикация запланирована", result.text)
        self.assertIn("29.08.2026 12:00 · MAX · Запланировано", result.text)

        cancelled = replace(scheduled, status="cancelled")
        with patch(
            "clientplatform.application.native_member_interactions.cancel_publication_schedule",
            return_value=cancelled,
        ) as cancel:
            cancelled_message = native_member_ui._publication_cancel_result(
                actor, publication_id
            )
        cancel.assert_called_once_with(actor=actor, publication_id=publication_id)
        self.assertIn("План публикации «План» отменён", cancelled_message.text)
        self.assertIn("Ничего автоматически не отправлено", cancelled_message.text)


class NativeMemberDispatchRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.business_id = str(uuid4())
        self.connection_id = str(uuid4())
        self.member_id = 101
        self.subject = "700001"
        self.conn.executescript(
            """
            CREATE TABLE businesses(id TEXT PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE business_members(
                id TEXT PRIMARY KEY, business_id TEXT NOT NULL,
                user_id INTEGER NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE connections(
                id TEXT PRIMARY KEY, business_id TEXT NOT NULL,
                platform TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE accounts(account_id INTEGER PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE account_channel_identities(
                account_id INTEGER NOT NULL, platform TEXT NOT NULL,
                external_user_id TEXT NOT NULL
            );
            CREATE TABLE provider_dispatch_outbox(
                id TEXT PRIMARY KEY, business_id TEXT NOT NULL,
                platform TEXT NOT NULL, source_kind TEXT NOT NULL,
                source_id TEXT NOT NULL, logical_delivery_id TEXT,
                partner_campaign_id TEXT, partner_candidate_id TEXT,
                sales_followup_id TEXT, connection_id TEXT NOT NULL,
                recipient_kind TEXT NOT NULL, customer_identity_id TEXT,
                external_subject TEXT NOT NULL, payload_kind TEXT NOT NULL,
                payload_ref TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL,
                available_at TEXT NOT NULL, locked_at TEXT, lock_token TEXT,
                provider_message_id TEXT, last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                sent_at TEXT, dead_at TEXT,
                UNIQUE(business_id, idempotency_key)
            );
            """
        )
        self.conn.execute(
            "INSERT INTO businesses(id,status) VALUES(?, 'active')",
            (self.business_id,),
        )
        self.conn.execute(
            "INSERT INTO business_members(id,business_id,user_id,status) VALUES(?,?,?,'active')",
            (str(uuid4()), self.business_id, self.member_id),
        )
        self.conn.execute(
            "INSERT INTO accounts(account_id,status) VALUES(?, 'active')",
            (self.member_id,),
        )
        self.conn.execute(
            "INSERT INTO account_channel_identities(account_id,platform,external_user_id) VALUES(?, 'vk', ?)",
            (self.member_id, self.subject),
        )
        self.conn.execute(
            "INSERT INTO connections(id,business_id,platform,status) VALUES(?,?,'vk','active')",
            (self.connection_id, self.business_id),
        )
        self.repository = DispatchOutboxRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_member_interaction_is_idempotent_and_has_no_customer_identity(self) -> None:
        message = CustomerInteractionMessage(text="Рабочий кабинет")
        kwargs = dict(
            business_id=self.business_id,
            connection_id=self.connection_id,
            member_user_id=self.member_id,
            platform="vk",
            external_subject=self.subject,
            interaction=message,
            interaction_key="route:r:event:e:member:101:menu",
            now="2026-08-21T05:00:00+00:00",
        )
        first = self.repository.materialize_member_interaction(**kwargs)
        second = self.repository.materialize_member_interaction(**kwargs)
        self.assertEqual(first.id, second.id)
        row = self.conn.execute(
            "SELECT source_kind,recipient_kind,customer_identity_id,external_subject FROM provider_dispatch_outbox WHERE id=?",
            (first.id,),
        ).fetchone()
        self.assertEqual("member_interaction", row["source_kind"])
        self.assertEqual("external_subject", row["recipient_kind"])
        self.assertIsNone(row["customer_identity_id"])
        self.assertEqual(self.subject, row["external_subject"])

    def test_member_interaction_rejects_unlinked_member_identity(self) -> None:
        with self.assertRaises(ValueError):
            self.repository.materialize_member_interaction(
                business_id=self.business_id,
                connection_id=self.connection_id,
                member_user_id=202,
                platform="vk",
                external_subject=self.subject,
                interaction=CustomerInteractionMessage(text="Рабочий кабинет"),
                interaction_key="other-member",
            )

    def test_max_member_boundary_is_quarantined_instead_of_replayed(self) -> None:
        max_connection_id = str(uuid4())
        max_subject = "900001"
        self.conn.execute(
            "INSERT INTO account_channel_identities(account_id,platform,external_user_id) VALUES(?, 'max', ?)",
            (self.member_id, max_subject),
        )
        self.conn.execute(
            "INSERT INTO connections(id,business_id,platform,status) VALUES(?,?,'max','active')",
            (max_connection_id, self.business_id),
        )
        dispatch = self.repository.materialize_member_interaction(
            business_id=self.business_id,
            connection_id=max_connection_id,
            member_user_id=self.member_id,
            platform="max",
            external_subject=max_subject,
            interaction=CustomerInteractionMessage(text="Рабочий кабинет"),
            interaction_key="max-member-menu",
            now="2026-08-21T05:00:00+00:00",
        )
        self.conn.execute(
            "UPDATE provider_dispatch_outbox SET status='sending',locked_at=?,lock_token=? WHERE id=?",
            ("2026-08-21T05:00:00+00:00", "lease-1", dispatch.id),
        )
        claimed = ClaimedProviderDispatch(
            dispatch=replace(
                dispatch,
                status=DispatchStatus.SENDING,
                locked_at="2026-08-21T05:00:00+00:00",
                lock_token="lease-1",
            ),
            external_subject=max_subject,
            credential_reference="secret://member/max",
        )
        self.assertTrue(
            self.repository.mark_provider_non_replay_boundary(
                claimed,
                now="2026-08-21T05:00:01+00:00",
            )
        )
        quarantined = self.repository._quarantine_stale_member_interaction_boundaries(
            stale_before="2026-08-21T05:00:02+00:00",
            now="2026-08-21T05:00:03+00:00",
        )
        self.assertEqual(1, quarantined)
        row = self.conn.execute(
            "SELECT status,last_error FROM provider_dispatch_outbox WHERE id=?",
            (dispatch.id,),
        ).fetchone()
        self.assertEqual("dead", row["status"])
        self.assertIn("ambiguous", row["last_error"])


class _FakeRequest:
    def __init__(self, payload: dict[str, object], *, route_id: str, headers: dict[str, str] | None = None) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.match_info = {"route_id": route_id}
        self.headers = headers or {}

    async def read(self) -> bytes:
        return self._raw


@unittest.skipUnless(
    _AIOHTTP_AVAILABLE,
    "aiohttp runtime dependency is not installed in dependency-light Canon",
)
class NativeMemberIngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_is_routed_before_customer_or_sales_admission(self) -> None:
        from clientplatform.runtime.messenger_channel_ingress import canonical_max_webhook

        route = _route(ConnectionPlatform.MAX)
        actor = _actor(route)
        resolution = NativeMemberResolution(actor=actor, account_id=actor.user_id)
        request = _FakeRequest(
            {
                "update_type": "message_created",
                "update_id": 91001,
                "timestamp": 1787265000000,
                "message": {
                    "body": {"mid": "member-1", "text": "клиенты"},
                    "sender": {"user_id": 900001, "first_name": "Owner"},
                },
            },
            route_id=route.id,
            headers={"X-Max-Bot-Api-Secret": "route-webhook-secret"},
        )
        with (
            patch(
                "clientplatform.runtime.messenger_channel_ingress.resolve_messenger_ingress_route",
                return_value=route,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.EnvironmentCredentialProvider.resolve",
                return_value="route-webhook-secret",
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.claim_inbound_event",
                return_value=True,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.resolve_native_member",
                return_value=resolution,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.process_native_member_interaction",
                return_value=True,
            ) as member_ui,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.ensure_channel_customer"
            ) as customer_admission,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.record_customer_channel_message"
            ) as sales,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.complete_inbound_event"
            ) as complete,
        ):
            response = await canonical_max_webhook(request)  # type: ignore[arg-type]

        self.assertEqual(200, response.status)
        member_ui.assert_called_once()
        customer_admission.assert_not_called()
        sales.assert_not_called()
        complete.assert_called_once()
