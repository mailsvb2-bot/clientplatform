from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_every_deploy_entrypoint_uses_the_worker_lock() -> None:
    wrapper = _text("deploy.sh")

    acquire = wrapper.index("acquire_deploy_lock\n")
    topology = wrapper.index('bash "$SOURCE_DIR/scripts/check_remote_main_topology.sh"', acquire)
    repair = wrapper.index('bash "$RECOVERY_SCRIPT" repair "$SOURCE_DIR"', topology)

    assert 'LOCK_FILE="${LOCK_FILE:-$SOURCE_DIR/data/deploy/metrotherapy_deploy.lock}"' in wrapper
    assert 'FLOCK_BIN="${FLOCK_BIN:-/usr/bin/flock}"' in wrapper
    assert 'parent_lock="$(readlink -f "/proc/$PPID/fd/9"' in wrapper
    assert 'exec 8<>"$LOCK_FILE"' in wrapper
    assert '"$FLOCK_BIN" -w "$LOCK_WAIT_SECONDS" 8' in wrapper
    assert "DEPLOY_LOCK_HELD=1" in wrapper
    assert acquire < topology < repair


def test_release_compatibility_matches_startup_schema_validation() -> None:
    checker = _text("scripts/check_release_runtime_compatibility.sh")

    init_db = checker.index("init_db()")
    validate = checker.index("validate_all(strict=True)")
    marker = checker.index('print("RELEASE_RUNTIME_COMPATIBILITY_OK")')

    assert 'METRO_DATA_DIR="$STATE_ROOT/data"' in checker
    assert 'METRO_LOGS_DIR="$STATE_ROOT/logs"' in checker
    assert init_db < validate < marker


def test_recovery_does_not_treat_tree_integrity_as_runtime_readiness() -> None:
    recovery = _text("scripts/repair_contaminated_current_release.sh")
    valid_current = recovery.index('if validate_release "$current_path"; then')
    compatible_current = recovery.index('if runtime_compatible_release "$current_path"; then', valid_current)
    stale_marker = recovery.index("CURRENT_RELEASE_MARKER_STALE", compatible_current)
    previous_rescue = recovery.index("CURRENT_RELEASE_PREVIOUS_RESCUED")
    recorded_rebuild = recovery.index("CURRENT_RELEASE_ROLLBACK_REBUILT")

    assert valid_current < compatible_current < stale_marker
    assert previous_rescue < recorded_rebuild
    assert "no runtime-compatible recovery target" in recovery


def test_changed_shell_scripts_have_valid_syntax() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    for relative in (
        "deploy.sh",
        "scripts/check_release_runtime_compatibility.sh",
        "scripts/repair_contaminated_current_release.sh",
    ):
        completed = subprocess.run(
            [bash, "-n", str(ROOT / relative)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, f"{relative}: {completed.stderr}"
