from __future__ import annotations

import unittest

from scripts.check_clientplatform_product_purity import _forbidden_tokens
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / ".github" / "workflows" / "production-server-topology-probe.yml"
RECOVERY = ROOT / ".github" / "workflows" / "production-deploy-recovery.yml"
DISK_MAINTENANCE = ROOT / ".github" / "workflows" / "production-disk-maintenance.yml"
BRANCH_CLEANUP = ROOT / ".github" / "workflows" / "single-main-topology.yml"
REPAIR = ROOT / "scripts" / "repair_production_deploy_channel.sh"
OPERATIONS = ROOT / "deploy" / "clientplatform" / "GITHUB_OPERATIONS.md"
VISUAL_DIAGNOSTIC = ROOT / ".github" / "workflows" / "production-visual-provider-diagnostic.yml"


class ProductionWorkflowIsolationTests(unittest.TestCase):
    def _text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_clientplatform_production_operations_do_not_reference_other_products(self) -> None:
        for path in (TOPOLOGY, RECOVERY, REPAIR):
            with self.subTest(path=path):
                text = self._text(path)
                lowered = text.lower()
                for token in _forbidden_tokens():
                    self.assertNotIn(token.casefold(), lowered)
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

    def test_recovery_time_budget_covers_full_sequential_rollout(self) -> None:
        recovery = self._text(RECOVERY)
        diagnostic = self._text(VISUAL_DIAGNOSTIC)

        self.assertIn("timeout-minutes: 30", recovery)
        self.assertIn("timeout-minutes: 40", diagnostic)
        self.assertIn("const deadline = Date.now() + 30 * 60 * 1000;", diagnostic)


    def test_recovery_is_exact_sha_fast_forward_clientplatform_deploy(self) -> None:
        text = self._text(RECOVERY)
        for required in (
            "/opt/clientplatform",
            "mailsvb2-bot/clientplatform",
            "${{ github.sha }}",
            "persist-credentials: false",
            "fetch-depth: 0",
            'git bundle create "$source_bundle" "$source_ref"',
            'git bundle verify "$source_bundle"',
            'git fetch "$source_bundle" "$source_ref:refs/remotes/origin/main"',
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
        self.assertNotIn("git fetch --prune origin main", text)
        self.assertNotIn("git fetch origin", text)


    def test_disk_maintenance_is_marker_gated_and_never_prunes_runtime_state(self) -> None:
        text = self._text(DISK_MAINTENANCE)
        for required in (
            "workflow_dispatch:",
            "push:",
            "- main",
            "[production-disk-cleanup]",
            "github.event_name == 'workflow_dispatch'",
            "docker builder prune --force --all --keep-storage 512MB",
            "journalctl --vacuum-size=256M",
            "apt-get clean",
            "/opt/clientplatform",
            "StrictHostKeyChecking=yes",
            "UserKnownHostsFile=",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        for forbidden in (
            "docker image prune",
            "docker volume prune",
            "docker system prune",
            "docker network prune",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_disk_maintenance_foreign_worktree_audit_is_ownership_safe_and_fail_closed(self) -> None:
        text = self._text(DISK_MAINTENANCE)

        # Canonical /opt/clientplatform is already checked for branch=main and a clean
        # tracked worktree before snapshot. Arbitrary discovered repositories are
        # inventory-only: never execute Git in a repository we do not trust.
        self.assertGreaterEqual(text.count("audit=inventory_only"), 2)
        self.assertGreaterEqual(text.count("audit=canonical_prechecked"), 2)
        self.assertGreaterEqual(text.count("branch=not_evaluated"), 2)
        self.assertGreaterEqual(text.count("dirty=not_evaluated"), 2)
        self.assertGreaterEqual(text.count("reason=owner_unknown"), 2)
        self.assertNotIn("audit_git_as_owner", text)
        self.assertNotIn('git -C "$repo_path"', text)
        self.assertNotIn('git -c safe.directory="$repo_path"', text)
        self.assertNotIn('core.fsmonitor=false', text)
        self.assertNotIn('core.hooksPath=/dev/null', text)

        # The cleanup body runs in a fresh shell under flock, so it must initialize
        # the canonical repository path inside that subprocess instead of relying
        # on a non-exported parent-shell variable.
        cleanup_body = text.split("cat > \"$cleanup_script\" <<'CLEANUP'", 1)[1]
        self.assertIn("repo=/opt/clientplatform", cleanup_body)

        # Discovery itself is critical and must not disappear inside process
        # substitution or a best-effort `|| true`.
        self.assertGreaterEqual(text.count("WORKTREE_DISCOVERY_ERROR"), 2)
        self.assertIn("phase=pre_cleanup", text)
        self.assertIn("phase=post_cleanup", text)
        self.assertGreaterEqual(
            text.count('worktree_list="$(mktemp /tmp/clientplatform-worktrees.XXXXXX)"'),
            2,
        )
        self.assertGreaterEqual(text.count('done < "$worktree_list"'), 2)
        self.assertNotIn(
            'done < <(privileged find /root /home /opt /srv /tmp',
            text,
        )
        self.assertNotIn(
            'done < <(find /root /home /opt /srv /tmp',
            text,
        )
        self.assertGreaterEqual(text.count("worktree_audit_errors=0"), 2)
        self.assertGreaterEqual(
            text.count('if [ "$worktree_audit_errors" -ne 0 ]; then'),
            2,
        )
        self.assertIn("return 24", text)
        self.assertIn("exit 24", text)

    def test_branch_cleanup_deletes_only_exact_merged_pr_heads(self) -> None:
        text = self._text(BRANCH_CLEANUP)
        for required in (
            "pull-requests: read",
            "state: 'open'",
            "state: 'closed'",
            "base: 'main'",
            "pull.head.sha === branch.commit.sha",
            "github.rest.git.getRef",
            "currentRef.data.object.sha !== branch.commit.sha",
            "openPullsBeforeDelete",
            "Deleting proven-merged non-main branch",
            "Refusing to delete non-main branches without exact merged proof",
            "ops/single-main-topology",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        self.assertNotIn("Deleting non-main branch:", text)

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

    def test_visual_diagnostic_redacts_provider_controlled_job_fields(self) -> None:
        text = self._text(VISUAL_DIAGNOSTIC)
        for required in (
            "def safe_error_code(value: object) -> str:",
            "redacted_untrusted_error",
            '"model_recorded": bool(str(row["model"] or "").strip())',
            "CLIENTPLATFORM_PRODUCTION_VISUAL_RECENT_JOB_COUNTS",
            "CLIENTPLATFORM_PRODUCTION_VISUAL_RECENT_JOBS",
            'privileged docker exec -i "$provider_id" python -',
            "CLIENTPLATFORM_PRODUCTION_VISUAL_PROVIDER_CREDENTIAL_PRESENCE",
            "CLIENTPLATFORM_PRODUCTION_VISUAL_PROVIDER_CONTENT_CHAIN",
            "CLIENTPLATFORM_PRODUCTION_VISUAL_APP_CONTENT_CHAIN",
            'CLIENTPLATFORM_PROBE_JOB_ID="$job_id"',
            'CLIENTPLATFORM_PROBE_SCOPE_ID="$scope_id"',
            '"YANDEX_MODEL_CATALOG_API_KEY": bool(os.environ.get("YANDEX_MODEL_CATALOG_API_KEY"))',
            '"YANDEX_MODEL_CATALOG_AUTH_SCHEME": bool(os.environ.get("YANDEX_MODEL_CATALOG_AUTH_SCHEME"))',
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        app_probe, provider_probe = text.split('privileged docker exec -i "$provider_id" python -', 1)
        self.assertNotIn("YANDEX_MODEL_CATALOG_API_KEY", app_probe)
        self.assertNotIn("YANDEX_MODEL_CATALOG_AUTH_SCHEME", app_probe)
        self.assertIn("YANDEX_MODEL_CATALOG_API_KEY", provider_probe)
        self.assertIn("YANDEX_MODEL_CATALOG_AUTH_SCHEME", provider_probe)
        self.assertNotIn('"model": str(row["model"] or "")[:160]', text)
        self.assertNotIn('error_code = str(row["error_code"] or "")[:160]', text)
        self.assertNotIn('print(job_id)', text)
        self.assertNotIn('print(scope_id)', text)

    def test_visual_diagnostic_waits_for_exact_marked_deploy_before_probe(self) -> None:
        text = self._text(VISUAL_DIAGNOSTIC)

        wait_step = text.index("- name: Wait for marked production deploy")
        probe_step = text.index("- name: Probe live production visual provider state")
        self.assertLess(wait_step, probe_step)
        for required in (
            "github.event_name == 'push'",
            "contains(github.event.head_commit.message, '[recover-production-deploy]')",
            "ops/clientplatform-production-deploy-recovery",
            "listCommitStatusesForRef",
            "deploy?.state === 'success'",
            "['failure', 'error'].includes(deploy.state)",
            "Timed out waiting for exact production deploy recovery status before visual probe.",
            "timeout-minutes: 40",
            "const deadline = Date.now() + 30 * 60 * 1000;",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)


    def test_operations_doc_preserves_private_health_contract(self) -> None:
        text = self._text(OPERATIONS)
        self.assertIn("`/opt/clientplatform`", text)
        self.assertIn("loopback-only", text)
        self.assertIn("verified value", text)
        self.assertIn("There is no cross-product webhook fallback", text)
        self.assertIn("repair_production_deploy_channel.sh", text)


if __name__ == "__main__":
    unittest.main()