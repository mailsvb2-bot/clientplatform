from __future__ import annotations

import importlib.util
import json
import sqlite3
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
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
from clientplatform.domain.customers import CustomerPlatform
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


class NativeCustomerCardPrivacyTests(unittest.TestCase):
    def test_customer_card_hides_internal_external_product_binding_identity(self) -> None:
        route = _route(ConnectionPlatform.VK)
        actor = _actor(route)
        record = SimpleNamespace(
            customer=SimpleNamespace(
                display_name="Анна",
                status=SimpleNamespace(value="active"),
                created_at="2026-09-20T10:00:00+00:00",
            ),
            identities=(
                SimpleNamespace(
                    platform=CustomerPlatform.INTERNAL,
                    username=None,
                    display_name=None,
                    external_subject="extp:connector-uuid:opaque-fingerprint",
                ),
                SimpleNamespace(
                    platform=CustomerPlatform.PHONE,
                    username=None,
                    display_name=None,
                    external_subject="79991234567",
                ),
            ),
        )
        with (
            patch.object(native_member_ui, "get_customer", return_value=record),
            patch.object(native_member_ui, "get_customer_timeline", return_value=object()),
            patch.object(
                native_member_ui,
                "format_customer_timeline_lines",
                return_value=("• 20.09.2026 · Клиент добавлен",),
            ),
        ):
            message = native_member_ui._customer_message(actor, str(uuid4()))

        self.assertIn("Телефон: 79991234567", message.text)
        self.assertNotIn("extp:", message.text)
        self.assertNotIn("opaque-fingerprint", message.text)
        self.assertNotIn("ClientPlatform:", message.text)


class NativeOwnerInputSurfaceTests(unittest.TestCase):
    def test_managed_ingress_uses_route_scoped_pending_surface(self) -> None:
        route = _route(ConnectionPlatform.VK)
        actor = _actor(route)
        resolution = NativeMemberResolution(actor=actor, account_id=actor.user_id)
        db_context = MagicMock()
        db_context.__enter__.return_value = object()
        outbox = MagicMock()
        outbox.materialize_member_interaction.return_value = "queued"
        with (
            patch.object(native_member_ui, "resolve_tenant_context", return_value=actor),
            patch.object(
                native_member_ui,
                "_pending_owner_input",
                return_value=(native_member_ui.ParsedMemberInteraction("menu"), None),
            ) as pending,
            patch.object(
                native_member_ui,
                "_render",
                return_value=CustomerInteractionMessage(text="Меню"),
            ) as render,
            patch.object(native_member_ui, "get_db", return_value=db_context),
            patch.object(
                native_member_ui, "DispatchOutboxRepository", return_value=outbox
            ),
        ):
            native_member_ui.process_native_member_interaction(
                route=route,
                resolution=resolution,
                external_subject="700001",
                raw_text="обычное сообщение",
                provider_event_id="event-1",
            )

        expected_surface = f"route:{route.id}"
        self.assertEqual(pending.call_args.kwargs["surface"], expected_surface)
        self.assertEqual(render.call_args.kwargs["input_surface"], expected_surface)

    def test_textual_cancel_clears_managed_form_without_resolving_value(self) -> None:
        route = _route(ConnectionPlatform.MAX)
        actor = _actor(route)
        surface = f"route:{route.id}"
        session = SimpleNamespace(business_id=actor.business_id, action="activity_description")
        with (
            patch.object(native_member_ui, "get_owner_input_session", return_value=session),
            patch.object(native_member_ui, "clear_owner_input") as clear,
            patch.object(native_member_ui, "resolve_owner_input") as resolve,
        ):
            parsed, pending = native_member_ui._pending_owner_input(
                actor,
                platform=route.platform,
                surface=surface,
                raw_text="Отмена",
            )

        self.assertEqual(parsed.action, "owner-input-cancelled")
        self.assertIsNone(pending)
        resolve.assert_not_called()
        clear.assert_called_once_with(
            user_id=actor.user_id, platform="max", surface=surface
        )

    def test_webinar_wizard_callback_preserves_durable_pending_session(self) -> None:
        route = _route(ConnectionPlatform.VK)
        actor = _actor(route)
        surface = f"route:{route.id}"
        session = SimpleNamespace(business_id=actor.business_id, action="online_event")
        with (
            patch.object(native_member_ui, "get_owner_input_session", return_value=session),
            patch.object(native_member_ui, "clear_owner_input") as clear,
            patch.object(native_member_ui, "abandon_native_event_wizard") as abandon,
        ):
            parsed, pending = native_member_ui._pending_owner_input(
                actor,
                platform=route.platform,
                surface=surface,
                raw_text="cpm:event-wizard:count:3",
            )
        self.assertEqual(parsed.action, "event-wizard")
        self.assertEqual(parsed.args, ("count", "3"))
        self.assertIsNone(pending)
        clear.assert_not_called()
        abandon.assert_not_called()

    def test_webinar_text_cancel_abandons_draft_through_canonical_helper(self) -> None:
        route = _route(ConnectionPlatform.MAX)
        actor = _actor(route)
        surface = f"route:{route.id}"
        session = SimpleNamespace(business_id=actor.business_id, action="online_event")
        with (
            patch.object(native_member_ui, "get_owner_input_session", return_value=session),
            patch.object(native_member_ui, "clear_owner_input") as clear,
            patch.object(native_member_ui, "abandon_native_event_wizard", return_value=True) as abandon,
        ):
            parsed, pending = native_member_ui._pending_owner_input(
                actor,
                platform=route.platform,
                surface=surface,
                raw_text="Отмена",
            )
        self.assertEqual(parsed.action, "owner-input-cancelled")
        self.assertIsNone(pending)
        abandon.assert_called_once_with(actor, platform=route.platform, surface=surface)
        clear.assert_not_called()


