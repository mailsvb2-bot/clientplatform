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
        callback = f"cp:sendp:{token}:{token}"
        self.assertLessEqual(len(callback.encode("utf-8")), 64)

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

    def test_control_router_precedes_legacy_start_and_remains_feature_gated(self) -> None:
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertLess(
            source.index("dp.include_router(clientplatform_control.router)"),
            source.index("dp.include_router(start.router)"),
        )
        handler = Path("handlers/clientplatform_control.py").read_text(encoding="utf-8")
        tree = ast.parse(handler)
        self.assertIn("CLIENTPLATFORM_CONTROL_BOT_ENABLED", Path("clientplatform/runtime/control_bot.py").read_text())
        self.assertTrue(
            any(
                isinstance(node, ast.ClassDef)
                and node.name == "ClientPlatformControlEnabled"
                for node in tree.body
            )
        )
        self.assertIn("чем Вы занимаетесь", handler)
        self.assertIn("connector_key", handler)

    def test_legacy_start_handler_is_unchanged_and_still_registered(self) -> None:
        source = Path("handlers/start.py").read_text(encoding="utf-8")
        self.assertIn("@router.message(CommandStart())", source)
        self.assertIn("Добро пожаловать в Метротерапию", source)


if __name__ == "__main__":
    unittest.main()
