from __future__ import annotations

import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from clientplatform.application import native_event_wizard as wizard
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.event_content import EventContentMode, EventContentStage
from clientplatform.domain.tenancy import PlatformRole, TenantContext


BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
EVENT_ID = "22222222-2222-4222-8222-222222222222"


class NativeEventWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actor = TenantContext(
            business_id=BUSINESS_ID,
            user_id=101,
            membership_id="33333333-3333-4333-8333-333333333333",
            role=PlatformRole.OWNER,
        )
        self.platform = ConnectionPlatform.VK
        self.surface = "official"
        self.store: dict[str, str] = {}

    def _save(self, _actor, *, platform, surface, context) -> None:
        self.assertEqual(platform, self.platform)
        self.assertEqual(surface, self.surface)
        self.store.clear()
        self.store.update({key: str(value) for key, value in context.items()})

    def _context(self, _actor, *, platform, surface) -> dict[str, str]:
        self.assertEqual(platform, self.platform)
        self.assertEqual(surface, self.surface)
        return dict(self.store)

    @staticmethod
    def _commands(message) -> list[str]:
        return [button.command for row in message.rows for button in row]

    def _choose_window(self, selected: date):
        wizard.handle_native_event_wizard_action(
            self.actor,
            args=("date", selected.isoformat()),
            platform=self.platform,
            surface=self.surface,
        )
        wizard.handle_native_event_wizard_action(
            self.actor,
            args=("start", "1900"),
            platform=self.platform,
            surface=self.surface,
        )
        return wizard.handle_native_event_wizard_action(
            self.actor,
            args=("duration", "120"),
            platform=self.platform,
            surface=self.surface,
        )

    def test_three_day_flow_uses_buttons_and_finishes_with_canonical_actions(self) -> None:
        created = SimpleNamespace(event_id=EVENT_ID)
        appended = (SimpleNamespace(event_id=EVENT_ID),)
        published = SimpleNamespace(
            registration_url=lambda base: f"{base}/e/demo",
        )
        window = SimpleNamespace(days_until_event=5, max_warmup_days=5)
        warmup = SimpleNamespace(requested_days=2, drafts=())
        content_modes = MagicMock()
        with (
            patch.object(wizard, "_save", side_effect=self._save),
            patch.object(wizard, "_context", side_effect=self._context),
            patch.object(wizard, "create_multisession_online_event_draft", return_value=created) as create,
            patch.object(
                wizard,
                "append_multisession_online_event_draft_session",
                return_value=appended,
            ) as append,
            patch.object(wizard, "publish_multisession_online_event_draft", return_value=published) as publish,
            patch.object(wizard, "get_event_warmup_window", return_value=window),
            patch.object(wizard, "save_event_warmup_plan", return_value=warmup),
            patch.object(wizard, "list_event_sessions", return_value=()),
            patch.object(wizard, "set_event_content_mode", content_modes),
            patch.object(wizard, "clear_owner_input") as clear,
            patch.object(
                wizard.settings,
                "MESSENGER_PUBLIC_BASE_URL",
                "https://clientplatform.example.test",
                create=True,
            ),
        ):
            first = wizard.begin_native_event_wizard(
                self.actor,
                platform=self.platform,
                surface=self.surface,
            )
            self.assertIn("Как называется", first.text)
            self.assertEqual(self.store["step"], "title")

            count = wizard.handle_native_event_wizard_text(
                self.actor,
                action="event-wizard-title-text",
                args=("Большой интенсив",),
                platform=self.platform,
                surface=self.surface,
            )
            self.assertIn("Сколько дней", count.text)
            wizard.handle_native_event_wizard_action(
                self.actor,
                args=("count", "3"),
                platform=self.platform,
                surface=self.surface,
            )
            wizard.handle_native_event_wizard_action(
                self.actor,
                args=("timezone", "moscow"),
                platform=self.platform,
                surface=self.surface,
            )
            venue = wizard.handle_native_event_wizard_action(
                self.actor,
                args=("venue", "telemost"),
                platform=self.platform,
                surface=self.surface,
            )
            self.assertIn("выберите дату", venue.text)

            selected = date.fromisoformat(self.store["min_date"])
            room = self._choose_window(selected)
            self.assertIn("cpm:event-venue-open:telemost", self._commands(room))
            wizard.handle_native_event_wizard_text(
                self.actor,
                action="event-wizard-room-text",
                args=("https://example.test/day-1",),
                platform=self.platform,
                surface=self.surface,
            )
            self.assertEqual(self.store["position"], "2")
            self.assertEqual(self.store["event_id"], EVENT_ID)

            second_date = date.fromisoformat(self.store["min_date"]) + timedelta(days=1)
            self._choose_window(second_date)
            wizard.handle_native_event_wizard_text(
                self.actor,
                action="event-wizard-room-text",
                args=("https://example.test/day-2",),
                platform=self.platform,
                surface=self.surface,
            )
            self.assertEqual(self.store["position"], "3")

            third_date = date.fromisoformat(self.store["min_date"]) + timedelta(days=1)
            self._choose_window(third_date)
            after_publish = wizard.handle_native_event_wizard_text(
                self.actor,
                action="event-wizard-room-text",
                args=("https://example.test/day-3",),
                platform=self.platform,
                surface=self.surface,
            )
            self.assertIn("Вебинар создан", after_publish.text)
            self.assertEqual(self.store["step"], "warmup_days")
            self.assertEqual(self.store["published"], "1")
            create.assert_called_once()
            self.assertEqual(append.call_count, 2)
            publish.assert_called_once_with(actor=self.actor, event_id=EVENT_ID)

            mode = wizard.handle_native_event_wizard_text(
                self.actor,
                action="event-wizard-warmup-text",
                args=("2",),
                platform=self.platform,
                surface=self.surface,
            )
            self.assertIn("Как оформить прогрев", mode.text)
            wizard.handle_native_event_wizard_action(
                self.actor,
                args=("mode", EventContentStage.WARMUP.value, EventContentMode.TEXT_WITH_IMAGE.value),
                platform=self.platform,
                surface=self.surface,
            )
            wizard.handle_native_event_wizard_action(
                self.actor,
                args=("mode", EventContentStage.EVENT_DAY.value, EventContentMode.TEXT.value),
                platform=self.platform,
                surface=self.surface,
            )
            final = wizard.handle_native_event_wizard_action(
                self.actor,
                args=("mode", EventContentStage.POST_EVENT.value, EventContentMode.TEXT_IN_IMAGE.value),
                platform=self.platform,
                surface=self.surface,
            )
            self.assertIn("https://clientplatform.example.test/e/demo", final.text)
            commands = self._commands(final)
            self.assertTrue(any(command.startswith("cpm:event-wizard:wp:") for command in commands))
            self.assertIn(f"cpm:event-announce:{EVENT_ID}", commands)
            clear.assert_called_once()
            self.assertEqual(content_modes.call_count, 3)

    def test_date_picker_stays_inside_native_provider_button_limit(self) -> None:
        self.store.update(
            {
                "timezone": "Europe/Moscow",
                "count": "3",
                "position": "1",
                "min_date": "2026-09-20",
            }
        )
        with patch.object(wizard, "_context", side_effect=self._context):
            message = wizard.handle_native_event_wizard_action(
                self.actor,
                args=("date-page", "0"),
                platform=self.platform,
                surface=self.surface,
            )
        self.assertLessEqual(sum(len(row) for row in message.rows), 10)
        self.assertTrue(any(command.startswith("cpm:event-wizard:date:") for command in self._commands(message)))


    def test_date_picker_last_page_clamps_to_supported_range(self) -> None:
        self.store.update(
            {
                "timezone": "Europe/Moscow",
                "count": "3",
                "position": "1",
                "min_date": "2026-09-20",
            }
        )
        with patch.object(wizard, "_context", side_effect=self._context):
            message = wizard.handle_native_event_wizard_action(
                self.actor,
                args=("date-page", str(wizard._MAX_DATE_DAYS - 4)),
                platform=self.platform,
                surface=self.surface,
            )
        date_commands = [
            command
            for command in self._commands(message)
            if command.startswith("cpm:event-wizard:date:")
        ]
        self.assertEqual(len(date_commands), 5)
        self.assertFalse(
            any(
                command.startswith("cpm:event-wizard:date-page:")
                and command.endswith(str(wizard._MAX_DATE_DAYS + 3))
                for command in self._commands(message)
            )
        )

    def test_ucr_is_explicitly_unavailable_until_public_room_link_exists(self) -> None:
        self.store.update(
            {
                "step": "venue",
                "timezone": "Europe/Moscow",
                "count": "1",
                "position": "1",
                "title": "Вебинар",
            }
        )
        with (
            patch.object(wizard, "_context", side_effect=self._context),
            patch.object(wizard, "_save", side_effect=self._save),
        ):
            message = wizard.handle_native_event_wizard_action(
                self.actor,
                args=("venue", "ucr"),
                platform=self.platform,
                surface=self.surface,
            )
        self.assertIn("не выдаёт", message.text)
        self.assertEqual(self.store["step"], "venue")


