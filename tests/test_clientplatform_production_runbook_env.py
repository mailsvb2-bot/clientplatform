from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "runbooks" / "CLIENTPLATFORM_PRODUCTION_ISOLATION.md"


class ClientPlatformProductionRunbookEnvTests(unittest.TestCase):
    def test_manual_compose_commands_use_production_env_file(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")

        self.assertIn(
            "docker compose --env-file clientplatform.env -f compose.production.yml config",
            text,
        )
        self.assertIn(
            "docker compose --env-file clientplatform.env -f compose.production.yml up -d --build",
            text,
        )
        self.assertIn("`--env-file clientplatform.env` is mandatory", text)
        self.assertNotIn("docker compose -f compose.production.yml config", text)
        self.assertNotIn("docker compose -f compose.production.yml up -d --build", text)


if __name__ == "__main__":
    unittest.main()
