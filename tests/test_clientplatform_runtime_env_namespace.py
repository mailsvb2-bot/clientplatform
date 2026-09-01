from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed local interpreter and test script
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_ENV_NAMES = (
    "CLIENTPLATFORM_DATA_DIR",
    "CLIENTPLATFORM_LOGS_DIR",
    "CLIENTPLATFORM_DB_ENGINE",
    "CLIENTPLATFORM_DB_PATH",
    "DATABASE_URL",
)
_PROBE = """
import json
from core import paths
from services.db import runtime
print(json.dumps({
    "data_dir": str(paths.DATA_DIR),
    "logs_dir": str(paths.LOGS_DIR),
    "db_engine": paths.DB_ENGINE,
    "db_path": str(paths.DB_PATH),
    "runtime_engine": runtime.CONFIG.engine,
    "driver_hint": runtime.postgres_driver_error_hint(),
}, sort_keys=True))
"""


class ClientPlatformRuntimeEnvironmentTests(unittest.TestCase):
    def _probe(self, values: dict[str, str]) -> dict[str, str]:
        env = dict(os.environ)
        for name in _ENV_NAMES:
            env.pop(name, None)
        env.update(
            {
                "APP_ENV": "test",
                "LOAD_DOTENV": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(ROOT),
            }
        )
        env.update(values)
        completed = subprocess.run(  # nosec B603 - static interpreter and script
            [sys.executable, "-c", _PROBE],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip())

    def test_clientplatform_namespace_controls_runtime_paths_and_database(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = self._probe(
                {
                    "CLIENTPLATFORM_DATA_DIR": str(root / "client-data"),
                    "CLIENTPLATFORM_LOGS_DIR": str(root / "client-logs"),
                    "CLIENTPLATFORM_DB_ENGINE": "sqlite",
                    "CLIENTPLATFORM_DB_PATH": str(root / "client.sqlite3"),
                }
            )
        self.assertTrue(result["data_dir"].endswith("client-data"))
        self.assertTrue(result["logs_dir"].endswith("client-logs"))
        self.assertEqual(result["db_engine"], "sqlite")
        self.assertEqual(result["runtime_engine"], "sqlite")
        self.assertTrue(result["db_path"].endswith("client.sqlite3"))
        self.assertIn("postgresql://clientplatform:secret", result["driver_hint"])

    def test_canonical_scenario_gate_uses_product_namespace(self) -> None:
        source = (ROOT / "scripts/all_user_scenario_gate.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"CLIENTPLATFORM_DB_ENGINE": "sqlite"', source)
        self.assertIn('env["CLIENTPLATFORM_DB_PATH"]', source)
        self.assertIn('"MESSENGER_WEBHOOK_ENABLED": "0"', source)
        self.assertIn('"VK_WEBHOOK_ENABLED": "0"', source)

    def test_migration_backend_detection_uses_canonical_runtime(self) -> None:
        paths = (
            ROOT / "services/migrations/_helpers.py",
            ROOT / "services/migrations/scheduled_jobs_to_jobs_v1.py",
        )
        for path in paths:
            source = path.read_text(encoding="utf-8")
            self.assertIn("is_postgres_enabled", source, str(path))
            self.assertNotIn("CLIENTPLATFORM_DB_ENGINE", source, str(path))

    def test_runtime_namespace_files_are_in_critical_static_gate(self) -> None:
        source = (ROOT / "scripts/critical_static_gate.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"core/paths.py"', source)
        self.assertIn('"services/db/runtime.py"', source)


if __name__ == "__main__":
    unittest.main()
