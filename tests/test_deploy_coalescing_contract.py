from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_LAUNCHER = ROOT / "deploy.sh"
IMMUTABLE_DEPLOY = ROOT / "scripts" / "immutable_deploy.sh"
RELEASE_MANAGER = ROOT / "scripts" / "immutable_release.py"
RELEASE_BUILDER = ROOT / "scripts" / "build_immutable_release.sh"
CURRENT_RELEASE_RECOVERY = ROOT / "scripts" / "repair_contaminated_current_release.sh"
CANDIDATE_PREPARER = ROOT / "scripts" / "prepare_immutable_candidate.sh"
REMOTE_TOPOLOGY = ROOT / "scripts" / "check_remote_main_topology.sh"


def _run(*command: str, cwd: Path) -> str:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return completed.stdout.strip()


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _run("git", "add", filename, cwd=repo)
    _run("git", "commit", "-m", message, cwd=repo)
    return _run("git", "rev-parse", "HEAD", cwd=repo)


def _assert_bash_syntax(path: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None
    completed = subprocess.run(
        [bash, "-n", str(path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def _prepare_deploy_fixture(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "-b", "main", cwd=repo)
    _run("git", "config", "user.name", "Deploy Contract", cwd=repo)
    _run("git", "config", "user.email", "deploy-contract@example.test", cwd=repo)

    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copy2(RELEASE_MANAGER, scripts / RELEASE_MANAGER.name)
    shutil.copy2(RELEASE_BUILDER, scripts / RELEASE_BUILDER.name)
    _run("git", "add", "scripts", cwd=repo)
    _run("git", "commit", "-m", "fixture release tooling", cwd=repo)

    copied_script = tmp_path / "immutable_deploy.sh"
    shutil.copy2(IMMUTABLE_DEPLOY, copied_script)
    return repo, copied_script


def test_deploy_launcher_and_immutable_pipeline_have_valid_bash_syntax() -> None:
    _assert_bash_syntax(DEPLOY_LAUNCHER)
    _assert_bash_syntax(IMMUTABLE_DEPLOY)
    _assert_bash_syntax(RELEASE_BUILDER)
    _assert_bash_syntax(CURRENT_RELEASE_RECOVERY)
    _assert_bash_syntax(CANDIDATE_PREPARER)
    _assert_bash_syntax(REMOTE_TOPOLOGY)


def test_deploy_launcher_delegates_only_to_clientplatform_production_deploy() -> None:
    source = DEPLOY_LAUNCHER.read_text(encoding="utf-8")

    assert 'exec python3 "$ROOT/scripts/clientplatform_production_deploy.py" "$@"' in source
    assert "run_deploy_worker.sh" not in source
    assert "recover_stale_deploy_worker.sh" not in source
    assert "immutable_deploy.sh" not in source
    assert "check_remote_main_topology.sh" not in source
    assert "git reset --hard" not in source
    assert "pip install" not in source


def test_older_trigger_is_coalesced_before_release_build_or_switch(tmp_path) -> None:
    bash = shutil.which("bash")
    git = shutil.which("git")
    assert bash is not None
    assert git is not None

    repo, copied_script = _prepare_deploy_fixture(tmp_path)
    trigger_sha = _commit(repo, "one.txt", "one\n", "one")
    deployed_sha = _commit(repo, "two.txt", "two\n", "two")
    _run("git", "remote", "add", "origin", str(repo), cwd=repo)

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    marker = state_dir / "deployed_sha"
    marker.write_text(f"{deployed_sha}\n", encoding="utf-8")
    runtime_root = tmp_path / "runtime"

    env = os.environ.copy()
    env.update(
        {
            "APP_DIR": str(repo),
            "DEPLOY_TRIGGER_SHA": trigger_sha,
            "DEPLOY_STATE_DIR": str(state_dir),
            "DEPLOYED_SHA_FILE": str(marker),
            "METRO_RUNTIME_ROOT": str(runtime_root),
            "METRO_RELEASES_DIR": str(runtime_root / "releases"),
            "METRO_CURRENT_RELEASE_LINK": str(runtime_root / "current"),
            "METRO_PREVIOUS_RELEASE_LINK": str(runtime_root / "previous"),
            "METRO_IMMUTABLE_SYSTEMD_OVERRIDE": str(tmp_path / "immutable-release.conf"),
        }
    )
    completed = subprocess.run(
        [bash, str(copied_script)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert f"deploy coalesced trigger={trigger_sha} deployed={deployed_sha}" in completed.stdout
    assert "build immutable release" not in completed.stdout
    assert "mandatory production backup" not in completed.stdout