class NativeFollowupContentParityTests(unittest.TestCase):
    def test_followup_content_message_exposes_owner_edit_and_reset(self) -> None:
        route = _route(ConnectionPlatform.VK)
        actor = _actor(route)
        event_id = str(uuid4())
        preview = SimpleNamespace(
            segment="attended_unpaid",
            segment_label="Были на вебинаре, но не купили",
            stage=2,
            offset_label="+24 часа",
            text="{name}, мой текст: {offer}",
            source="owner",
        )
        snapshot = SimpleNamespace(
            items=(SimpleNamespace(id=event_id, title="Практика"),)
        )
        with (
            patch.object(native_member_ui, "_business_name", return_value="Бизнес"),
            patch.object(native_member_ui, "resolve_events_snapshot", return_value=snapshot),
            patch.object(
                native_member_ui,
                "get_event_followup_content_plan",
                return_value=(preview,),
            ),
        ):
            message = native_member_ui._event_followup_content_message(
                actor, event_id, 0
            )

        self.assertIn("Дожим 1/1", message.text)
        self.assertIn("Источник: ваш текст", message.text)
        self.assertIn("Имя, мой текст: [ссылка на предложение]", message.text)
        commands = [
            button.command for row in message.rows for button in row
        ]
        self.assertIn(f"cpm:event-followup-edit:{event_id}:0", commands)
        self.assertIn(f"cpm:event-followup-reset:{event_id}:0", commands)

    def test_followup_edit_and_reset_use_canonical_owner_input_and_store(self) -> None:
        route = _route(ConnectionPlatform.MAX)
        actor = _actor(route)
        event_id = str(uuid4())
        surface = f"route:{route.id}"
        preview = SimpleNamespace(
            segment="no_show",
            segment_label="Не пришли",
            stage=1,
            offset_label="+1 час",
            text="Автотекст {offer}",
            source="template",
        )
        snapshot = SimpleNamespace(
            items=(SimpleNamespace(id=event_id, title="Практика"),)
        )

        with (
            patch.object(
                native_member_ui,
                "get_event_followup_content_plan",
                return_value=(preview,),
            ),
            patch.object(native_member_ui, "begin_owner_input") as begin,
        ):
            edit = native_member_ui._event_followup_edit_message(
                actor,
                event_id=event_id,
                index=0,
                current_platform=route.platform,
                input_surface=surface,
            )
        self.assertIn("Пришлите новый текст дожима", edit.text)
        begin.assert_called_once_with(
            actor=actor,
            platform="max",
            surface=surface,
            action="event_followup_text",
            context={
                "event_id": event_id,
                "index": "0",
                "segment": "no_show",
                "stage": "1",
            },
        )

        with (
            patch.object(
                native_member_ui,
                "get_event_followup_content_plan",
                return_value=(preview,),
            ),
            patch.object(native_member_ui, "reset_event_followup_text") as reset,
            patch.object(native_member_ui, "_business_name", return_value="Бизнес"),
            patch.object(native_member_ui, "resolve_events_snapshot", return_value=snapshot),
        ):
            result = native_member_ui._event_followup_reset_result(
                actor,
                event_id=event_id,
                index=0,
            )
        reset.assert_called_once_with(
            actor=actor,
            event_id=event_id,
            segment="no_show",
            stage=1,
        )
        self.assertIn("Дожим 1/1", result.text)


