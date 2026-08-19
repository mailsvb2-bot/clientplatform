from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.integrations.yandex_direct import YandexDirectError
from handlers import clientplatform_yandex_screen_code as screen_code


class _State:
    def __init__(self) -> None:
        self.data = {
            "business_token": "business-1",
            "oauth_state": "oauth-state",
            "oauth_user_id": 101,
        }
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.cleared = True
        self.data.clear()


def _message(*, text: str | None = "opaque-code", caption: str | None = None):
    return SimpleNamespace(
        text=text,
        caption=caption,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        delete=AsyncMock(),
    )


async def _immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


class YandexCodeRetryFsmTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_code_rejections_keep_fsm_for_resubmission(self) -> None:
        for provider_code in ("provider_invalid_grant", "provider_bad_verification_code"):
            with self.subTest(provider_code=provider_code):
                state = _State()
                incoming = _message()
                with (
                    patch.object(screen_code.asyncio, "to_thread", new=_immediate_to_thread),
                    patch.object(screen_code.control, "_user_id", return_value=101),
                    patch.object(
                        screen_code,
                        "screen_code_provider_from_environment",
                        return_value=object(),
                    ),
                    patch.object(
                        screen_code,
                        "complete_yandex_direct_oauth",
                        side_effect=YandexDirectError(provider_code),
                    ),
                ):
                    await screen_code.complete_yandex_direct_screen_code(incoming, state)

                incoming.delete.assert_awaited_once()
                self.assertFalse(state.cleared)
                self.assertEqual(state.data["oauth_state"], "oauth-state")
                rendered = incoming.answer.await_args.args[0]
                self.assertIn("Яндекс не принял этот код", rendered)
                self.assertIn("сессия подключения пока сохранена", rendered)
                self.assertNotIn(provider_code, rendered)

    async def test_non_code_provider_failure_still_fails_closed_and_restarts(self) -> None:
        state = _State()
        incoming = _message()
        with (
            patch.object(screen_code.asyncio, "to_thread", new=_immediate_to_thread),
            patch.object(screen_code.control, "_user_id", return_value=101),
            patch.object(
                screen_code,
                "screen_code_provider_from_environment",
                return_value=object(),
            ),
            patch.object(
                screen_code,
                "complete_yandex_direct_oauth",
                side_effect=YandexDirectError("provider_invalid_client"),
            ),
        ):
            await screen_code.complete_yandex_direct_screen_code(incoming, state)

        self.assertTrue(state.cleared)
        self.assertIn("Начните подключение", incoming.answer.await_args.args[0])
        self.assertNotIn("provider_invalid_client", incoming.answer.await_args.args[0])

    def test_confirmation_code_can_be_read_from_textual_caption(self) -> None:
        incoming = _message(text=None, caption="caption-code")
        self.assertEqual(screen_code._incoming_confirmation_code(incoming), "caption-code")


if __name__ == "__main__":
    unittest.main()
