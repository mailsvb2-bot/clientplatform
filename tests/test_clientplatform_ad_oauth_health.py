from __future__ import annotations

import unittest
from pathlib import Path


class AdOAuthHealthContractTests(unittest.TestCase):
    def test_ad_oauth_health_is_additive_and_backward_compatible(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "runtime"
            / "messenger_webhooks.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'payload: dict[str, Any] = {"ok": True, "service": "http-ingress"}',
            source,
        )
        self.assertIn(
            'request.app.get("clientplatform_ad_oauth_bot") is not None',
            source,
        )
        self.assertIn('payload["ad_oauth"] = True', source)
        self.assertNotIn('"ad_oauth": False', source)


if __name__ == "__main__":
    unittest.main()