class NativeEventAnnouncementParityTests(unittest.TestCase):
    def test_ai_announcement_keeps_confirmation_and_attributed_links_on_vk(self) -> None:
        route = _route(ConnectionPlatform.VK)
        actor = _actor(route)
        draft = SimpleNamespace(
            text="Приходите на вебинар",
            generated_by="ai:test",
            registration_url=lambda *, public_base_url, source: (
                f"https://clientplatform.example.test/e/demo?source={source}"
            ),
        )
        with patch.object(
            native_member_ui,
            "draft_event_announcement_template",
            return_value=draft,
        ):
            message = native_member_ui._event_announcement_message(
                actor,
                "event-1",
                current_platform=ConnectionPlatform.VK,
            )

        self.assertIn("требует Вашего подтверждения", message.text)
        self.assertIn("source=vk", message.text)
        self.assertIn("source=ads", message.text)




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
        schedule_version = native_member_ui.encode_publication_schedule_version(
            publication.scheduled_at or ""
        )
        self.assertIn(
            f"cpm:publication-cancel:{publication.id}:{schedule_version}",
            commands,
        )

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
            result = native_member_ui._render(
                actor,
                parsed,
                linked=True,
                setup_issuer=None,
                setup_key="route:vk:event:91002:member:900001:action:schedule",
                current_platform=ConnectionPlatform.VK,
            )
        schedule.assert_called_once_with(
            actor=actor,
            publication_id=publication_id,
            local_time="29.08.2026 12:00",
            idempotency_key="route:vk:event:91002:member:900001:action:schedule",
        )
        self.assertIn("✅ Публикация запланирована", result.text)
        self.assertIn("29.08.2026 12:00 · MAX · Запланировано", result.text)

        cancelled = replace(scheduled, status="cancelled")
        schedule_version = native_member_ui.encode_publication_schedule_version(
            scheduled.scheduled_at or ""
        )
        confirm = native_member_ui._publication_cancel_confirm(
            actor, publication_id, schedule_version
        )
        confirm_commands = {button.command for row in confirm.rows for button in row}
        self.assertIn(
            f"cpm:publication-cancel-ok:{publication_id}:{schedule_version}",
            confirm_commands,
        )
        with patch(
            "clientplatform.application.native_member_interactions.cancel_publication_schedule",
            return_value=cancelled,
        ) as cancel:
            cancelled_message = native_member_ui._publication_cancel_result(
                actor, publication_id, schedule_version
            )
        cancel.assert_called_once_with(
            actor=actor,
            publication_id=publication_id,
            expected_scheduled_at=scheduled.scheduled_at,
        )
        self.assertIn("План публикации «План» отменён", cancelled_message.text)
        self.assertIn("Ничего автоматически не отправлено", cancelled_message.text)

        stale = native_member_ui._publication_cancel_result(
            actor, publication_id, "not-a-version"
        )
        self.assertIn("неактуальна", stale.text.casefold())

        with patch(
            "clientplatform.application.native_member_interactions.cancel_publication_schedule",
            side_effect=ValueError("publication schedule changed; refresh and retry"),
        ) as stale_cancel:
            stale_after_reschedule = native_member_ui._publication_cancel_result(
                actor, publication_id, schedule_version
            )
        stale_cancel.assert_called_once_with(
            actor=actor,
            publication_id=publication_id,
            expected_scheduled_at=scheduled.scheduled_at,
        )
        self.assertIn("неактуальна", stale_after_reschedule.text.casefold())


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


