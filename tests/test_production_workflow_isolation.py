from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / ".github" / "workflows" / "production-server-topology-probe.yml"
RECOVERY = ROOT / ".github" / "workflows" / "production-deploy-recovery.yml"
REPAIR = ROOT / "scripts" / "repair_production_deploy_channel.sh"
OPERATIONS = ROOT / "deploy" / "clientplatform" / "GITHUB_OPERATIONS.md"


class ProductionWorkflowIsolationTests(unittest.TestCase):
    def _text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_clientplatform_production_operations_do_not_reference_other_products(self) -> None:
        for path in (TOPOLOGY, RECOVERY, REPAIR):
            with self.subTest(path=path):
                text = self._text(path)
                lowered = text.lower()
                self.assertNotIn("metrotherapy", lowered)
                self.assertNotIn("metro_", lowered)
                self.assertNotIn("/github-deploy", lowered)

    def test_topology_probe_targets_dedicated_checkout_and_is_fail_closed(self) -> None:
        text = self._text(TOPOLOGY)
        for required in (
            "/opt/clientplatform",
            "mailsvb2-bot/clientplatform",
            "refs/heads",
            "local_branch_count",
            "local_branches",
            "current_branch",
            "tracked_dirty_count",
            'branch_count" != "1"',
            'branch_csv" != "main"',
            'current_branch" != "main"',
            "StrictHostKeyChecking=yes",
            "UserKnownHostsFile=",
            "CLIENTPLATFORM_PRODUCTION_SSH_HOST",
            "CLIENTPLATFORM_PRODUCTION_SSH_USER",
            "CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY",
            "CLIENTPLATFORM_PRODUCTION_SSH_KNOWN_HOSTS",
            "ops/clientplatform-server-single-main",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_recovery_is_exact_sha_fast_forward_clientplatform_deploy(self) -> None:
        text = self._text(RECOVERY)
        for required in (
            "/opt/clientplatform",
            "mailsvb2-bot/clientplatform",
            "${{ github.sha }}",
            "git fetch --prune origin main",
            "git merge --ff-only origin/main",
            'fetched_sha" != "$expected_sha"',
            "scripts/clientplatform_production_deploy.py",
            "--recover-unavailable-baseline",
            "StrictHostKeyChecking=yes",
            "UserKnownHostsFile=",
            "CLIENTPLATFORM_PRODUCTION_SSH_HOST",
            "CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY",
            "CLIENTPLATFORM_PRODUCTION_SSH_KNOWN_HOSTS",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_repair_bootstrap_only_configures_dedicated_clientplatform_ssh(self) -> None:
        text = self._text(REPAIR)
        for required in (
            'APP_DIR="${APP_DIR:-/opt/clientplatform}"',
            'REPO="${REPO:-mailsvb2-bot/clientplatform}"',
            "CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY_FILE",
            "CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY",
            "CLIENTPLATFORM_PRODUCTION_SSH_KNOWN_HOSTS",
            "/etc/ssh/ssh_host_ed25519_key.pub",
            "GITHUB_PRODUCTION_TRANSPORT=dedicated_ssh",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        self.assertNotIn("ssh-keyscan", text)

    def test_operations_doc_preserves_private_health_contract(self) -> None:
        text = self._text(OPERATIONS)
        self.assertIn("`/opt/clientplatform`", text)
        self.assertIn("loopback-only", text)
        self.assertIn("verified value", text)
        self.assertIn("There is no cross-product webhook fallback", text)
        self.assertIn("repair_production_deploy_channel.sh", text)


if __name__ == "__main__":
    unittest.main()
