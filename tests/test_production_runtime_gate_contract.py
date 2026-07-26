from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_production_gate_skips_pytest_in_runtime_release() -> None:
    gate = _text("scripts/production_gate.py")
    start = gate.index('"scripts/post_deploy_verify.py"')
    end = gate.index('env=gate_env,', start)
    invocation = gate[start:end]

    assert '"--skip-pytest"' in invocation
    assert invocation.index('"--skip-pytest"') < invocation.index(
        '"--require-disaster-recovery-green"'
    )


def test_rollback_selects_release_guard_before_restart() -> None:
    deploy = _text("scripts/immutable_deploy.sh")
    start = deploy.index("rollback() {")
    end = deploy.index("cleanup_old_releases() {", start)
    rollback = deploy[start:end]

    switch = rollback.index('"$SYSTEM_PYTHON" "$RELEASE_MANAGER" rollback')
    switch_end = rollback.index('; then', switch)
    resolve = rollback.index('rollback_release_dir="$(release_path_from_link', switch_end)
    guard = rollback.index('bash "$RUNTIME_WRITE_GUARD" for-release', resolve)
    restart = rollback.index('systemctl restart "$SERVICE_NAME"', guard)

    assert switch < switch_end < resolve < guard < restart
    assert "|| true" not in rollback[switch:switch_end]
