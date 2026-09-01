from __future__ import annotations

import unittest
from pathlib import Path

class NativeRuntimeOwnershipTests(unittest.TestCase):
    def test_retired_scheduler_and_second_brain_are_absent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "services/scheduler.py").exists())
        self.assertFalse((root / "core/engine.py").exists())
        self.assertFalse((root / "core/ai/decision_core.py").exists())
        source = (root / "app.py").read_text(encoding="utf-8")
        for retired in ("start_scheduler", "SelfHealingEngine", "DecisionCore", "SOVEREIGN_ENABLED"):
            self.assertNotIn(retired, source)


class NativeProcessSourceContractTests(unittest.TestCase):
    def test_native_only_branch_precedes_telegram_bot_construction(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(
            encoding="utf-8"
        )
        native_branch = source.index("if not telegram_enabled:")
        native_start = source.index("await _on_startup(None)", native_branch)
        token_lookup = source.index("token = (settings.BOT_TOKEN", native_branch)
        bot_construction = source.index("bot = build_bot(token)", token_lookup)
        self.assertLess(native_branch, native_start)
        self.assertLess(native_start, token_lookup)
        self.assertLess(token_lookup, bot_construction)

    def test_native_only_startup_uses_canonical_policy_and_http_ingress(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("assert_native_only_runtime_policy()", source)
        self.assertIn(
            "start_messenger_webhook_runtime(bot=bot, dispatcher=dp)",
            source,
        )
        self.assertIn(
            'raise RuntimeError("native-only runtime requires canonical HTTP ingress")',
            source,
        )


if __name__ == "__main__":
    unittest.main()
