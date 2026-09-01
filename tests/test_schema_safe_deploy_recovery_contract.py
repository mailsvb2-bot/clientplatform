from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_root_entrypoint_has_one_canonical_deploy_lock_owner() -> None:
    wrapper = _text("deploy.sh")
    deploy = _text("scripts/clientplatform_production_deploy.py")

    assert 'exec python3 "$ROOT/scripts/clientplatform_production_deploy.py" "$@"' in wrapper
    assert "acquire_deploy_lock" not in wrapper
    assert "run_deploy_worker.sh" not in wrapper

    main = deploy.index("def main() -> int:")
    open_lock = deploy.index('with LOCK_PATH.open("a+") as lock_handle:', main)
    flock = deploy.index(
        "fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)",
        open_lock,
    )
    deploy_call = deploy.index("            deploy(\n", flock)

    assert 'LOCK_PATH = Path("/run/lock/clientplatform-production-deploy.lock")' in deploy
    assert open_lock < flock < deploy_call


def test_release_compatibility_matches_startup_schema_validation() -> None:
    checker = _text("scripts/check_release_runtime_compatibility.sh")

    init_db = checker.index("init_db()")
    validate = checker.index("validate_all(strict=True)")
    marker = checker.index('print("RELEASE_RUNTIME_COMPATIBILITY_OK")')

    assert 'CLIENTPLATFORM_DATA_DIR="$STATE_ROOT/data"' in checker
    assert 'CLIENTPLATFORM_LOGS_DIR="$STATE_ROOT/logs"' in checker
    assert init_db < validate < marker

def test_canonical_deploy_and_compatibility_scripts_have_valid_syntax() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    for relative in (
        "deploy.sh",
        "scripts/check_release_runtime_compatibility.sh",
    ):
        completed = subprocess.run(
            [bash, "-n", str(ROOT / relative)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, f"{relative}: {completed.stderr}"
