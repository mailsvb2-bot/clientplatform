from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.clientplatform_http_probe import synthetic_journey


ROOT = Path(__file__).resolve().parents[1]


class ClientPlatformHttpProbeTransportTests(unittest.TestCase):
    @staticmethod
    def _responses(webhook_status: int):
        def request(
            url: str,
            *,
            method: str = "GET",
            payload: bytes | None = None,
            headers: dict[str, str] | None = None,
            timeout: float = 10.0,
        ) -> tuple[int, bytes, float]:
            del payload, headers, timeout
            if url == "http://127.0.0.1:8182/healthz":
                return 200, b'{"ok":true}', 0.01
            if url == "http://127.0.0.1:8182/readyz":
                return 200, b'{"ok":true}', 0.01
            if url == "https://client.example.test/" and method == "GET":
                return 200, b"ClientPlatform", 0.01
            if url == "https://client.example.test/telegram-webhook" and method == "POST":
                return webhook_status, b"", 0.01
            raise AssertionError(f"unexpected request: {method} {url}")

        return request

    def test_polling_requires_telegram_webhook_to_be_absent(self) -> None:
        with patch(
            "scripts.clientplatform_http_probe._request",
            side_effect=self._responses(404),
        ):
            result = synthetic_journey(
                health_base_url="http://127.0.0.1:8182",
                public_base_url="https://client.example.test",
                webhook_prefix="/telegram-webhook",
                telegram_transport="polling",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["telegram_transport"], "polling")
        self.assertEqual(result["telegram_webhook_absent"]["status"], 404)
        self.assertTrue(result["public_root"]["body_exact"])
        self.assertNotIn("invalid_webhook_secret", result)

    def test_polling_rejects_exposed_telegram_webhook_path(self) -> None:
        with patch(
            "scripts.clientplatform_http_probe._request",
            side_effect=self._responses(200),
        ):
            result = synthetic_journey(
                health_base_url="http://127.0.0.1:8182",
                public_base_url="https://client.example.test",
                webhook_prefix="/telegram-webhook",
                telegram_transport="polling",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["telegram_webhook_absent"]["status"], 200)

    def test_webhook_transport_requires_secret_rejection(self) -> None:
        with patch(
            "scripts.clientplatform_http_probe._request",
            side_effect=self._responses(403),
        ):
            result = synthetic_journey(
                health_base_url="http://127.0.0.1:8182",
                public_base_url="https://client.example.test",
                webhook_prefix="/telegram-webhook",
                telegram_transport="webhook",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["invalid_webhook_secret"]["status"], 403)
        self.assertNotIn("telegram_webhook_absent", result)

    def test_unknown_transport_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "polling or webhook"):
            synthetic_journey(
                health_base_url="http://127.0.0.1:8182",
                public_base_url="https://client.example.test",
                webhook_prefix="/telegram-webhook",
                telegram_transport="unknown",
            )

    def test_caddy_exposes_only_exact_root_as_public_fallback(self) -> None:
        caddy = (ROOT / "deploy/clientplatform/Caddyfile").read_text(encoding="utf-8")

        self.assertIn('handle / {\n        respond "ClientPlatform" 200', caddy)
        self.assertIn('handle {\n        respond "not found" 404', caddy)
        self.assertIn('handle /healthz {\n        respond "not public" 404', caddy)
        self.assertIn('handle /readyz {\n        respond "not public" 404', caddy)


if __name__ == "__main__":
    unittest.main()
