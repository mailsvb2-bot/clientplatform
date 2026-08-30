from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from runtime import messenger_ingress_reliability as reliability
from services.messenger import reply_dispatcher
from services.messenger.clientplatform_entry import (
    handle_clientplatform_entry,
    parse_clientplatform_entry_command,
)
from services.messenger.text_ui import MessengerReply


class _FakeRequest:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload, ensure_ascii=False)
        self.headers: dict[str, str] = {}

    async def text(self) -> str:
        return self._body


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

    def test_max_activity_step_saves_profile_and_opens_native_dashboard(self) -> None:
        entry = SimpleNamespace(user_id=707)
        access = SimpleNamespace(
            business=SimpleNamespace(id="business-707", name="Школа английского")
        )
        actor = SimpleNamespace(user_id=707, business_id="business-707")
        interaction = CustomerInteractionMessage(
            text="🏠 Школа английского",
            rows=((CustomerInteractionButton(label="⋯ Все возможности", command="cpm:menu-all"),),),
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
                "services.messenger.clientplatform_entry.save_business_profile"
            ) as save_profile,
            patch(
                "services.messenger.clientplatform_entry.render_native_member_interaction",
                return_value=interaction,
            ),
        ):
            _, replies = handle_clientplatform_entry(
                707,
                platform="max",
                external_user_id="max-707",
                text="деятельность Провожу уроки английского онлайн",
            )
        save_profile.assert_called_once()
        self.assertEqual(
            save_profile.call_args.kwargs["activity_description"],
            "Провожу уроки английского онлайн",
        )
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0].kind, "text")
        self.assertIn("Описание сохранено", replies[0].text)
        self.assertEqual(replies[1].kind, "clientplatform_interaction")
        restored = CustomerInteractionMessage.from_json(replies[1].meta["interaction"])
        self.assertIn("Школа английского", restored.text)
        self.assertEqual(restored.rows[0][0].command, "cpm:menu-all")

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
                "business_id": "business-101",
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
        self.assertEqual(payload, {"command": "cpm:messengers"})
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
                "business_id": "business-303",
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
        self.assertEqual(button["payload"], "cpm:work")

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
