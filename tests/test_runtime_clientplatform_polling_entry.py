from __future__ import annotations

import importlib
import os
import unittest
from unittest.mock import patch

from handlers.clientplatform_entry import register_clientplatform_bot_commands
from runtime.telegram_transport import telegram_transport, telegram_webhook_requested


class _FakeCommandBot:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.commands = []

    async def set_my_commands(self, commands):
        self.commands = list(commands)
        return self.result


class ClientPlatformPollingEntryTests(unittest.IsolatedAsyncioTestCase):
    def test_webhook_environment_cannot_change_telegram_transport(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TRANSPORT": "webhook",
                "TELEGRAM_WEBHOOK_ENABLED": "1",
                "MESSENGER_WEBHOOK_ENABLED": "1",
            },
            clear=False,
        ):
            self.assertTrue(telegram_webhook_requested())
            self.assertEqual(telegram_transport(), "polling")

    def test_main_normalization_changes_only_telegram_transport_flags(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TRANSPORT": "webhook",
                "TELEGRAM_WEBHOOK_ENABLED": "1",
                "MESSENGER_WEBHOOK_ENABLED": "1",
                "VK_WEBHOOK_ENABLED": "1",
                "MAX_WEBHOOK_ENABLED": "1",
            },
            clear=False,
        ):
            main = importlib.import_module("main")
            main = importlib.reload(main)
            self.assertTrue(main._TELEGRAM_WEBHOOK_OVERRIDE_IGNORED)
            self.assertEqual(os.environ["TELEGRAM_TRANSPORT"], "polling")
            self.assertEqual(os.environ["RUN_MODE"], "polling")
            self.assertEqual(os.environ["TELEGRAM_WEBHOOK_ENABLED"], "0")
            self.assertEqual(os.environ["MESSENGER_WEBHOOK_ENABLED"], "1")
            self.assertEqual(os.environ["VK_WEBHOOK_ENABLED"], "1")
            self.assertEqual(os.environ["MAX_WEBHOOK_ENABLED"], "1")

    async def test_start_command_is_registered_in_telegram_menu(self) -> None:
        bot = _FakeCommandBot()
        self.assertTrue(await register_clientplatform_bot_commands(bot))
        commands = {command.command: command.description for command in bot.commands}
        self.assertEqual(commands["start"], "Открыть ClientPlatform")
        self.assertIn("mybot", commands)

    def test_lazy_router_registers_command_startup_once(self) -> None:
        import handlers

        entry, _ = handlers._load_clientplatform_modules()
        entry_again, _ = handlers._load_clientplatform_modules()
        self.assertIs(entry, entry_again)
        self.assertTrue(entry._telegram_commands_startup_composed)
        callbacks = [getattr(item, "callback", None) for item in entry.router.startup.handlers]
        self.assertEqual(callbacks.count(register_clientplatform_bot_commands), 1)


if __name__ == "__main__":
    unittest.main()
