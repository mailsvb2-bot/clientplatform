from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "production-deploy-recovery.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_production_recovery_sources_exact_commit_without_server_github_credentials() -> None:
    workflow = _workflow()

    assert "persist-credentials: false" in workflow
    assert "fetch-depth: 0" in workflow
    assert 'checkout_sha="$(git rev-parse HEAD)"' in workflow
    assert 'git bundle create "$source_bundle" "$source_ref"' in workflow
    assert 'git bundle verify "$source_bundle"' in workflow
    assert 'bundle_sha="$(git bundle list-heads "$source_bundle" "$source_ref"' in workflow
    assert 'if [ "$bundle_sha" != "$TRIGGER_SHA" ]; then' in workflow

    # Production receives the already authenticated runner's exact Git object
    # bundle over the pinned SSH channel; it must not need GitHub credentials.
    assert 'cat > \'$remote_bundle\'' in workflow
    assert 'git fetch "$source_bundle" "$source_ref:refs/remotes/origin/main"' in workflow
    assert "git fetch --prune origin main" not in workflow
    assert "git fetch origin" not in workflow
    assert "GITHUB_TOKEN" not in workflow


def test_production_recovery_bundle_is_fail_closed_and_ephemeral() -> None:
    workflow = _workflow()

    assert 'expected_bundle="/tmp/clientplatform-recovery-$expected_sha.bundle"' in workflow
    expected_bundle_guard = (
        'if [ "$source_bundle" != "$expected_bundle" ] || '
        '[ ! -s "$source_bundle" ]; then'
    )
    assert expected_bundle_guard in workflow
    assert "trap 'rm -f -- \"$source_bundle\"' EXIT" in workflow
    assert 'if [ "$bundle_sha" != "$expected_sha" ]; then' in workflow
    assert 'if [ "$fetched_sha" != "$expected_sha" ]; then' in workflow
    assert "git merge --ff-only origin/main" in workflow
    assert 'if [ "$deployed_source_sha" != "$expected_sha" ]; then' in workflow


def test_production_recovery_keeps_origin_and_worktree_guards() -> None:
    workflow = _workflow()

    assert 'origin="$(git remote get-url origin 2>/dev/null || true)"' in workflow
    assert "https://github.com/mailsvb2-bot/clientplatform.git" in workflow
    assert 'if [ "$current_branch" != "main" ]; then' in workflow
    assert "git status --porcelain=v1 --untracked-files=no --ignore-submodules=none" in workflow
    assert "scripts/clientplatform_production_deploy.py" in workflow
    assert "--recover-unavailable-baseline" in workflow
