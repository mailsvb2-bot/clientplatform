from __future__ import annotations

import importlib.util
from types import SimpleNamespace
import unittest
from unittest.mock import patch

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None
if _AIOHTTP_AVAILABLE:
    from clientplatform.runtime import public_business


class _Request:
    def __init__(self, *, form=None, query=None, content_length=256):
        self.match_info = {"business_token": "business-token"}
        self.query = query or {}
        self.content_length = content_length
        self._form = form or {}

    async def post(self):
        return self._form


@unittest.skipUnless(
    _AIOHTTP_AVAILABLE,
    "aiohttp runtime dependency is not installed in dependency-light Canon",
)
class PublicBusinessHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_renders_escaped_first_party_form(self) -> None:
        entry = SimpleNamespace(
            business_name="Практика <script>",
            activity_description="Помощь <b>людям</b>",
        )
        with patch.object(
            public_business,
            "get_public_business_entry",
            return_value=entry,
        ):
            response = await public_business.public_business_page(
                _Request(
                    query={
                        "source": 'landing" autofocus',
                        "campaign_ref": "<campaign>",
                    }
                )
            )

        self.assertEqual(response.status, 200)
        self.assertIn("Практика &lt;script&gt;", response.text)
        self.assertIn("Помощь &lt;b&gt;людям&lt;/b&gt;", response.text)
        self.assertNotIn("<script>", response.text)
        self.assertIn("Отправить заявку", response.text)
        self.assertIn("name=consent", response.text)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("form-action 'self'", response.headers["Content-Security-Policy"])

    async def test_submit_requires_consent_without_writing(self) -> None:
        entry = SimpleNamespace(
            business_name="Практика",
            activity_description="",
        )
        with (
            patch.object(public_business, "get_public_business_entry", return_value=entry),
            patch.object(public_business, "capture_public_business_lead") as capture,
        ):
            response = await public_business.public_business_submit(
                _Request(
                    form={
                        "name": "Анна",
                        "email": "anna@example.test",
                    }
                )
            )
        self.assertEqual(response.status, 400)
        self.assertIn("согласие", response.text.lower())
        capture.assert_not_called()

    async def test_honeypot_returns_success_without_writing(self) -> None:
        entry = SimpleNamespace(
            business_name="Практика",
            activity_description="",
        )
        with (
            patch.object(public_business, "get_public_business_entry", return_value=entry),
            patch.object(public_business, "capture_public_business_lead") as capture,
        ):
            response = await public_business.public_business_submit(
                _Request(
                    form={
                        "name": "Bot",
                        "email": "bot@example.test",
                        "company": "spam",
                        "consent": "yes",
                    }
                )
            )
        self.assertEqual(response.status, 200)
        self.assertIn("Заявка принята", response.text)
        capture.assert_not_called()

    async def test_valid_submit_passes_bounded_attribution_to_canonical_capture(self) -> None:
        entry = SimpleNamespace(
            business_name="Практика",
            activity_description="",
        )
        with (
            patch.object(public_business, "get_public_business_entry", return_value=entry),
            patch.object(public_business, "capture_public_business_lead") as capture,
        ):
            response = await public_business.public_business_submit(
                _Request(
                    form={
                        "name": "Анна",
                        "email": "anna@example.test",
                        "phone": "+7 999 111 22 33",
                        "consent": "yes",
                        "source": "s" * 300,
                        "campaign_ref": "c" * 400,
                    }
                )
            )

        self.assertEqual(response.status, 200)
        self.assertIn("Заявка передана бизнесу", response.text)
        capture.assert_called_once_with(
            business_token="business-token",
            display_name="Анна",
            email="anna@example.test",
            phone="+7 999 111 22 33",
            source="s" * 120,
            campaign_ref="c" * 200,
        )

    async def test_oversized_body_is_rejected_before_parse(self) -> None:
        response = await public_business.public_business_submit(
            _Request(content_length=public_business.PUBLIC_BUSINESS_MAX_BODY_BYTES + 1)
        )
        self.assertEqual(response.status, 413)


if __name__ == "__main__":
    unittest.main()
