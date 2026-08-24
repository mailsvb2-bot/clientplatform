from __future__ import annotations

import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.domain.connections import ConnectionPlatform
from config.settings import settings

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None


class _Request:
    def __init__(self, *, token: str = "A" * 43, form: dict[str, str] | None = None) -> None:
        self.match_info = {"token": token}
        self.content_type = "application/x-www-form-urlencoded"
        self._form = form or {}

    async def post(self):
        return self._form


@unittest.skipUnless(
    _AIOHTTP_AVAILABLE,
    "aiohttp runtime dependency is not installed in dependency-light Canon",
)
class NativeMessengerSetupHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_renders_password_form_with_no_store_headers(self) -> None:
        from clientplatform.runtime.native_messenger_setup_http import (
            native_messenger_setup_get,
        )

        grant = SimpleNamespace(
            business_name="Практика",
            platform=ConnectionPlatform.VK,
        )
        with patch(
            "clientplatform.runtime.native_messenger_setup_http.inspect_native_messenger_setup",
            return_value=grant,
        ):
            response = await native_messenger_setup_get(_Request())  # type: ignore[arg-type]

        self.assertEqual(response.status, 200)
        self.assertIn('type="password"', response.text)
        self.assertIn("ID сообщества", response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    async def test_post_consumes_capability_and_never_echoes_provider_token(self) -> None:
        from clientplatform.runtime.native_messenger_setup_http import (
            native_messenger_setup_post,
        )

        preview = SimpleNamespace(
            business_name="Практика",
            platform=ConnectionPlatform.MAX,
        )
        actor = object()
        grant = SimpleNamespace(
            business_name="Практика",
            platform=ConnectionPlatform.MAX,
            actor=actor,
        )
        result = SimpleNamespace(display_name="MAX Бот", username="max_bot")
        provision = AsyncMock(return_value=result)
        with (
            patch(
                "clientplatform.runtime.native_messenger_setup_http.inspect_native_messenger_setup",
                return_value=preview,
            ),
            patch(
                "clientplatform.runtime.native_messenger_setup_http.consume_native_messenger_setup",
                return_value=grant,
            ) as consume,
            patch(
                "clientplatform.runtime.native_messenger_setup_http.provision_max_channel",
                provision,
            ),
            patch.object(
                settings,
                "MESSENGER_PUBLIC_BASE_URL",
                "https://client.example.test",
            ),
        ):
            response = await native_messenger_setup_post(
                _Request(form={"provider_token": "raw-provider-secret"})  # type: ignore[arg-type]
            )

        self.assertEqual(response.status, 200)
        self.assertNotIn("raw-provider-secret", response.text)
        consume.assert_called_once()
        self.assertEqual(provision.await_args.kwargs["provider_token"], "raw-provider-secret")
        self.assertIs(provision.await_args.kwargs["actor"], actor)


if __name__ == "__main__":
    unittest.main()
