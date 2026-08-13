from __future__ import annotations

import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4


_AIOGRAM_AVAILABLE = importlib.util.find_spec("aiogram") is not None


class _State:
    def __init__(self) -> None:
        self.cleared = 0

    async def clear(self) -> None:
        self.cleared += 1


class _Message:
    def __init__(self) -> None:
        self.answers: list[tuple[str, dict[str, object]]] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append((text, kwargs))


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=101)
        self.message = _Message()
        self.answers: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def answer(self, *args: object, **kwargs: object) -> None:
        self.answers.append((args, kwargs))


async def _inline_to_thread(func, /, *args, **kwargs):
    return func(*args, **kwargs)


@unittest.skipUnless(_AIOGRAM_AVAILABLE, "aiogram is not installed")
class SalesAIHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from handlers import clientplatform_sales

        self.sales = clientplatform_sales
        self.business_id = str(uuid4())
        self.lead_id = str(uuid4())
        self.token = self.sales._token(self.business_id)
        self.actor = SimpleNamespace(business_id=self.business_id)
        self.control_patches = [
            patch.object(
                self.sales.control,
                "_actor",
                AsyncMock(return_value=self.actor),
            ),
            patch.object(
                self.sales.control,
                "_callback_message",
                lambda callback: callback.message,
            ),
            patch.object(self.sales.control, "_keyboard", lambda rows: rows),
        ]
        for item in self.control_patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_toggle_sales_ai_fails_closed_when_runtime_is_unavailable(self) -> None:
        callback = _Callback(f"cps:sat:{self.token}")
        state = _State()
        from clientplatform.application import sales_ai_drafts

        with patch.object(
            sales_ai_drafts,
            "sales_ai_runtime_available",
            return_value=False,
        ):
            await self.sales.toggle_sales_ai(callback, state)

        self.assertEqual(state.cleared, 0)
        self.assertEqual(
            callback.answers[-1],
            (("ИИ сейчас не настроен на сервере",), {"show_alert": True}),
        )

    async def test_toggle_sales_ai_disables_existing_consent_and_refreshes_work(self) -> None:
        callback = _Callback(f"cps:sat:{self.token}")
        state = _State()
        refresh = AsyncMock()
        from clientplatform.application import sales_ai_drafts, sales_ai_settings

        changes: list[tuple[object, bool]] = []

        def change(*, actor, enabled, **_kwargs):
            changes.append((actor, enabled))
            return enabled

        with (
            patch.object(self.sales.asyncio, "to_thread", _inline_to_thread),
            patch.object(self.sales, "_send_sales_work", refresh),
            patch.object(
                sales_ai_drafts,
                "sales_ai_runtime_available",
                return_value=True,
            ),
            patch.object(
                sales_ai_settings,
                "get_business_sales_ai_enabled",
                return_value=True,
            ),
            patch.object(
                sales_ai_settings,
                "set_business_sales_ai_enabled",
                side_effect=change,
            ),
        ):
            await self.sales.toggle_sales_ai(callback, state)

        self.assertEqual(state.cleared, 1)
        self.assertEqual(changes, [(self.actor, False)])
        self.assertEqual(callback.answers[-1][0], ("ИИ-помощник выключен",))
        refresh.assert_awaited_once()

    async def test_toggle_sales_ai_shows_provider_bound_consent_before_enabling(self) -> None:
        callback = _Callback(f"cps:sat:{self.token}")
        state = _State()
        from clientplatform.application import sales_ai_drafts, sales_ai_settings

        with (
            patch.object(self.sales.asyncio, "to_thread", _inline_to_thread),
            patch.object(
                sales_ai_drafts,
                "sales_ai_runtime_available",
                return_value=True,
            ),
            patch.object(
                sales_ai_drafts,
                "sales_ai_runtime_provider_label",
                return_value="DeepSeek",
            ),
            patch.object(
                sales_ai_drafts,
                "sales_ai_runtime_consent_target",
                return_value="deepseek:https://api.deepseek.com",
            ),
            patch.object(
                sales_ai_settings,
                "get_business_sales_ai_enabled",
                return_value=False,
            ),
        ):
            await self.sales.toggle_sales_ai(callback, state)

        self.assertEqual(state.cleared, 1)
        self.assertEqual(callback.answers[-1], ((), {}))
        text, kwargs = callback.message.answers[-1]
        self.assertIn("DeepSeek", text)
        self.assertIn("deepseek:https://api.deepseek.com", text)
        self.assertIn("не получает права отправлять сообщения", text)
        buttons = kwargs["reply_markup"]
        self.assertEqual(buttons[0][0][1], f"cps:sae:{self.token}")

    async def test_enable_sales_ai_uses_redacted_mode_and_notice_confirmation(self) -> None:
        callback = _Callback(f"cps:sae:{self.token}")
        state = _State()
        refresh = AsyncMock()
        captured: dict[str, object] = {}
        from clientplatform.application import sales_ai_drafts, sales_ai_settings

        def enable(**kwargs: object) -> bool:
            captured.update(kwargs)
            return True

        with (
            patch.object(self.sales.asyncio, "to_thread", _inline_to_thread),
            patch.object(self.sales, "_send_sales_work", refresh),
            patch.object(
                sales_ai_drafts,
                "sales_ai_runtime_available",
                return_value=True,
            ),
            patch.object(
                sales_ai_settings,
                "set_business_sales_ai_enabled",
                side_effect=enable,
            ),
        ):
            await self.sales.enable_sales_ai(callback, state)

        self.assertEqual(state.cleared, 1)
        self.assertIs(captured["actor"], self.actor)
        self.assertIs(captured["enabled"], True)
        self.assertEqual(captured["data_mode"], "redacted")
        self.assertIs(captured["customer_notice_confirmed"], True)
        self.assertEqual(callback.answers[-1][0], ("ИИ-помощник включён",))
        refresh.assert_awaited_once()

    async def test_draft_sales_answer_shows_review_only_draft(self) -> None:
        callback = _Callback(
            f"cps:sad:{self.token}:{self.sales._token(self.lead_id)}"
        )
        state = _State()
        from clientplatform.application import sales_ai_drafts

        with patch.object(
            sales_ai_drafts,
            "draft_sales_reply",
            AsyncMock(return_value=SimpleNamespace(text="Предлагаю обсудить аудит.")),
        ):
            await self.sales.draft_sales_answer(callback, state)

        self.assertEqual(state.cleared, 1)
        self.assertEqual(callback.answers[0][0], ("Готовлю черновик…",))
        text, _kwargs = callback.message.answers[-1]
        self.assertIn("Предлагаю обсудить аудит.", text)
        self.assertIn("ничего не отправил клиенту автоматически", text)


if __name__ == "__main__":
    unittest.main()