def test_native_warmup_preview_edit_reset_and_post_creation_setup() -> None:
    actor = TenantContext(
        business_id=BUSINESS_ID,
        user_id=101,
        membership_id="33333333-3333-4333-8333-333333333333",
        role=PlatformRole.OWNER,
    )
    plan = SimpleNamespace(
        requested_days=2,
        drafts=(
            SimpleNamespace(
                position=1,
                publish_date=date(2026, 9, 20),
                text="Мой прогрев",
                source="owner",
            ),
            SimpleNamespace(
                position=2,
                publish_date=date(2026, 9, 21),
                text="Автотекст",
                source="template",
            ),
        ),
    )
    with patch.object(wizard, "get_saved_event_warmup_plan", return_value=plan):
        preview = wizard._warmup_preview(
            actor,
            event_id=EVENT_ID,
            requested_days=2,
            page=0,
        )
    commands = NativeEventWizardTests._commands(preview)
    assert any(":we:" in command for command in commands)
    assert any(":wr:" in command for command in commands)
    assert "Источник: Ваш текст" in preview.text

    with (
        patch.object(
            wizard,
            "get_event_warmup_window",
            return_value=SimpleNamespace(max_warmup_days=6),
        ),
        patch.object(wizard, "begin_owner_input") as begin,
    ):
        setup = wizard.handle_native_event_wizard_action(
            actor,
            args=("ws", EVENT_ID),
            platform=ConnectionPlatform.MAX,
            surface="official",
        )
        assert any(":wset:" in command for command in NativeEventWizardTests._commands(setup))
        custom = wizard.handle_native_event_wizard_action(
            actor,
            args=("wc", EVENT_ID, "6"),
            platform=ConnectionPlatform.MAX,
            surface="official",
        )
        assert "число дней" in custom.text
        assert begin.call_args.kwargs["action"] == "event_warmup_days"

    with (
        patch.object(wizard, "set_event_warmup_text") as save_text,
        patch.object(wizard, "_warmup_preview", return_value=SimpleNamespace(text="saved", rows=())) as render,
    ):
        saved = wizard.handle_native_event_wizard_text(
            actor,
            action="event-we-text",
            args=(EVENT_ID, "2", "1", "Полностью свой текст"),
            platform=ConnectionPlatform.VK,
            surface="official",
        )
    assert saved.text == "saved"
    save_text.assert_called_once_with(
        actor=actor,
        event_id=EVENT_ID,
        position=1,
        text="Полностью свой текст",
    )
    render.assert_called_once()



def test_native_webinar_button_commands_fit_compact_transport_boundary() -> None:
    event_id = "22222222-2222-4222-8222-222222222222"
    commands = (
        f"cpm:event-wizard:wp:{event_id}:14:10",
        f"cpm:event-wizard:we:{event_id}:14:10",
        f"cpm:event-wizard:wr:{event_id}:14:10",
        f"cpm:event-wizard:ws:{event_id}",
        f"cpm:event-wizard:wset:{event_id}:14",
        f"cpm:event-wizard:wc:{event_id}:366",
        f"cpm:event-content:{event_id}",
        f"cpm:event-content-followups:{event_id}",
    )
    for command in commands:
        assert len(command.encode("utf-8")) <= 64, command


if __name__ == "__main__":
    unittest.main()
