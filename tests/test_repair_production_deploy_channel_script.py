from __future__ import annotations

# This contract also protects the one-main cleanup path used after repair PRs.
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "repair_production_deploy_channel.sh"
RECOVERY_WORKFLOW = ROOT / ".github" / "workflows" / "production-deploy-recovery.yml"
TOPOLOGY_WORKFLOW = ROOT / ".github" / "workflows" / "production-server-topology-probe.yml"
CLEANUP_WORKFLOW = ROOT / ".github" / "workflows" / "single-main-topology.yml"


def test_production_deploy_repair_script_has_valid_bash_syntax() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    assert SCRIPT.is_file()

    completed = subprocess.run(
        [bash, "-n", str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_production_deploy_repair_script_is_clientplatform_only_and_secret_safe() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    lowered = text.lower()

    assert 'APP_DIR="${APP_DIR:-/opt/clientplatform}"' in text
    assert 'REPO="${REPO:-mailsvb2-bot/clientplatform}"' in text
    assert "metrotherapy" not in lowered
    assert "metro_" not in lowered
    assert "/github-deploy" not in lowered
    assert "CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY_FILE" in text
    assert "gh secret set CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY" in text
    assert 'cat "$SSH_PRIVATE_KEY_FILE"' in text
    assert "echo $SSH_PRIVATE_KEY" not in text
    assert "printf '%s' \"$SSH_PRIVATE_KEY\"" not in text
    assert "SERVER_LOCAL_BRANCH_COUNT=" in text
    assert "GITHUB_PRODUCTION_TRANSPORT=dedicated_ssh" in text


def test_production_deploy_repair_script_pins_known_host_from_local_sshd_identity() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "/etc/ssh/ssh_host_ed25519_key.pub" in text
    assert "ssh-keyscan" not in text
    assert "CLIENTPLATFORM_PRODUCTION_SSH_KNOWN_HOSTS" in text
    assert 'known_hosts_line="$known_host_name $host_key"' in text


def test_recovery_workflow_uses_dedicated_clientplatform_ssh_and_exact_trigger_sha() -> None:
    text = RECOVERY_WORKFLOW.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "metrotherapy" not in lowered
    assert "/github-deploy" not in lowered
    assert "secrets.CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY" in text
    assert "TRIGGER_SHA: ${{ github.sha }}" in text
    assert "GitHub recovery trigger SHA is invalid" in text
    assert 'fetched_sha" != "$expected_sha"' in text
    assert "git fetch --prune origin main" in text
    assert "git merge --ff-only origin/main" in text
    assert "scripts/clientplatform_production_deploy.py" in text
    assert "--recover-unavailable-baseline" in text
    assert "StrictHostKeyChecking=yes" in text
    assert "UserKnownHostsFile=" in text


def test_topology_probe_is_read_only_clientplatform_ssh_contract() -> None:
    text = TOPOLOGY_WORKFLOW.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "metrotherapy" not in lowered
    assert "/github-deploy" not in lowered
    assert "secrets.CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY" in text
    assert "workflow_dispatch:" in text
    assert "repo=/opt/clientplatform" in text
    assert "git for-each-ref --format='%(refname:short)' refs/heads" in text
    assert 'branch_count" != "1"' in text
    assert 'branch_csv" != "main"' in text
    assert 'current_branch" != "main"' in text
    assert "StrictHostKeyChecking=yes" in text
    assert "UserKnownHostsFile=" in text
    assert "ops/clientplatform-server-single-main" in text


def test_github_topology_cleanup_retries_eventually_consistent_branch_reads() -> None:
    text = CLEANUP_WORKFLOW.read_text(encoding="utf-8")

    delete_ref = text.index("github.rest.git.deleteRef")
    verification_loop = text.index("for (let attempt = 1; attempt <= 10; attempt += 1)")
    final_assertion = text.index("Expected exactly one GitHub branch named main")

    assert delete_ref < verification_loop < final_assertion
    assert "GITHUB_BRANCH_VERIFY_ATTEMPT=${attempt}" in text
    assert "setTimeout(resolve, attempt * 500)" in text
    assert "names.length === 1 && names[0] === 'main'" in text
