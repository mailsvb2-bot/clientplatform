from __future__ import annotations

import ast
import unittest
from pathlib import Path


class CreativeSafetySourceContractTests(unittest.TestCase):
    def test_creative_handler_has_explicit_paid_confirmation_and_single_shot_prefix(self) -> None:
        source = Path("handlers/clientplatform_creative_studio.py").read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn("✅ Создать 1 картинку", source)
        self.assertIn('"cpc:generate:"', source)
        self.assertIn('"_ONE_SHOT_PREFIXES"', source)
        self.assertIn('"cpc:generate:"', source)
        self.assertIn('"cpc:abandon:"', source)
        self.assertIn('"cpc:redeliver:"', source)
        self.assertIn("create_business_image_from_frozen_payload", source)
        self.assertIn("load_goal_visual_brand", source)
        self.assertIn('_receipt_callback("generate"', source)

    def test_existing_visual_gateway_is_reused_not_reimplemented(self) -> None:
        application = Path("clientplatform/application/visual_creatives.py").read_text(encoding="utf-8")
        ast.parse(application)
        self.assertIn("return submit_visual(", application)
        self.assertIn("build_business_image_brief", application)
        self.assertNotIn("requests.post", application)
        self.assertNotIn("httpx", application)


if __name__ == "__main__":
    unittest.main()
