from __future__ import annotations

from pathlib import Path

from scripts.check_deploy_governance import deploy_governance_problems
from scripts.check_release_hygiene import ALLOWED_ROOT_FILES

ROOT = Path(__file__).resolve().parents[1]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_deploy_wrapper_has_no_mutable_runtime_logic() -> None:
    wrapper = _text("deploy.sh")

    assert "scripts/clientplatform_production_deploy.py" in wrapper
    assert "exec python3" in wrapper
    assert "scripts/immutable_deploy.sh" not in wrapper
    assert "METROTHERAPY_ENV_FILE" not in wrapper
    assert "metrotherapy.service" not in wrapper
    assert "/root/metrotherapy" not in wrapper
    assert "/etc/metrotherapy" not in wrapper
    assert "git reset" not in wrapper
    assert "git fetch" not in wrapper
    assert "git merge" not in wrapper
    assert "pip install" not in wrapper


def test_immutable_deploy_governance_is_closed() -> None:
    assert deploy_governance_problems() == []


def test_builder_creates_per_sha_hash_locked_release() -> None:
    builder = _text("scripts/build_immutable_release.sh")

    assert 'FINAL_DIR="$RELEASES_DIR/$SHA"' in builder
    assert 'git -C "$SOURCE_DIR" archive --format=tar "$SHA"' in builder
    assert '"$SYSTEM_PYTHON" -m venv "$BUILD_DIR/.venv"' in builder
    assert "--no-compile --require-hashes" in builder
    assert 'tree-digest "$BUILD_DIR"' in builder
    assert 'chmod 0755 "$BUILD_DIR"' in builder
    assert "not path.is_symlink()" in builder
    assert 'mv "$BUILD_DIR" "$FINAL_DIR"' in builder
    assert '"$SYSTEM_PYTHON" "$MANAGER" validate "$FINAL_DIR"' in builder
    assert 'rm -rf "$BUILD_DIR"' in builder


def test_builder_publishes_content_addressed_audio_assets() -> None:
    builder = _text("scripts/build_immutable_release.sh")

    assert 'AUDIO_RELEASES_DIR="${AUDIO_RELEASES_DIR:-$(dirname "$RUNTIME_ROOT")/audio-releases}"' in builder
    assert 'AUDIO_BUILD_DIR="$(mktemp -d "$AUDIO_RELEASES_DIR/.build-audio.XXXXXX")"' in builder
    assert 'cp -a "$AUDIO_SOURCE_DIR/." "$AUDIO_BUILD_DIR/"' in builder
    assert '"$SYSTEM_PYTHON" "$AUDIO_MANAGER" seal "$AUDIO_BUILD_DIR"' in builder
    assert 'AUDIO_ASSET_DIR="$AUDIO_RELEASES_DIR/$AUDIO_ASSET_SHA256"' in builder
    assert 'ln -s "$AUDIO_ASSET_DIR" "$BUILD_DIR/audio"' in builder
    assert "write-release-pointer" in builder
    assert 'chmod 0750 "$AUDIO_RELEASES_DIR"' in builder
    assert 'chmod -R a+rX' not in builder
    assert 'cp -a "$SOURCE_DIR/audio/." "$SHARED_AUDIO_DIR/"' not in builder


def test_immutable_deploy_orders_switch_gate_proof_and_marker() -> None:
    deploy = _text("scripts/immutable_deploy.sh")
    pipeline = deploy.index("sync_deploy_webhook_service\n")
    expand = deploy.index('validate_candidate_and_expand_schema "$CANDIDATE_DIR"', pipeline)
    compatibility = deploy.index(
        'verify_previous_release_on_expanded_schema "$CURRENT_RELEASE_DIR"',
        expand,
    )
    switch = deploy.index('if [ "$NEW_SHA" != "$OLD_RUNTIME_SHA" ]', compatibility)
    restart = deploy.index("restart_runtime_and_wait", switch)
    gate = deploy.index('"$CURRENT_LINK/scripts/production_gate.py"', restart)
    proof = deploy.index('"$SYSTEM_PYTHON" "$RELEASE_MANAGER" write-proof', gate)
    marker = deploy.index('record_successful_deployed_sha "$NEW_SHA"', proof)

    assert expand < compatibility < switch < restart < gate < proof < marker
    assert "--require-hashes" not in deploy
    assert "post_deploy_verify.py --skip-pytest" not in deploy