class NativeBusinessSettingsParityTests(unittest.TestCase):
    def test_vk_and_max_settings_expose_same_direction_and_delete_actions(self) -> None:
        for platform in (ConnectionPlatform.VK, ConnectionPlatform.MAX):
            with self.subTest(platform=platform.value):
                actor = _actor(_route(platform))
                message = native_member_ui._manage_message(actor)
                labels = [button.label for row in message.rows for button in row]
                commands = [button.command for row in message.rows for button in row]
                self.assertIn("🧭 Направления деятельности", labels)
                self.assertIn("🗑 Удалить организацию", labels)
                self.assertIn("cpm:directions:0", commands)
                self.assertIn("cpm:business-retire", commands)
                with patch.object(
                    native_member_ui,
                    "list_activity_directions",
                    return_value=[],
                ):
                    directions = native_member_ui._directions_message(actor)
                direction_labels = [
                    button.label for row in directions.rows for button in row
                ]
                self.assertIn("✏️ Описание организации", direction_labels)

    def test_admin_can_edit_direction_but_cannot_delete_business(self) -> None:
        actor = replace(_actor(_route(ConnectionPlatform.VK)), role=PlatformRole.ADMINISTRATOR)
        message = native_member_ui._manage_message(actor)
        labels = [button.label for row in message.rows for button in row]
        self.assertIn("🧭 Направления деятельности", labels)
        self.assertNotIn("🗑 Удалить организацию", labels)

    def test_business_delete_confirmation_is_explicit_and_preserves_history(self) -> None:
        actor = _actor(_route(ConnectionPlatform.MAX))
        with patch.object(native_member_ui, "_business_name", return_value="Сантехник"):
            message = native_member_ui._business_retire_confirm(actor)
        labels = [button.label for row in message.rows for button in row]
        self.assertIn("Удалить бизнес «Сантехник»?", message.text)
        self.assertIn("Оплаты, результаты и аудит не удаляются", message.text)
        self.assertIn("🗑 Да, удалить бизнес", labels)

    def test_business_delete_uses_canonical_archive_owner(self) -> None:
        actor = _actor(_route(ConnectionPlatform.VK))
        archived = SimpleNamespace(name="Сантехник")
        with patch.object(native_member_ui, "archive_business", return_value=archived) as archive:
            message = native_member_ui._business_retire_result(actor)
        archive.assert_called_once_with(actor=actor)
        self.assertIn("удалён из активных", message.text)
        self.assertIn("История сохранена", message.text)


