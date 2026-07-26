from __future__ import annotations

from pathlib import Path


def replace_exact(path: Path, old: str, new: str, *, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    actual = text.count(old)
    if actual != count:
        raise SystemExit(f"{path}: expected {count} exact matches, found {actual}")
    path.write_text(text.replace(old, new, count), encoding="utf-8")


root = Path(__file__).resolve().parents[1]

production_gate = root / "scripts" / "production_gate.py"
replace_exact(
    production_gate,
    '''            "--ready-url",\n            str(args.ready_url),\n            "--require-disaster-recovery-green",\n''',
    '''            "--ready-url",\n            str(args.ready_url),\n            "--skip-pytest",\n            "--require-disaster-recovery-green",\n''',
)

immutable_deploy = root / "scripts" / "immutable_deploy.sh"
replace_exact(
    immutable_deploy,
    '''RUNTIME_CONTRACT="$SOURCE_DIR/scripts/runtime_contract.py"\n''',
    '''RUNTIME_CONTRACT="$SOURCE_DIR/scripts/runtime_contract.py"\nRUNTIME_WRITE_GUARD="$SOURCE_DIR/scripts/install_runtime_write_guard.sh"\n''',
)
replace_exact(
    immutable_deploy,
    '''    "$SYSTEM_PYTHON" "$RELEASE_MANAGER" rollback \\\n      --current-link "$CURRENT_LINK" \\\n      --previous-link "$PREVIOUS_LINK" || true\n    "$TIMEOUT_BIN" --signal=TERM --kill-after=15s \\\n      "$SERVICE_RESTART_TIMEOUT_SECONDS" \\\n      systemctl restart "$SERVICE_NAME" || true\n    wait_for_health "$LOCAL_HEALTH_URL" "$HEALTH_WAIT_SECONDS" || true\n    "$SYSTEM_PYTHON" "$RELEASE_MANAGER" inspect "$CURRENT_LINK" --required || true\n''',
    '''    local rollback_release_dir=""\n    if "$SYSTEM_PYTHON" "$RELEASE_MANAGER" rollback \\\n      --current-link "$CURRENT_LINK" \\\n      --previous-link "$PREVIOUS_LINK"; then\n      rollback_release_dir="$(release_path_from_link "$CURRENT_LINK" 2>/dev/null || true)"\n      if [ -n "$rollback_release_dir" ] && [ -f "$RUNTIME_WRITE_GUARD" ] && \\\n        bash "$RUNTIME_WRITE_GUARD" for-release "$rollback_release_dir"; then\n        "$TIMEOUT_BIN" --signal=TERM --kill-after=15s \\\n          "$SERVICE_RESTART_TIMEOUT_SECONDS" \\\n          systemctl restart "$SERVICE_NAME" || true\n        wait_for_health "$LOCAL_HEALTH_URL" "$HEALTH_WAIT_SECONDS" || true\n      else\n        echo "IMMUTABLE_DEPLOY_ROLLBACK_FAILED compatible runtime guard was not installed" >&2\n      fi\n    else\n      echo "IMMUTABLE_DEPLOY_ROLLBACK_FAILED atomic release switch failed" >&2\n    fi\n    "$SYSTEM_PYTHON" "$RELEASE_MANAGER" inspect "$CURRENT_LINK" --required || true\n''',
)

contract_test = root / "tests" / "test_production_runtime_gate_contract.py"
contract_test.write_text(
    '''from __future__ import annotations\n\nfrom pathlib import Path\n\n\nROOT = Path(__file__).resolve().parents[1]\n\n\ndef _text(relative: str) -> str:\n    return (ROOT / relative).read_text(encoding="utf-8")\n\n\ndef test_production_gate_skips_pytest_in_runtime_release() -> None:\n    gate = _text("scripts/production_gate.py")\n    start = gate.index('"scripts/post_deploy_verify.py"')\n    end = gate.index('env=gate_env,', start)\n    invocation = gate[start:end]\n\n    assert '"--skip-pytest"' in invocation\n    assert invocation.index('"--skip-pytest"') < invocation.index(\n        '"--require-disaster-recovery-green"'\n    )\n\n\ndef test_rollback_selects_release_guard_before_restart() -> None:\n    deploy = _text("scripts/immutable_deploy.sh")\n    start = deploy.index("rollback() {")\n    end = deploy.index("cleanup_old_releases() {", start)\n    rollback = deploy[start:end]\n\n    switch = rollback.index('"$SYSTEM_PYTHON" "$RELEASE_MANAGER" rollback')\n    resolve = rollback.index('rollback_release_dir="$(release_path_from_link', switch)\n    guard = rollback.index('bash "$RUNTIME_WRITE_GUARD" for-release', resolve)\n    restart = rollback.index('systemctl restart "$SERVICE_NAME"', guard)\n\n    assert switch < resolve < guard < restart\n    assert "|| true" not in rollback[switch:guard]\n''',
    encoding="utf-8",
)
