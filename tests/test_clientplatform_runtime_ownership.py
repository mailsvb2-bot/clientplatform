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
        )
        for relative in required_scripts:
            with self.subTest(path=relative):
                self.assertTrue((root / relative).is_file(), relative)

        postgres_backup = (root / "scripts/clientplatform_postgres_backup.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('subparsers.add_parser("restore-drill")', postgres_backup)
        self.assertIn("CLIENTPLATFORM_RESTORE_DRILL_OK", postgres_backup)

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
        self.assertTrue(wrapper.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n"))
        self.assertIn(
            'exec python3 "$ROOT/scripts/clientplatform_production_deploy.py" "$@"',
            wrapper,
        )
        self.assertNotIn("scripts/immutable_deploy.sh", wrapper)
        self.assertNotIn("run_deploy_worker.sh", wrapper)
        self.assertNotIn("repair_contaminated_current_release.sh", wrapper)
        self.assertNotIn("/root/metrotherapy", wrapper)
        self.assertNotIn("/etc/metrotherapy", wrapper)
        self.assertNotIn("metrotherapy.service", wrapper)

    def test_canonical_deploy_owns_lock_backup_readiness_rollback_and_evidence(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts/clientplatform_production_deploy.py").read_text(
            encoding="utf-8"
        )

        main_start = source.index("def main() -> int:")
        lock = source.index(
            "fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)",
            main_start,
        )
        deploy_call = source.index("            deploy(\n", lock)
        self.assertLess(lock, deploy_call)
        self.assertIn("CLIENTPLATFORM_PRODUCTION_DEPLOY_FAILED:production_deploy_already_running", source)

        deploy_start = source.index("def deploy(\n")
        encrypted_backup = source.index(
            "backup_reference = _encrypted_backup(compose)", deploy_start
        )
        emergency_backup = source.index(
            "backup_reference = str(_local_backup(target_sha))", deploy_start
        )
        build = source.index('_run([*compose, "build", "app", "backup"])', deploy_start)
        rollout = source.index(
            '_run([*compose, "up", "-d", "--force-recreate", "app", "caddy"])',
            build,
        )
        readiness = source.index("_wait_for_readiness(timeout_seconds)", rollout)
        external = source.index("_external_https(domain)", readiness)
        rollback = source.index("                _rollback(\n", external)
        rollback_evidence = source.index(
            "            rollback_evidence = _write_evidence(\n", rollback
        )
        success_evidence = source.index("    evidence = _write_evidence(\n", rollback_evidence)

        self.assertLess(encrypted_backup, build)
        self.assertLess(emergency_backup, build)
        self.assertLess(build, rollout)
        self.assertLess(rollout, readiness)
        self.assertLess(readiness, external)
        self.assertLess(external, rollback)
        self.assertLess(rollback, rollback_evidence)
        self.assertLess(rollback_evidence, success_evidence)
        self.assertNotIn("run_deploy_worker.sh", source)
        self.assertNotIn("scripts/immutable_deploy.sh", source)
        self.assertNotIn("/var/lib/metrotherapy", source)

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
