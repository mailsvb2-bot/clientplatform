from __future__ import annotations

import ast
import unittest
from pathlib import Path

from clientplatform.application.control_callbacks import token_uuid, uuid_token


class ClientPlatformControlBotContractTests(unittest.TestCase):
    def test_uuid_callback_encoding_round_trips_and_fits_telegram_limit(self) -> None:
        value = "12345678-1234-5678-1234-567812345678"
        token = uuid_token(value)
        self.assertEqual(token_uuid(token), value)
        callbacks = (
            f"cp:sendp:{token}:{token}",
            f"cp:slotadd:{token}:{token}",
            f"cp:cprog:{token}:{token}",
            f"cp:done:{token}:{token}:999",
        )
        for callback in callbacks:
            self.assertLessEqual(len(callback.encode("utf-8")), 64, callback)

    def test_material_handler_accepts_telegram_media_and_text(self) -> None:
        handler = Path("handlers/clientplatform_control.py").read_text(encoding="utf-8")
        attributes = (
            "message.audio",
            "message.voice",
            "message.video",
            "message.document",
            "message.photo",
            "message.text",
        )
        for attribute in attributes:
            self.assertIn(attribute, handler)
        self.assertIn("ContentKind.AUDIO", handler)
        self.assertIn("ContentKind.TEXT", handler)

    def test_control_router_precedes_legacy_start_and_has_explicit_emergency_gate(self) -> None:
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertLess(
            source.index("dp.include_router(clientplatform_control.router)"),
            source.index("dp.include_router(start.router)"),
        )
        handler = Path("handlers/clientplatform_control.py").read_text(encoding="utf-8")
        tree = ast.parse(handler)
        runtime = Path("clientplatform/runtime/control_bot.py").read_text(encoding="utf-8")
        self.assertIn("CLIENTPLATFORM_CONTROL_BOT_ENABLED", runtime)
        self.assertIn("return True", runtime)
        self.assertIn("_FALSE_VALUES", runtime)
        self.assertTrue(
            any(
                isinstance(node, ast.ClassDef)
                and node.name == "ClientPlatformControlEnabled"
                for node in tree.body
            )
        )
        self.assertIn("чем Вы занимаетесь", handler)
        self.assertIn("connector_key", handler)
        self.assertIn("cp:slotadd:", handler)
        self.assertIn("cp:book:", handler)
        self.assertIn("list_customer_businesses", handler)

    def test_customer_program_progress_is_wired_into_both_portals(self) -> None:
        handler = Path("handlers/clientplatform_control.py").read_text(encoding="utf-8")
        for callback_prefix in ("cp:cprograms:", "cp:cprog:", "cp:done:"):
            self.assertIn(callback_prefix, handler)
        self.assertIn("Мои программы", handler)
        self.assertIn("complete_customer_lesson", handler)
        self.assertIn("list_business_program_progress", handler)
        self.assertIn("Прогресс клиентов", handler)

    def test_production_templates_enable_clientplatform_with_opt_out(self) -> None:
        env_example = Path("deploy/metrotherapy.env.example").read_text(encoding="utf-8")
        service = Path("deploy/metrotherapy.service").read_text(encoding="utf-8")
        for source in (env_example, service):
            self.assertIn("CLIENTPLATFORM_CONTROL_BOT_ENABLED=1", source)
            self.assertIn("CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED=1", source)
        self.assertIn("emergency rollback", env_example)

        control_default = service.index("Environment=CLIENTPLATFORM_CONTROL_BOT_ENABLED=1")
        dispatch_default = service.index("Environment=CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED=1")
        first_override_file = service.index("EnvironmentFile=-/etc/metrotherapy/metrotherapy.env")
        second_override_file = service.index("EnvironmentFile=-/etc/default/metrotherapy")
        self.assertLess(control_default, first_override_file)
        self.assertLess(dispatch_default, first_override_file)
        self.assertLess(first_override_file, second_override_file)

    def test_legacy_start_handler_is_unchanged_and_still_registered(self) -> None:
        source = Path("handlers/start.py").read_text(encoding="utf-8")
        self.assertIn("@router.message(CommandStart())", source)
        self.assertIn("Добро пожаловать в Метротерапию", source)


if __name__ == "__main__":
    unittest.main()
