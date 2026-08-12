from __future__ import annotations

import unittest
from pathlib import Path


class ClientPlatformRuntimeOwnershipTests(unittest.TestCase):
    def test_legacy_metrotherapy_production_cluster_is_absent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        legacy_paths = (
            "deploy/deploy.sh",
            "deploy/install_server.sh",
            "deploy/metrotherapy.env.example",
            "deploy/metrotherapy.service",
            "deploy/nginx-metrotherapy.conf",
            "deploy/post_deploy_smoke.sh",
            "deploy/github-deploy-webhook.service",
            "ops/deploy_webhook.py",
            "ops/deploy_webhook_hardened.py",
            "ops/autodeploy_smoke_marker.md",
        )
        for relative in legacy_paths:
            with self.subTest(path=relative):
                self.assertFalse((root / relative).exists(), relative)

    def test_deploy_root_cannot_become_a_second_runtime(self) -> None:
        root = Path(__file__).resolve().parents[1]
        deploy_root = root / "deploy"
        root_files = {path.name for path in deploy_root.iterdir() if path.is_file()}
        self.assertEqual(root_files, {"RUNTIME_CONTRACT.md"})

    def test_clientplatform_runtime_is_canonical_and_complete(self) -> None:
        root = Path(__file__).resolve().parents[1]
        canonical = root / "deploy" / "clientplatform"
        required = (
            "Caddyfile",
            "clientplatform.production.env.example",
            "clientplatform.service",
            "compose.production.yml",
        )
        for relative in required:
            with self.subTest(path=relative):
                self.assertTrue((canonical / relative).is_file(), relative)

        contract = (root / "deploy" / "RUNTIME_CONTRACT.md").read_text(
            encoding="utf-8"
        )
        self.assertTrue(contract.startswith("# ClientPlatform production runtime contract"))
        self.assertIn("deploy/clientplatform/", contract)
        self.assertNotIn("cd /root/metrotherapy", contract)
        self.assertNotIn("/etc/metrotherapy/metrotherapy.env", contract)


if __name__ == "__main__":
    unittest.main()
