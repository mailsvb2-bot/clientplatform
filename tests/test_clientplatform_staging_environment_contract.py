from __future__ import annotations

import unittest
from pathlib import Path


class ClientPlatformStagingEnvironmentContractTests(unittest.TestCase):
    def test_workflow_uses_configured_clientplatform_environment(self) -> None:
        workflow = Path(".github/workflows/clientplatform-telegram-staging.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("environment: clientplatform_bot", workflow)
        self.assertIn(
            "CLIENTPLATFORM_STAGING_TELEGRAM_BOT_TOKEN to the clientplatform_bot environment",
            workflow,
        )
        self.assertNotIn("environment: clientplatform-staging", workflow)


if __name__ == "__main__":
    unittest.main()