def test_rollback_switches_symlink_before_restart() -> None:
    deploy = _text("scripts/immutable_deploy.sh")
    rollback = deploy.index('"$SYSTEM_PYTHON" "$RELEASE_MANAGER" rollback')
    restart = deploy.index('systemctl restart "$SERVICE_NAME"', rollback)

    assert rollback < restart
    assert "git reset --hard" not in deploy
    assert 'PREVIOUS_LINK="${METRO_PREVIOUS_RELEASE_LINK:-$RUNTIME_ROOT/previous}"' in deploy


def test_previous_release_is_checked_after_expand_migrations() -> None:
    deploy = _text("scripts/immutable_deploy.sh")
    pipeline = deploy.index("sync_deploy_webhook_service\n")
    expand = deploy.index('validate_candidate_and_expand_schema "$CANDIDATE_DIR"', pipeline)
    compatibility = deploy.index(
        'verify_previous_release_on_expanded_schema "$CURRENT_RELEASE_DIR"',
        expand,
    )
    switch = deploy.index('if [ "$NEW_SHA" != "$OLD_RUNTIME_SHA" ]', compatibility)

    assert expand < compatibility < switch
    assert "PREVIOUS_RELEASE_EXPANDED_SCHEMA_OK" in deploy


def test_deployed_sha_is_not_written_by_preflight_or_rollback() -> None:
    deploy = _text("scripts/immutable_deploy.sh")

    assert deploy.count('record_successful_deployed_sha "$NEW_SHA"') == 1
    call = deploy.index('record_successful_deployed_sha "$NEW_SHA"')
    assert deploy.index("PRODUCTION_GATE_OK") < call
    rollback_region = deploy[deploy.index("rollback() {") : deploy.index("cleanup_old_releases() {")]
    assert "record_successful_deployed_sha" not in rollback_region


def test_clientplatform_systemd_template_executes_only_canonical_runtime() -> None:
    service = _text("deploy/clientplatform/clientplatform.service")

    assert "WorkingDirectory=/var/lib/clientplatform/runtime/current" in service
    assert "EnvironmentFile=/etc/clientplatform/clientplatform.env" in service
    assert (
        "ExecStart=/var/lib/clientplatform/runtime/current/.venv/bin/python main.py"
        in service
    )
    assert "scripts/clientplatform_production_preflight.py" in service
    assert "PYTHONDONTWRITEBYTECODE=1" in service
    assert "metrotherapy.service" not in service
    assert "/var/lib/metrotherapy" not in service
    assert "/root/metrotherapy" not in service


def test_release_marker_is_a_canonical_hygiene_artifact() -> None:
    assert ".release.json" in ALLOWED_ROOT_FILES


def test_remote_topology_gate_is_read_only_and_exact() -> None:
    topology = _text("scripts/check_remote_main_topology.sh")

    assert "ls-remote --heads origin" in topology
    assert 'branches" != "main"' in topology
    assert "REMOTE_TOPOLOGY_OK" in topology
    assert "git push" not in topology
    assert "deleteRef" not in topology


def test_candidate_validator_runs_with_release_guardrails() -> None:
    deploy = _text("scripts/immutable_deploy.sh")
    start = deploy.index("validate_candidate_and_expand_schema() {")
    end = deploy.index("verify_previous_release_on_expanded_schema() {", start)
    candidate = deploy[start:end]

    assert "VALIDATOR_RELEASE_MODE=1" in candidate
    assert "VALIDATOR_RELEASE_MODE=0" not in candidate
    assert "VALIDATOR_STRICT=1" in candidate
    assert "VALIDATOR_GUARDRAILS_STRICT=1" in candidate


def test_immutable_systemd_dropin_wins_over_legacy_override() -> None:
    deploy = _text("scripts/immutable_deploy.sh")

    assert "$SERVICE_NAME.d/zz-immutable-release.conf" in deploy
    assert "$SERVICE_NAME.d/immutable-release.conf" not in deploy
    assert "verify_immutable_systemd_effective_config" in deploy
    install = deploy.index('SYSTEMD_CHANGED="$(install_immutable_systemd_override)"')
    verify = deploy.index("verify_immutable_systemd_effective_config", install)
    restart = deploy.index("restart_runtime_and_wait", verify)
    assert install < verify < restart


def test_canonical_deploy_owns_lock_readiness_and_rollback() -> None:
    deploy = _text("scripts/clientplatform_production_deploy.py")

    lock = deploy.index("fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)")
    backup = deploy.index("backup_reference =")
    change = deploy.index("changed = False", backup)
    readiness = deploy.index("_wait_for_readiness(timeout_seconds)", change)
    evidence = deploy.index("CLIENTPLATFORM_PRODUCTION_DEPLOY_OK", readiness)

    assert lock > 0
    assert backup < change < readiness < evidence
    assert "_rollback(" in deploy
    assert "git push" not in deploy
    assert "git reset --hard" not in deploy