class NativeEventHubParityTests(unittest.TestCase):
    @staticmethod
    def _snapshot(*, can_manage: bool = True, can_enable: bool = True):
        return SimpleNamespace(
            items=(
                SimpleNamespace(
                    title="Вебинар",
                    local_start="15.09.2026 19:00",
                    registered=10,
                    join_clicked=8,
                    attendance_confirmed=6,
                    offer_clicked=4,
                    paid=2,
                    revenue=(SimpleNamespace(display="10 000 RUB"),),
                ),
            ),
            commercial_followups_enabled=False,
            commercial_followups_effective=False,
            commercial_followups_platform_available=True,
            commercial_followup_segments=(
                "no_show",
                "join_signal_unpaid",
                "attended_unpaid",
                "offer_clicked_unpaid",
            ),
            commercial_followup_channels=("email", "max", "vk"),
            can_manage=can_manage,
            can_enable_commercial_followups=can_enable,
            can_expand_commercial_followups=can_enable,
            limitations=(),
        )

    def test_webinar_alias_opens_hub_instead_of_creation_wizard(self) -> None:
        self.assertEqual(parse_native_member_interaction("вебинар").action, "events")
        self.assertEqual(parse_native_member_interaction("онлайн-мероприятие").action, "events")

    def test_growth_menu_uses_canonical_webinar_hub_for_manager(self) -> None:
        route = _route(ConnectionPlatform.VK)
        manager = replace(_actor(route), role=PlatformRole.MANAGER)
        message = native_member_ui._growth_message(manager)
        commands = {button.command for row in message.rows for button in row}
        self.assertIn("cpm:events", commands)
        self.assertNotIn("cpm:event-new", commands)

    def test_event_hub_separates_read_access_from_creation(self) -> None:
        route = _route(ConnectionPlatform.MAX)
        manager = replace(_actor(route), role=PlatformRole.MANAGER)
        with (
            patch.object(native_member_ui, "_business_name", return_value="Бизнес"),
            patch.object(
                native_member_ui,
                "resolve_events_snapshot",
                return_value=self._snapshot(can_manage=False, can_enable=False),
            ) as resolve,
        ):
            message = native_member_ui._events_message(manager)
        resolve.assert_called_once_with(actor=manager, business_name="Бизнес", limit=5)
        self.assertIn("регистрации 10", message.text)
        self.assertNotIn("После включения — кому писать", message.text)
        self.assertIn("Вы можете смотреть результаты", message.text)
        commands = {button.command for row in message.rows for button in row}
        self.assertNotIn("cpm:event-new", commands)
        self.assertNotIn("cpm:event-settings", commands)

    def test_owner_event_hub_exposes_creation_and_settings_entry(self) -> None:
        route = _route(ConnectionPlatform.VK)
        owner = _actor(route)
        with (
            patch.object(native_member_ui, "_business_name", return_value="Бизнес"),
            patch.object(
                native_member_ui,
                "resolve_events_snapshot",
                return_value=self._snapshot(),
            ),
        ):
            message = native_member_ui._events_message(owner)
        commands = {button.command for row in message.rows for button in row}
        self.assertIn("cpm:event-new", commands)
        self.assertIn("cpm:event-settings", commands)
        self.assertNotIn("cpm:event-followups:on", commands)
        self.assertNotIn("cpm:event-channel:max:off", commands)

    def test_owner_event_settings_exposes_canonical_autosend_actions(self) -> None:
        route = _route(ConnectionPlatform.VK)
        owner = _actor(route)
        with (
            patch.object(native_member_ui, "_business_name", return_value="Бизнес"),
            patch.object(
                native_member_ui,
                "resolve_events_snapshot",
                return_value=self._snapshot(),
            ),
        ):
            message = native_member_ui._event_settings_message(owner)
        commands = {button.command for row in message.rows for button in row}
        self.assertIn("cpm:event-followups:on", commands)
        self.assertIn("cpm:event-channel:max:off", commands)

    def test_native_autosend_mutation_uses_canonical_setter_then_refreshes_settings(self) -> None:
        route = _route(ConnectionPlatform.MAX)
        owner = _actor(route)
        refreshed = CustomerInteractionMessage(text="refreshed")
        with (
            patch.object(native_member_ui, "set_business_event_followups_enabled") as setter,
            patch.object(native_member_ui, "_event_settings_message", return_value=refreshed) as refresh,
        ):
            result = native_member_ui._event_followups_action(owner, ("on",))
        setter.assert_called_once_with(actor=owner, enabled=True)
        refresh.assert_called_once_with(owner)
        self.assertIs(result, refreshed)
