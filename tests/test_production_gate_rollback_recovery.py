from __future__ import annotations

import shutil
import subprocess
from pathlib import Path



ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy.sh"
PRODUCTION_DEPLOY = ROOT / "scripts" / "clientplatform_production_deploy.py"
WRITE_GUARD = ROOT / "scripts" / "install_runtime_write_guard.sh"


def _run(*command: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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


def test_runtime_write_guard_and_deploy_scripts_have_valid_bash_syntax() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    for path in (DEPLOY, WRITE_GUARD):
        completed = _run(bash, "-n", str(path), cwd=ROOT)
        assert completed.returncode == 0, f"{path}: {completed.stderr}"


def test_root_deploy_defers_recovery_to_canonical_clientplatform_deploy() -> None:
    wrapper = DEPLOY.read_text(encoding="utf-8")
    canonical = PRODUCTION_DEPLOY.read_text(encoding="utf-8")

    assert 'exec python3 "$ROOT/scripts/clientplatform_production_deploy.py" "$@"' in wrapper
    assert "install_runtime_write_guard.sh" not in wrapper
    assert "repair_contaminated_current_release.sh" not in wrapper
    assert "immutable_deploy.sh" not in wrapper

    assert "_wait_for_baseline_readiness" in canonical
    assert "_wait_for_readiness" in canonical
    assert "production_not_ready_before_deploy" in canonical
    assert "_rollback(" in canonical
    assert "CLIENTPLATFORM_PRODUCTION_ROLLBACK_OK" in canonical

    dropin = WRITE_GUARD.read_text(encoding="utf-8")
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in dropin
    assert "Environment=PYTHONPYCACHEPREFIX=$STATE_ROOT/python-cache" in dropin
    assert "Environment=XDG_CACHE_HOME=$STATE_ROOT/xdg-cache" in dropin
    assert "Environment=MPLCONFIGDIR=$STATE_ROOT/matplotlib" in dropin
    assert "Environment=TMPDIR=$STATE_ROOT/tmp" in dropin
    assert "ReadOnlyPaths=$RUNTIME_ROOT" in dropin
