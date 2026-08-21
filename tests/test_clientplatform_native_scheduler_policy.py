from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from services import scheduler


async def _auto_audio_tick(_bot: object) -> None:
    return None


async def _engine_tick(_bot: object) -> None:
    return None


class NativeSchedulerPolicyTests(unittest.TestCase):
    def test_native_only_scheduler_starts_no_telegram_owner_ticks(self) -> None:
        with patch.object(scheduler, "_start_owner_tick") as start_owner:
            scheduler._start_telegram_owner_ticks(
                None,
                auto_audio_tick=_auto_audio_tick,
                engine_tick=_engine_tick,
            )
        start_owner.assert_not_called()

    def test_telegram_scheduler_keeps_both_legacy_owner_ticks(self) -> None:
        bot = object()
        with patch.object(scheduler, "_start_owner_tick") as start_owner:
            scheduler._start_telegram_owner_ticks(
                bot,
                auto_audio_tick=_auto_audio_tick,
                engine_tick=_engine_tick,
            )
        self.assertEqual(2, start_owner.call_count)
        self.assertEqual("auto_audio.tick", start_owner.call_args_list[0].args[0])
        self.assertEqual("engine.tick", start_owner.call_args_list[1].args[0])


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
