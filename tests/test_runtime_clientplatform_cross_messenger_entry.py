from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application.control_callbacks import uuid_token
from clientplatform.application.tenancy import (
    create_business,
    grant_business_member,
    resolve_tenant_context as resolve_real_tenant_context,
)
from clientplatform.domain.activity import ActivityNotFound
from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from clientplatform.domain.tenancy import PlatformRole, TenantAccessDenied, TenantContext
from runtime import messenger_ingress_reliability as reliability
from services.db import get_db_ro
from services.messenger import reply_dispatcher
from services.messenger.clientplatform_entry import (
    handle_clientplatform_entry,
    parse_clientplatform_entry_command,
)
from services.messenger.text_ui import MessengerReply
from services.schema import init_db


class _FakeRequest:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload, ensure_ascii=False)
        self.headers: dict[str, str] = {}

    async def text(self) -> str:
        return self._body


B1 = "11111111-1111-1111-1111-111111111111"
B2 = "22222222-2222-2222-2222-222222222222"
B3 = "33333333-3333-3333-3333-333333333333"


class ClientPlatformCrossMessengerEntryTests(unittest.IsolatedAsyncioTestCase):
    def test_plain_start_is_recognized_in_vk_and_max(self) -> None:
        for text in ("start", "/start", "Старт", "начать", "главное меню"):
            with self.subTest(text=text):
                command = parse_clientplatform_entry_command(text)
                self.assertIsNotNone(command)
                assert command is not None
                self.assertEqual(command.action, "start")

    def test_max_bot_started_event_is_start_without_text(self) -> None:
        command = parse_clientplatform_entry_command(
            "",
            event_type="bot_started",
        )
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.action, "start")

    def test_explicit_owner_deep_link_is_owner_entry(self) -> None:
        for text in ("/start cpo_landing", "start cpo_site", "cpo_landing"):
            with self.subTest(text=text):
                command = parse_clientplatform_entry_command(text)
                self.assertIsNotNone(command)
                assert command is not None
                self.assertEqual(command.action, "start")
                self.assertTrue(command.value.startswith("cpo_"))

    def test_customer_acquisition_start_is_not_owner_entry(self) -> None:
        self.assertIsNone(
            parse_clientplatform_entry_command("/start cpa_sourceToken123")
        )
        self.assertIsNone(
            parse_clientplatform_entry_command("start cpj_customerInvite123")
        )

    def test_start_payload_is_preserved(self) -> None:
        command = parse_clientplatform_entry_command("/start bridge_abc")
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.action, "start")
        self.assertEqual(command.value, "bridge_abc")

    def test_vk_start_opens_canonical_owner_dashboard_not_legacy_product(self) -> None:
        entry = SimpleNamespace(user_id=101)
        access = SimpleNamespace(
            business=SimpleNamespace(id="business-101", name="Практика Анны")
        )
        actor = SimpleNamespace(user_id=101, business_id="business-101")
        interaction = CustomerInteractionMessage(
            text="🏠 Практика Анны\n\nClientPlatform показывает главное действие.",
            rows=((CustomerInteractionButton(label="💬 Мессенджеры", command="cpm:messengers"),),),
        )
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=[access],
            ),
            patch(
                "services.messenger.clientplatform_entry.resolve_tenant_context",
                return_value=actor,
            ),
            patch(
                "services.messenger.clientplatform_entry.render_native_member_interaction",
                return_value=interaction,
            ) as render,
        ):
            user_id, replies = handle_clientplatform_entry(
                101,
                platform="vk",
                external_user_id="vk-101",
                text="/start",
            )
        self.assertEqual(user_id, 101)
        self.assertEqual(replies[0].kind, "clientplatform_interaction")
        restored = CustomerInteractionMessage.from_json(replies[0].meta["interaction"])
        self.assertIn("ClientPlatform", restored.text)
        self.assertIn("Практика Анны", restored.text)
        self.assertEqual(restored.rows[0][0].command, "cpm:messengers")
        self.assertEqual(replies[0].meta["business_id"], "business-101")
        self.assertNotIn("Метротерап", restored.text)
        render.assert_called_once()
        self.assertEqual(render.call_args.kwargs["raw_text"], "cpm:menu")

    def test_native_owner_alias_is_routed_to_control_plane(self) -> None:
        command = parse_clientplatform_entry_command("мессенджеры")
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.action, "owner_control")
        self.assertEqual(command.value, "мессенджеры")

    def test_native_text_mutations_are_routed_to_owner_control(self) -> None:
        commands = (
            "программа Новый курс",
            f"урок {B1} текст | Введение | Материал урока",
            "оплата 1500 RUB",
            "черновик vk | Заголовок | Текст публикации",
            f"заметка {B1} Позвонить клиенту завтра",
        )
        for text in commands:
            with self.subTest(text=text):
                command = parse_clientplatform_entry_command(text)
                self.assertIsNotNone(command)
                assert command is not None
                self.assertEqual(command.action, "owner_control")
                self.assertEqual(command.value, text)

    def test_owner_mutation_interaction_key_is_unique_per_provider_event(self) -> None:
        entry = SimpleNamespace(user_id=909)
        access = SimpleNamespace(
            business=SimpleNamespace(id=B1, name="Практика Анны")
        )
        actor = SimpleNamespace(user_id=909, business_id=B1)
        interaction = CustomerInteractionMessage(text="✅ Платёж сохранён")
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=[access],
            ),
            patch(
                "services.messenger.clientplatform_entry.resolve_tenant_context",
                return_value=actor,
            ),
            patch(
                "services.messenger.clientplatform_entry.render_native_member_interaction",
                return_value=interaction,
            ) as render,
        ):
            for event_key in ("vk-event-1", "vk-event-2", "vk-event-1"):
                handle_clientplatform_entry(
                    909,
                    platform="vk",
                    external_user_id="vk-909",
                    text="оплата 1500 RUB",
                    event_key=event_key,
                )

        keys = [call.kwargs["interaction_key"] for call in render.call_args_list]
        self.assertNotEqual(keys[0], keys[1])
        self.assertEqual(keys[0], keys[2])

    def test_workspace_command_is_recognized_separately_from_native_action(self) -> None:
        command = parse_clientplatform_entry_command(
            f"cpw:act:{uuid_token(B1)}:cpm:messengers"
        )
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.action, "workspace")

    def test_multi_business_start_returns_server_resolved_selector(self) -> None:
        entry = SimpleNamespace(user_id=505)
        accesses = [
            SimpleNamespace(business=SimpleNamespace(id=B1, name="Практика Анны")),
            SimpleNamespace(business=SimpleNamespace(id=B2, name="Школа Анны")),
        ]
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=accesses,
            ),
        ):
            _, replies = handle_clientplatform_entry(
                505, platform="vk", external_user_id="505", text="start"
            )
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].kind, "clientplatform_interaction")
        self.assertNotIn("business_id", replies[0].meta)
        restored = CustomerInteractionMessage.from_json(replies[0].meta["interaction"])
        self.assertEqual(
            [row[0].command for row in restored.rows],
            [f"cpw:open:{uuid_token(B1)}", f"cpw:open:{uuid_token(B2)}"],
        )

    def test_multi_business_selection_revalidates_access_and_opens_selected_tenant(self) -> None:
        entry = SimpleNamespace(user_id=505)
        accesses = [
            SimpleNamespace(business=SimpleNamespace(id=B1, name="Практика Анны")),
            SimpleNamespace(business=SimpleNamespace(id=B2, name="Школа Анны")),
        ]
        actor = SimpleNamespace(user_id=505, business_id=B2)
        interaction = CustomerInteractionMessage(
            text="🏠 Школа Анны",
            rows=((CustomerInteractionButton(label="💬 Мессенджеры", command="cpm:messengers"),),),
        )
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=accesses,
            ),
            patch(
                "services.messenger.clientplatform_entry.resolve_tenant_context",
                return_value=actor,
            ) as resolve,
            patch(
                "services.messenger.clientplatform_entry.set_owner_control_workspace",
                return_value=B2,
            ) as remember,
            patch(
                "services.messenger.clientplatform_entry.render_native_member_interaction",
                return_value=interaction,
            ) as render,
        ):
            _, replies = handle_clientplatform_entry(
                505,
                platform="max",
                external_user_id="505",
                text=f"cpw:open:{uuid_token(B2)}",
            )
        resolve.assert_called_once_with(user_id=505, business_id=B2)
        remember.assert_called_once_with(user_id=505, platform="max", business_id=B2)
        self.assertEqual(render.call_args.kwargs["raw_text"], "cpm:menu")
        self.assertEqual(replies[0].meta["business_id"], B2)

    def test_scoped_multi_business_action_keeps_selected_tenant(self) -> None:
        entry = SimpleNamespace(user_id=505)
        accesses = [
            SimpleNamespace(business=SimpleNamespace(id=B1, name="Практика Анны")),
            SimpleNamespace(business=SimpleNamespace(id=B2, name="Школа Анны")),
        ]
        actor = SimpleNamespace(user_id=505, business_id=B1)
        interaction = CustomerInteractionMessage(text="Мессенджеры")
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=accesses,
            ),
            patch(
                "services.messenger.clientplatform_entry.resolve_tenant_context",
                return_value=actor,
            ) as resolve,
            patch(
                "services.messenger.clientplatform_entry.set_owner_control_workspace",
                return_value=B1,
            ) as remember,
            patch(
                "services.messenger.clientplatform_entry.render_native_member_interaction",
                return_value=interaction,
            ) as render,
        ):
            _, replies = handle_clientplatform_entry(
                505,
                platform="vk",
                external_user_id="505",
                text=f"cpw:act:{uuid_token(B1)}:cpm:messengers",
            )
        resolve.assert_called_once_with(user_id=505, business_id=B1)
        remember.assert_called_once_with(user_id=505, platform="vk", business_id=B1)
        self.assertEqual(render.call_args.kwargs["raw_text"], "cpm:messengers")
        self.assertEqual(replies[0].meta["business_id"], B1)

    def test_multi_business_plain_text_continuation_uses_server_saved_workspace(self) -> None:
        entry = SimpleNamespace(user_id=505)
        accesses = [
            SimpleNamespace(business=SimpleNamespace(id=B1, name="Практика Анны")),
            SimpleNamespace(business=SimpleNamespace(id=B2, name="Школа Анны")),
        ]
        actor = SimpleNamespace(user_id=505, business_id=B2)
        interaction = CustomerInteractionMessage(text="✅ Платёж сохранён")
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=accesses,
            ),
            patch(
                "services.messenger.clientplatform_entry.get_owner_control_workspace",
                return_value=B2,
            ) as selected,
            patch(
                "services.messenger.clientplatform_entry.resolve_tenant_context",
                return_value=actor,
            ) as resolve,
            patch(
                "services.messenger.clientplatform_entry.render_native_member_interaction",
                return_value=interaction,
            ) as render,
        ):
            _, replies = handle_clientplatform_entry(
                505,
                platform="max",
                external_user_id="505",
                text="оплата 1500 RUB",
                event_key="max-payment-1",
            )
        selected.assert_called_once_with(user_id=505, platform="max")
        resolve.assert_called_once_with(user_id=505, business_id=B2)
        self.assertEqual(render.call_args.kwargs["raw_text"], "оплата 1500 RUB")
        self.assertEqual(replies[0].meta["business_id"], B2)

    def test_tampered_or_inaccessible_business_token_fails_closed_to_selector(self) -> None:
        entry = SimpleNamespace(user_id=505)
        accesses = [SimpleNamespace(business=SimpleNamespace(id=B1, name="Практика Анны"))]
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=accesses,
            ),
            patch(
                "services.messenger.clientplatform_entry.resolve_tenant_context"
            ) as resolve,
        ):
            _, replies = handle_clientplatform_entry(
                505,
                platform="vk",
                external_user_id="505",
                text=f"cpw:open:{uuid_token(B3)}",
            )
        resolve.assert_not_called()
        self.assertIn("недоступен", replies[0].text)
        self.assertEqual(replies[1].kind, "clientplatform_interaction")

    def test_activity_description_is_channel_neutral_onboarding_step(self) -> None:
        command = parse_clientplatform_entry_command(
            "деятельность Ремонтирую автомобили и принимаю заказы"
        )
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.action, "describe_business")
        self.assertEqual(
            command.value,
            "Ремонтирую автомобили и принимаю заказы",
        )

    def test_max_new_user_can_begin_without_telegram(self) -> None:
        entry = SimpleNamespace(user_id=202)
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=[],
            ),
        ):
            _, replies = handle_clientplatform_entry(
                202,
                platform="max",
                external_user_id="max-202",
                text="",
                event_type="bot_started",
            )
        combined = " ".join(reply.text for reply in replies)
        self.assertIn("MAX", combined)
        self.assertIn("без перехода в Telegram", combined)
        self.assertIn("бизнес <название>", combined)
        self.assertEqual(replies[0].kind, "clientplatform_interaction")
        interaction = CustomerInteractionMessage.from_json(replies[0].meta["interaction"])
        self.assertEqual(interaction.rows[0][0].label, "Подключить мой бизнес")
        self.assertEqual(interaction.rows[0][0].command, "business")

    def test_business_command_creates_real_tenant(self) -> None:
        entry = SimpleNamespace(user_id=303)
        created = SimpleNamespace(
            business=SimpleNamespace(name="Автосервис Север")
        )
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=[],
            ),
            patch(
                "services.messenger.clientplatform_entry.create_business",
                return_value=created,
            ) as create,
        ):
            _, replies = handle_clientplatform_entry(
                303,
                platform="vk",
                external_user_id="vk-303",
                text="бизнес Автосервис Север",
            )
        create.assert_called_once_with(
            owner_user_id=303,
            name="Автосервис Север",
        )
        self.assertIn("создано", replies[0].text)
        self.assertIn("деятельность <описание>", replies[0].text)

    def test_business_command_retry_reuses_existing_tenant(self) -> None:
        entry = SimpleNamespace(user_id=303)
        existing = SimpleNamespace(
            business=SimpleNamespace(name="Автосервис Север")
        )
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=[existing],
            ),
            patch(
                "services.messenger.clientplatform_entry.create_business"
            ) as create,
        ):
            _, replies = handle_clientplatform_entry(
                303,
                platform="vk",
                external_user_id="vk-303",
                text="бизнес  автосервис   север",
            )
        create.assert_not_called()
        self.assertIn("уже существует", replies[0].text)

    def test_max_activity_step_routes_existing_profile_through_native_renderer(self) -> None:
        entry = SimpleNamespace(user_id=707)
        access = SimpleNamespace(
            business=SimpleNamespace(id=B1, name="Школа английского")
        )
        actor = TenantContext(
            business_id=B1,
            user_id=707,
            membership_id="77777777-7777-7777-7777-777777777777",
            role=PlatformRole.OWNER,
        )
        interaction = CustomerInteractionMessage(
            text="✅ Описание деятельности обновлено",
            rows=((CustomerInteractionButton(label="✍️ Тексты", command="cpm:copy"),),),
        )
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=[access],
            ),
            patch(
                "services.messenger.clientplatform_entry.resolve_tenant_context",
                return_value=actor,
            ),
            patch(
                "clientplatform.application.native_member_interactions.resolve_tenant_context",
                return_value=actor,
            ),
            patch(
                "services.messenger.clientplatform_entry.get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Tallinn"),
            ),
            patch(
                "services.messenger.clientplatform_entry.save_business_profile"
            ) as direct_save,
            patch(
                "services.messenger.clientplatform_entry.render_native_member_interaction",
                return_value=interaction,
            ) as render,
        ):
            _, replies = handle_clientplatform_entry(
                707,
                platform="max",
                external_user_id="max-707",
                text="деятельность Провожу уроки английского онлайн",
                event_key="max-activity-existing",
            )

        direct_save.assert_not_called()
        render.assert_called_once()
        self.assertEqual(
            render.call_args.kwargs["raw_text"],
            "деятельность Провожу уроки английского онлайн",
        )
        self.assertEqual(len(replies), 1)
        restored = CustomerInteractionMessage.from_json(replies[0].meta["interaction"])
        self.assertIn("Описание деятельности обновлено", restored.text)

    def test_first_activity_uses_default_timezone_when_profile_does_not_exist(self) -> None:
        entry = SimpleNamespace(user_id=707)
        access = SimpleNamespace(
            business=SimpleNamespace(id=B1, name="Новая практика")
        )
        actor = TenantContext(
            business_id=B1,
            user_id=707,
            membership_id="77777777-7777-7777-7777-777777777777",
            role=PlatformRole.OWNER,
        )
        interaction = CustomerInteractionMessage(text="🏠 Новая практика")
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=[access],
            ),
            patch(
                "services.messenger.clientplatform_entry.resolve_tenant_context",
                return_value=actor,
            ),
            patch(
                "services.messenger.clientplatform_entry.settings.TIMEZONE",
                "Europe/Tallinn",
            ),
            patch(
                "services.messenger.clientplatform_entry.get_business_profile",
                side_effect=ActivityNotFound("missing"),
            ),
            patch(
                "services.messenger.clientplatform_entry.save_business_profile"
            ) as save_profile,
            patch(
                "services.messenger.clientplatform_entry.render_native_member_interaction",
                return_value=interaction,
            ) as render,
        ):
            _, replies = handle_clientplatform_entry(
                707,
                platform="max",
                external_user_id="max-707",
                text="деятельность Новая частная практика",
                event_key="max-activity-first",
            )

        self.assertEqual(
            save_profile.call_args.kwargs["timezone_name"],
            "Europe/Tallinn",
        )
        self.assertEqual(render.call_count, 1)
        self.assertEqual(render.call_args.kwargs["raw_text"], "cpm:menu")
        self.assertEqual(len(replies), 2)

    def test_activity_update_preserves_existing_business_timezone(self) -> None:
        entry = SimpleNamespace(user_id=808)
        access = SimpleNamespace(
            business=SimpleNamespace(id=B1, name="Международная практика")
        )
        actor = TenantContext(
            business_id=B1,
            user_id=808,
            membership_id="88888888-8888-8888-8888-888888888888",
            role=PlatformRole.OWNER,
        )
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=[access],
            ),
            patch(
                "services.messenger.clientplatform_entry.resolve_tenant_context",
                return_value=actor,
            ),
            patch(
                "clientplatform.application.native_member_interactions.resolve_tenant_context",
                return_value=actor,
            ),
            patch(
                "services.messenger.clientplatform_entry.get_business_profile",
                return_value=SimpleNamespace(timezone="America/New_York"),
            ),
            patch(
                "clientplatform.application.native_member_interactions.get_business_profile",
                return_value=SimpleNamespace(timezone="America/New_York"),
            ),
            patch(
                "clientplatform.application.native_member_interactions.save_business_profile",
                return_value=SimpleNamespace(
                    activity_description="Консультирую международных клиентов"
                ),
            ) as save_profile,
        ):
            _, replies = handle_clientplatform_entry(
                808,
                platform="vk",
                external_user_id="vk-808",
                text="деятельность Консультирую международных клиентов",
                event_key="vk-activity-1",
            )

        self.assertEqual(
            save_profile.call_args.kwargs["timezone_name"],
            "America/New_York",
        )
        restored = CustomerInteractionMessage.from_json(replies[0].meta["interaction"])
        self.assertIn("Описание деятельности обновлено", restored.text)

    def test_activity_update_for_manager_returns_permission_reply_without_write(self) -> None:
        entry = SimpleNamespace(user_id=909)
        access = SimpleNamespace(
            business=SimpleNamespace(id=B1, name="Практика с командой")
        )
        actor = TenantContext(
            business_id=B1,
            user_id=909,
            membership_id="99999999-9999-9999-9999-999999999999",
            role=PlatformRole.MANAGER,
        )
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=[access],
            ),
            patch(
                "services.messenger.clientplatform_entry.resolve_tenant_context",
                return_value=actor,
            ),
            patch(
                "clientplatform.application.native_member_interactions.resolve_tenant_context",
                return_value=actor,
            ),
            patch(
                "services.messenger.clientplatform_entry.save_business_profile"
            ) as direct_save,
            patch(
                "clientplatform.application.native_member_interactions.save_business_profile"
            ) as native_save,
        ):
            _, replies = handle_clientplatform_entry(
                909,
                platform="vk",
                external_user_id="vk-909",
                text="деятельность Пытаюсь изменить профиль",
                event_key="vk-manager-activity",
            )

        direct_save.assert_not_called()
        native_save.assert_not_called()
        self.assertEqual(len(replies), 1)
        restored = CustomerInteractionMessage.from_json(replies[0].meta["interaction"])
        self.assertEqual(restored.text, "Для Вашей роли этот раздел недоступен.")

    async def test_multi_business_selector_is_delivered_without_preselected_tenant(self) -> None:
        sent: list[tuple[str, str, dict[str, object]]] = []

        class FakeSender:
            async def send_text(self, external_user_id, text, **kwargs):
                sent.append((str(external_user_id), str(text), dict(kwargs)))
                return {"ok": True}

        interaction = CustomerInteractionMessage(
            text="Выберите бизнес",
            rows=((CustomerInteractionButton(
                label="Практика Анны",
                command=f"cpw:open:{uuid_token(B1)}",
            ),),),
        )
        reply = MessengerReply(
            kind="clientplatform_interaction",
            text=interaction.text,
            meta={"interaction": interaction.to_json()},
        )
        with (
            patch.object(reply_dispatcher, "VkBotSender", return_value=FakeSender()),
            patch.object(reply_dispatcher, "MaxBotSender", return_value=FakeSender()),
        ):
            await reply_dispatcher.send_reply_bundle("vk", "vk-505", 505, [reply])

        keyboard = json.loads(str(sent[0][2]["keyboard_json"]))
        payload = json.loads(keyboard["buttons"][0][0]["action"]["payload"])
        self.assertEqual(payload["command"], f"cpw:open:{uuid_token(B1)}")

    async def test_global_vk_owner_interaction_renders_real_inline_buttons(self) -> None:
        sent: list[tuple[str, str, dict[str, object]]] = []

        class FakeSender:
            async def send_text(self, external_user_id, text, **kwargs):
                sent.append((str(external_user_id), str(text), dict(kwargs)))
                return {"ok": True}

        interaction = CustomerInteractionMessage(
            text="🏠 Практика Анны",
            rows=((CustomerInteractionButton(label="💬 Мессенджеры", command="cpm:messengers"),),),
        )
        reply = MessengerReply(
            kind="clientplatform_interaction",
            text=interaction.text,
            meta={
                "interaction": interaction.to_json(),
                "business_id": B1,
            },
        )
        with (
            patch.object(reply_dispatcher, "VkBotSender", return_value=FakeSender()),
            patch.object(reply_dispatcher, "MaxBotSender", return_value=FakeSender()),
        ):
            await reply_dispatcher.send_reply_bundle("vk", "vk-101", 101, [reply])

        self.assertEqual(len(sent), 1)
        keyboard = json.loads(str(sent[0][2]["keyboard_json"]))
        payload = json.loads(keyboard["buttons"][0][0]["action"]["payload"])
        self.assertEqual(payload, {"command": f"cpw:act:{uuid_token(B1)}:cpm:messengers"})
        self.assertNotIn("cpm:messengers", sent[0][1])

    async def test_global_max_owner_interaction_renders_real_callback_buttons(self) -> None:
        sent: list[tuple[str, str, dict[str, object]]] = []

        class FakeSender:
            async def send_text(self, external_user_id, text, **kwargs):
                sent.append((str(external_user_id), str(text), dict(kwargs)))
                return {"ok": True}

        interaction = CustomerInteractionMessage(
            text="🏠 Автосервис Север",
            rows=((CustomerInteractionButton(label="📊 Работа", command="cpm:work"),),),
        )
        reply = MessengerReply(
            kind="clientplatform_interaction",
            text=interaction.text,
            meta={
                "interaction": interaction.to_json(),
                "business_id": B2,
            },
        )
        with (
            patch.object(reply_dispatcher, "VkBotSender", return_value=FakeSender()),
            patch.object(reply_dispatcher, "MaxBotSender", return_value=FakeSender()),
        ):
            await reply_dispatcher.send_reply_bundle("max", "max-303", 303, [reply])

        attachments = sent[0][2]["attachments"]
        button = attachments[0]["payload"]["buttons"][0][0]
        self.assertEqual(button["type"], "callback")
        self.assertEqual(button["payload"], f"cpw:act:{uuid_token(B2)}:cpm:work")

    async def test_setup_link_resolution_fails_closed(self) -> None:
        class FakeSender:
            async def send_text(self, external_user_id, text, **kwargs):
                raise AssertionError("provider must not be called with unresolved setup link")

        interaction = CustomerInteractionMessage(
            text="Подключение MAX",
            rows=((CustomerInteractionButton(
                label="🔐 Открыть защищённую настройку",
                command="cpm:setup:123e4567-e89b-12d3-a456-426614174000",
            ),),),
        )
        reply = MessengerReply(
            kind="clientplatform_interaction",
            text=interaction.text,
            meta={
                "interaction": interaction.to_json(),
                "business_id": "business-303",
            },
        )
        with (
            patch.object(reply_dispatcher, "VkBotSender", return_value=FakeSender()),
            patch.object(reply_dispatcher, "MaxBotSender", return_value=FakeSender()),
            patch.object(
                reply_dispatcher,
                "_clientplatform_runtime_button_links",
                side_effect=ValueError("expired"),
            ),
        ):
            with self.assertRaises(reply_dispatcher.MessengerTransportError):
                await reply_dispatcher.send_reply_bundle(
                    "max", "max-303", 303, [reply]
                )

    async def test_setup_link_is_materialized_only_at_delivery_boundary(self) -> None:
        sent: list[tuple[str, str, dict[str, object]]] = []

        class FakeSender:
            async def send_text(self, external_user_id, text, **kwargs):
                sent.append((str(external_user_id), str(text), dict(kwargs)))
                return {"ok": True}

        interaction = CustomerInteractionMessage(
            text="Подключение ВКонтакте",
            rows=((CustomerInteractionButton(
                label="🔐 Открыть защищённую настройку",
                command="cpm:setup:123e4567-e89b-12d3-a456-426614174000",
            ),),),
        )
        reply = MessengerReply(
            kind="clientplatform_interaction",
            text=interaction.text,
            meta={
                "interaction": interaction.to_json(),
                "business_id": "business-303",
            },
        )
        durable = json.dumps(reply.meta, ensure_ascii=False)
        self.assertIn("cpm:setup:", durable)
        self.assertNotIn("https://", durable)
        with (
            patch.object(reply_dispatcher, "VkBotSender", return_value=FakeSender()),
            patch.object(reply_dispatcher, "MaxBotSender", return_value=FakeSender()),
            patch.object(
                reply_dispatcher,
                "_clientplatform_runtime_button_links",
                return_value={
                    "cpm:setup:123e4567-e89b-12d3-a456-426614174000":
                    "https://clientplatform.example/clientplatform/connect/opaque"
                },
            ),
        ):
            await reply_dispatcher.send_reply_bundle("vk", "vk-303", 303, [reply])

        keyboard = json.loads(str(sent[0][2]["keyboard_json"]))
        action = keyboard["buttons"][0][0]["action"]
        self.assertEqual(action["type"], "open_link")
        self.assertEqual(
            action["link"],
            "https://clientplatform.example/clientplatform/connect/opaque",
        )

    async def test_vk_webhook_start_reaches_clientplatform_entry(self) -> None:
        payload = {
            "type": "message_new",
            "event_id": "vk-start-1",
            "object": {
                "message": {
                    "id": 1,
                    "from_id": 501,
                    "text": "/start",
                }
            },
        }
        with (
            patch.object(reliability.legacy, "_vk_secret_ok", return_value=True),
            patch.object(
                reliability,
                "_process_clientplatform_entry_and_persist",
                return_value=True,
            ) as process,
        ):
            response = await reliability.vk_webhook(_FakeRequest(payload))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.text, "ok")
        process.assert_called_once()
        kwargs = process.call_args.kwargs
        self.assertEqual(kwargs["platform"], "vk")
        self.assertEqual(kwargs["event_type"], "message_new")
        self.assertEqual(kwargs["text"], "start")
        self.assertEqual(kwargs["extracted"]["external_user_id"], "501")

    async def test_vk_owner_ref_reaches_clientplatform_entry(self) -> None:
        payload = {
            "type": "message_new",
            "event_id": "vk-owner-ref-1",
            "object": {
                "message": {
                    "id": 2,
                    "from_id": 502,
                    "text": "start",
                    "ref": "cpo_landing",
                }
            },
        }
        with (
            patch.object(reliability.legacy, "_vk_secret_ok", return_value=True),
            patch.object(
                reliability,
                "_process_clientplatform_entry_and_persist",
                return_value=True,
            ) as process,
        ):
            response = await reliability.vk_webhook(_FakeRequest(payload))
        self.assertEqual(response.status, 200)
        process.assert_called_once()
        kwargs = process.call_args.kwargs
        self.assertEqual(kwargs["text"], "/start cpo_landing")
        self.assertEqual(kwargs["extracted"]["external_user_id"], "502")

    async def test_global_max_owner_callback_is_acknowledged_before_processing(self) -> None:
        payload = {
            "update_type": "message_callback",
            "timestamp": 1787259600000,
            "user": {"user_id": 601, "first_name": "Анна"},
            "callback": {
                "callback_id": "owner-callback-1",
                "payload": "cpm:work",
            },
        }
        acknowledged: list[str] = []

        class FakeMaxSender:
            async def answer_callback(self, *, callback_id: str):
                acknowledged.append(callback_id)
                return {"success": True}

        with (
            patch.object(reliability.legacy, "_max_secret_ok", return_value=True),
            patch.object(reliability, "MaxBotSender", return_value=FakeMaxSender()),
            patch.object(
                reliability,
                "_process_clientplatform_entry_and_persist",
                return_value=True,
            ) as process,
        ):
            response = await reliability.max_webhook(_FakeRequest(payload))

        self.assertEqual(response.status, 200)
        self.assertEqual(acknowledged, ["owner-callback-1"])
        process.assert_called_once()
        self.assertEqual(process.call_args.kwargs["text"], "cpm:work")

    async def test_max_native_bot_started_reaches_entry_without_text(self) -> None:
        payload = {
            "update_type": "bot_started",
            "update_id": 77,
            "user_id": 601,
        }
        with (
            patch.object(reliability.legacy, "_max_secret_ok", return_value=True),
            patch.object(
                reliability,
                "_process_clientplatform_entry_and_persist",
                return_value=True,
            ) as process,
        ):
            response = await reliability.max_webhook(_FakeRequest(payload))
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body), {"ok": True})
        process.assert_called_once()
        kwargs = process.call_args.kwargs
        self.assertEqual(kwargs["platform"], "max")
        self.assertEqual(kwargs["event_type"], "bot_started")
        self.assertEqual(kwargs["text"], "start")
        self.assertEqual(kwargs["extracted"]["external_user_id"], "601")

    async def test_max_owner_deep_link_payload_reaches_clientplatform_entry(self) -> None:
        payload = {
            "update_type": "bot_started",
            "update_id": 78,
            "user_id": 602,
            "payload": "cpo_landing",
        }
        with (
            patch.object(reliability.legacy, "_max_secret_ok", return_value=True),
            patch.object(
                reliability,
                "_process_clientplatform_entry_and_persist",
                return_value=True,
            ) as process,
        ):
            response = await reliability.max_webhook(_FakeRequest(payload))
        self.assertEqual(response.status, 200)
        process.assert_called_once()
        kwargs = process.call_args.kwargs
        self.assertEqual(kwargs["text"], "cpo_landing")
        self.assertEqual(kwargs["extracted"]["external_user_id"], "602")

    def test_owner_mutation_and_outbox_are_atomic_across_retry(self) -> None:
        # This module is also executed by the PostgreSQL bot-provisioning unittest
        # wall, which intentionally does not run pytest's canonical schema fixture.
        # Bootstrap the dedicated test database explicitly so this real transaction
        # regression is engine-neutral instead of depending on test-runner order.
        init_db()
        owner_user_id = 9401
        member_user_id = 9402
        access = create_business(owner_user_id=owner_user_id, name="Atomic Owner Business")
        business_id = str(access.business.id)
        owner = resolve_real_tenant_context(
            user_id=owner_user_id,
            business_id=business_id,
        )
        grant_business_member(
            actor=owner,
            user_id=member_user_id,
            role=PlatformRole.MANAGER,
        )
        extracted = {
            "user_id": owner_user_id,
            "external_user_id": str(owner_user_id),
            "username": None,
            "display_name": "Owner",
            "first_name": "Owner",
        }
        payload = {"event_id": "vk-owner-revoke-atomic"}
        command = f"cpm:member-revoke:{member_user_id}"

        with (
            patch.object(reliability, "claim_inbound_event", return_value=True),
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=SimpleNamespace(user_id=owner_user_id),
            ),
            patch.object(reliability, "fail_inbound_event") as fail_event,
            patch.object(
                reliability,
                "log_event",
                side_effect=RuntimeError("failure after durable reply"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "failure after durable reply"):
                reliability._process_clientplatform_entry_and_persist(
                    platform="vk",
                    event_key="vk-owner-revoke-atomic",
                    event_type="message_event",
                    payload=payload,
                    extracted=extracted,
                    text=command,
                )

        restored_member = resolve_real_tenant_context(
            user_id=member_user_id,
            business_id=business_id,
        )
        self.assertEqual(restored_member.role, PlatformRole.MANAGER)
        with get_db_ro() as conn:
            rolled_back_outbox = conn.execute(
                "SELECT COUNT(*) FROM messenger_delivery_outbox WHERE platform=? AND event_key=?",
                ("vk", "vk-owner-revoke-atomic"),
            ).fetchone()[0]
        self.assertEqual(rolled_back_outbox, 0)
        fail_event.assert_called_once()

        with (
            patch.object(reliability, "claim_inbound_event", return_value=True),
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=SimpleNamespace(user_id=owner_user_id),
            ),
            patch.object(reliability, "log_event"),
        ):
            processed = reliability._process_clientplatform_entry_and_persist(
                platform="vk",
                event_key="vk-owner-revoke-atomic",
                event_type="message_event",
                payload=payload,
                extracted=extracted,
                text=command,
            )

        self.assertTrue(processed)
        with self.assertRaises(TenantAccessDenied):
            resolve_real_tenant_context(
                user_id=member_user_id,
                business_id=business_id,
            )
        with get_db_ro() as conn:
            committed_outbox = conn.execute(
                "SELECT COUNT(*) FROM messenger_delivery_outbox WHERE platform=? AND event_key=?",
                ("vk", "vk-owner-revoke-atomic"),
            ).fetchone()[0]
        self.assertEqual(committed_outbox, 1)

    def test_webhook_entry_is_deduplicated_before_side_effects(self) -> None:
        extracted = {
            "user_id": 404,
            "external_user_id": "404",
            "username": None,
            "display_name": None,
            "first_name": None,
        }
        payload = {"event_id": "vk-entry-1"}
        with (
            patch.object(reliability, "claim_inbound_event", return_value=True),
            patch.object(
                reliability,
                "handle_clientplatform_entry",
                return_value=(404, [MessengerReply(text="ClientPlatform")]),
            ) as handle,
            patch.object(reliability, "persist_reply_bundle", return_value=True) as persist,
            patch.object(reliability, "log_event"),
        ):
            processed = reliability._process_clientplatform_entry_and_persist(
                platform="vk",
                event_key="vk-entry-1",
                event_type="message_new",
                payload=payload,
                extracted=extracted,
                text="/start",
            )
        self.assertTrue(processed)
        handle.assert_called_once()
        self.assertEqual(handle.call_args.kwargs["event_key"], "vk-entry-1")
        persist.assert_called_once()

        with (
            patch.object(reliability, "claim_inbound_event", return_value=False),
            patch.object(reliability, "handle_clientplatform_entry") as duplicate_handle,
        ):
            processed = reliability._process_clientplatform_entry_and_persist(
                platform="vk",
                event_key="vk-entry-1",
                event_type="message_new",
                payload=payload,
                extracted=extracted,
                text="/start",
            )
        self.assertFalse(processed)
        duplicate_handle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
