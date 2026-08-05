from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from runtime import messenger_webhooks


class AdOAuthHealthTests(unittest.TestCase):
    def test_ad_oauth_health_is_additive_and_backward_compatible(self) -> None:
        disabled = asyncio.run(messenger_webhooks._health(SimpleNamespace(app={})))
        self.assertEqual(
            json.loads(disabled.body),
            {
                "ok": True,
                "service": "http-ingress",
            },
        )

        enabled = asyncio.run(
            messenger_webhooks._health(
                SimpleNamespace(app={"clientplatform_ad_oauth_bot": object()})
            )
        )
        self.assertEqual(
            json.loads(enabled.body),
            {
                "ok": True,
                "service": "http-ingress",
                "ad_oauth": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
