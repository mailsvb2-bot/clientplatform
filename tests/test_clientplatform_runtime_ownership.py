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
            "scripts/install_github_deploy_webhook_service.sh",
            "scripts/run_deploy_worker.sh",
            "scripts/run_deploy_worker_observed.sh",
            "scripts/recover_stale_deploy_worker.sh",
            "tests/test_deploy_webhook_hardening.py",
            "tests/test_deploy_webhook_worker_isolation.py",
            "tests/test_github_deploy_webhook_service_installer.py",
            "tests/test_nginx_runtime_routes_contract.py",
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

        required_scripts = (
            "scripts/clientplatform_postgres_backup.py",
            "scripts/clientplatform_production_deploy.py",
            "scripts/clientplatform_production_preflight.py",
            "scripts/clientplatform_postgres_restore_drill.py",
        )
        for relative in required_scripts:
            with self.subTest(path=relative):
                self.assertTrue((root / relative).is_file(), relative)

        contract = (root / "deploy" / "RUNTIME_CONTRACT.md").read_text(
            encoding="utf-8"
        )
        self.assertTrue(contract.startswith("# ClientPlatform production runtime contract"))
        self.assertIn("deploy/clientplatform/", contract)
        self.assertNotIn("cd /root/metrotherapy", contract)
        self.assertNotIn("/etc/metrotherapy/metrotherapy.env", contract)

    def test_root_deploy_is_only_a_clientplatform_compatibility_entrypoint(self) -> None:
        root = Path(__file__).resolve().parents[1]
        wrapper = (root / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("scripts/clientplatform_production_deploy.py", wrapper)
        self.assertNotIn("scripts/immutable_deploy.sh", wrapper)
        self.assertNotIn("/root/metrotherapy", wrapper)
        self.assertNotIn("/etc/metrotherapy", wrapper)
        self.assertNotIn("metrotherapy.service", wrapper)

    def test_workflows_cannot_revive_legacy_deploy_webhook(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow_root = root / ".github" / "workflows"
        workflow_paths = sorted(workflow_root.glob("*.yml")) + sorted(
            workflow_root.glob("*.yaml")
        )
        workflow_text = "\n".join(
            path.read_text(encoding="utf-8") for path in workflow_paths
        )
        forbidden = (
            "tests.test_deploy_webhook_hardening",
            "ops/deploy_webhook_hardened.py",
            "github-deploy-webhook.service",
            "install_github_deploy_webhook_service.sh",
            "run_deploy_worker.sh",
            "run_deploy_worker_observed.sh",
            "recover_stale_deploy_worker.sh",
        )
        found = [value for value in forbidden if value in workflow_text]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
