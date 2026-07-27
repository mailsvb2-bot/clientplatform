from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECOVERY = ROOT / "scripts" / "repair_contaminated_current_release.sh"


def _run(
    *command: str,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _git(repo: Path, *args: str) -> str:
    completed = _run("git", *args, cwd=repo)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _commit(repo: Path, name: str, payload: str) -> str:
    (repo / name).write_text(payload, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"commit {name}")
    return _git(repo, "rev-parse", "HEAD")


def _manager(path: Path) -> Path:
    manager = path / "manager.py"
    manager.write_text(
        """
from __future__ import annotations
import json
import sys
from pathlib import Path

command = sys.argv[1]
target = Path(sys.argv[2]).resolve(strict=True)
valid = (target / ".valid").is_file()
if command == "validate":
    raise SystemExit(0 if valid else 1)
if command == "inspect":
    if not valid:
        raise SystemExit(1)
    print(json.dumps({"path": str(target), "sha": target.name}))
    raise SystemExit(0)
raise SystemExit(2)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return manager


def _builder(path: Path) -> Path:
    builder = path / "builder.sh"
    builder.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
target="$RELEASES_DIR/$1"
mkdir -p "$target"
printf 'valid\n' > "$target/.valid"
""",
        encoding="utf-8",
    )
    return builder


def _compatibility(path: Path) -> Path:
    checker = path / "compatibility.sh"
    checker.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
test -f "$1/.valid"
test "$(basename "$1")" = "$COMPATIBLE_SHA"
""",
        encoding="utf-8",
    )
    return checker


def test_recorded_deployed_sha_is_rebuilt_only_after_current_is_rejected(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    timeout = shutil.which("timeout")
    assert bash and timeout

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Recorded Rollback Test")
    _git(repo, "config", "user.email", "rollback@example.test")
    recorded_sha = _commit(repo, "recorded.txt", "recorded\n")
    failed_sha = _commit(repo, "failed.txt", "failed\n")

    runtime = tmp_path / "runtime"
    releases = runtime / "releases"
    recovery_root = runtime / "recovery-releases"
    deploy_state = tmp_path / "deploy-state"
    releases.mkdir(parents=True)
    deploy_state.mkdir()

    failed_release = releases / failed_sha
    failed_release.mkdir()
    current = runtime / "current"
    current.symlink_to(failed_release)
    deployed_sha_file = deploy_state / "deployed_sha"
    deployed_sha_file.write_text(f"{recorded_sha}\n", encoding="utf-8")

    manager = _manager(tmp_path)
    builder = _builder(tmp_path)
    compatibility = _compatibility(tmp_path)

    env = os.environ.copy()
    env.update(
        {
            "METRO_RUNTIME_ROOT": str(runtime),
            "METRO_RELEASES_DIR": str(releases),
            "METRO_CURRENT_RELEASE_LINK": str(current),
            "METRO_PREVIOUS_RELEASE_LINK": str(runtime / "previous"),
            "DEPLOY_STATE_DIR": str(deploy_state),
            "DEPLOYED_SHA_FILE": str(deployed_sha_file),
            "METRO_RECOVERY_RELEASES_ROOT": str(recovery_root),
            "METRO_RECOVERY_STATE_DIR": str(deploy_state / "contaminated"),
            "SYSTEM_PYTHON": sys.executable,
            "RELEASE_MANAGER": str(manager),
            "RELEASE_BUILDER": str(builder),
            "RELEASE_RUNTIME_COMPATIBILITY_CHECKER": str(compatibility),
            "COMPATIBLE_SHA": recorded_sha,
            "TIMEOUT_BIN": timeout,
            "RELEASE_BUILD_TIMEOUT_SECONDS": "30",
            "RELEASE_COMPAT_TIMEOUT_SECONDS": "30",
        }
    )

    completed = _run(bash, str(RECOVERY), "repair", str(repo), cwd=ROOT, env=env)

    assert completed.returncode == 0, completed.stderr
    assert "runtime-incompatible target" in completed.stderr
    assert "CURRENT_RELEASE_ROLLBACK_REBUILT" in completed.stdout
    assert f"deployed={recorded_sha}" in completed.stdout
    rebuilt = current.resolve(strict=True)
    assert rebuilt.name == recorded_sha
    assert rebuilt.parent.parent == recovery_root
    assert (rebuilt / ".valid").read_text(encoding="utf-8") == "valid\n"

    state_files = sorted((deploy_state / "contaminated").glob("*.state"))
    assert len(state_files) == 1
    state_lines = state_files[0].read_text(encoding="utf-8").splitlines()
    assert state_lines == [failed_sha, str(failed_release)]

    inspected = _run(
        sys.executable,
        str(manager),
        "inspect",
        str(current),
        "--required",
        cwd=ROOT,
    )
    assert inspected.returncode == 0
    assert json.loads(inspected.stdout)["sha"] == recorded_sha


def test_valid_runtime_is_not_downgraded_by_stale_deployed_marker(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    timeout = shutil.which("timeout")
    assert bash and timeout

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Stale Marker Test")
    _git(repo, "config", "user.email", "stale@example.test")
    recorded_sha = _commit(repo, "recorded.txt", "recorded\n")
    current_sha = _commit(repo, "current.txt", "current\n")

    runtime = tmp_path / "runtime"
    releases = runtime / "releases"
    deploy_state = tmp_path / "deploy-state"
    releases.mkdir(parents=True)
    deploy_state.mkdir()

    current_release = releases / current_sha
    current_release.mkdir()
    (current_release / ".valid").write_text("valid\n", encoding="utf-8")
    current = runtime / "current"
    current.symlink_to(current_release)
    deployed_sha_file = deploy_state / "deployed_sha"
    deployed_sha_file.write_text(f"{recorded_sha}\n", encoding="utf-8")

    manager = _manager(tmp_path)
    builder = _builder(tmp_path)
    compatibility = _compatibility(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "METRO_RUNTIME_ROOT": str(runtime),
            "METRO_RELEASES_DIR": str(releases),
            "METRO_CURRENT_RELEASE_LINK": str(current),
            "METRO_PREVIOUS_RELEASE_LINK": str(runtime / "previous"),
            "DEPLOY_STATE_DIR": str(deploy_state),
            "DEPLOYED_SHA_FILE": str(deployed_sha_file),
            "METRO_RECOVERY_RELEASES_ROOT": str(runtime / "recovery-releases"),
            "SYSTEM_PYTHON": sys.executable,
            "RELEASE_MANAGER": str(manager),
            "RELEASE_BUILDER": str(builder),
            "RELEASE_RUNTIME_COMPATIBILITY_CHECKER": str(compatibility),
            "COMPATIBLE_SHA": current_sha,
            "TIMEOUT_BIN": timeout,
            "RELEASE_BUILD_TIMEOUT_SECONDS": "30",
            "RELEASE_COMPAT_TIMEOUT_SECONDS": "30",
        }
    )

    completed = _run(bash, str(RECOVERY), "repair", str(repo), cwd=ROOT, env=env)

    assert completed.returncode == 0, completed.stderr
    assert f"CURRENT_RELEASE_MARKER_STALE current={current_sha} recorded={recorded_sha}" in completed.stdout
    assert "CURRENT_RELEASE_INTEGRITY_OK" in completed.stdout
    assert current.resolve(strict=True) == current_release
    assert not (runtime / "recovery-releases").exists()
    assert deployed_sha_file.read_text(encoding="utf-8") == f"{recorded_sha}\n"


def test_recovery_script_has_valid_bash_syntax() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    completed = _run(bash, "-n", str(RECOVERY), cwd=ROOT)
    assert completed.returncode == 0, completed.stderr
