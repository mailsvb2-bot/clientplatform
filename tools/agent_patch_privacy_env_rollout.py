from __future__ import annotations

import subprocess
from pathlib import Path


def replace_exact(path: str, old: str, new: str, *, count: int = 1) -> None:
    target = Path(path)
    source = target.read_text(encoding="utf-8")
    actual = source.count(old)
    if actual != count:
        raise SystemExit(f"{path}: expected {count} exact match(es), found {actual}")
    target.write_text(source.replace(old, new, count), encoding="utf-8")


replace_exact(
    "scripts/migrate_privacy_export_env.py",
    '''        current_metadata = path.stat()
        if not stat.S_ISREG(current_metadata.st_mode):
            raise MigrationError("environment file is no longer regular")
        original = path.read_bytes()
''',
    '''        current_metadata = path.stat()
        if not stat.S_ISREG(current_metadata.st_mode):
            raise MigrationError("environment file is no longer regular")
        current_mode = stat.S_IMODE(current_metadata.st_mode)
        if current_mode & stat.S_IWOTH:
            raise MigrationError("environment file became world-writable")
        original = path.read_bytes()
''',
)
replace_exact(
    "scripts/migrate_privacy_export_env.py",
    '''        backup = _backup_path(path)
        _write_exclusive_file(
            backup,
            original,
            mode=mode,
            uid=current_metadata.st_uid,
            gid=current_metadata.st_gid,
        )
        _atomic_replace(
            path,
            updated,
            mode=mode,
            uid=current_metadata.st_uid,
            gid=current_metadata.st_gid,
        )

        verified_text = path.read_text(encoding="utf-8")
        verified_assignments = _active_assignments(verified_text.splitlines(keepends=True))
        for key, expected in targets.items():
            actual = verified_assignments.get(key, (-1, ""))[1]
            if actual != expected:
                raise MigrationError(f"post-write verification failed for {key}")
        return MigrationResult(
''',
    '''        backup = _backup_path(path)
        _write_exclusive_file(
            backup,
            original,
            mode=current_mode,
            uid=current_metadata.st_uid,
            gid=current_metadata.st_gid,
        )
        try:
            _atomic_replace(
                path,
                updated,
                mode=current_mode,
                uid=current_metadata.st_uid,
                gid=current_metadata.st_gid,
            )

            verified_text = path.read_text(encoding="utf-8")
            verified_assignments = _active_assignments(verified_text.splitlines(keepends=True))
            for key, expected in targets.items():
                actual = verified_assignments.get(key, (-1, ""))[1]
                if actual != expected:
                    raise MigrationError(f"post-write verification failed for {key}")
        except (MigrationError, OSError, UnicodeError) as exc:
            try:
                _atomic_replace(
                    path,
                    original,
                    mode=current_mode,
                    uid=current_metadata.st_uid,
                    gid=current_metadata.st_gid,
                )
            except OSError as rollback_exc:
                raise MigrationError(
                    "environment migration failed and automatic rollback also failed"
                ) from rollback_exc
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError("post-write verification failed") from exc
        return MigrationResult(
''',
)
replace_exact(
    "scripts/immutable_deploy.sh",
    '''RELEASE_MANAGER="$SOURCE_DIR/scripts/immutable_release.py"
RELEASE_BUILDER="$SOURCE_DIR/scripts/build_immutable_release.sh"
LOCAL_HEALTH_URL="${LOCAL_HEALTH_URL:-http://127.0.0.1:8082/healthz}"
''',
    '''RELEASE_MANAGER="$SOURCE_DIR/scripts/immutable_release.py"
RELEASE_BUILDER="$SOURCE_DIR/scripts/build_immutable_release.sh"
ENV_MIGRATOR="$SOURCE_DIR/scripts/migrate_privacy_export_env.py"
RUNTIME_CONTRACT="$SOURCE_DIR/scripts/runtime_contract.py"
PRIVACY_EXPORT_DEFAULT_PUBLIC_BASE_URL="${PRIVACY_EXPORT_DEFAULT_PUBLIC_BASE_URL:-https://metrotherapy-bot.metrotherapy.ru}"
PRIVACY_EXPORT_DEFAULT_TTL_MINUTES="${PRIVACY_EXPORT_DEFAULT_TTL_MINUTES:-10}"
LOCAL_HEALTH_URL="${LOCAL_HEALTH_URL:-http://127.0.0.1:8082/healthz}"
''',
)
replace_exact(
    "scripts/immutable_deploy.sh",
    '''  echo "IMMUTABLE_DEPLOY_FAILED command=$label code=$code" >&2
  return "$code"
}

wait_for_health() {
''',
    '''  echo "IMMUTABLE_DEPLOY_FAILED command=$label code=$code" >&2
  return "$code"
}

reload_authoritative_environment() {
  [ -f "$ENV_FILE" ] || {
    echo "IMMUTABLE_DEPLOY_FAILED authoritative env file not found: $ENV_FILE" >&2
    return 1
  }
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
}

migrate_privacy_export_environment() {
  [ -f "$ENV_MIGRATOR" ] || {
    echo "IMMUTABLE_DEPLOY_FAILED privacy export env migrator is missing" >&2
    return 1
  }
  [ -f "$RUNTIME_CONTRACT" ] || {
    echo "IMMUTABLE_DEPLOY_FAILED runtime contract checker is missing" >&2
    return 1
  }
  run_bounded "$VALIDATOR_TIMEOUT_SECONDS" \
    "migrate authoritative privacy export environment" \
    "$SYSTEM_PYTHON" "$ENV_MIGRATOR" \
      --env-file "$ENV_FILE" \
      --public-base-url "$PRIVACY_EXPORT_DEFAULT_PUBLIC_BASE_URL" \
      --ttl-minutes "$PRIVACY_EXPORT_DEFAULT_TTL_MINUTES"
  reload_authoritative_environment
  run_bounded "$VALIDATOR_TIMEOUT_SECONDS" \
    "validate production runtime contract after env migration" \
    env PYTHONDONTWRITEBYTECODE=1 "$SYSTEM_PYTHON" "$RUNTIME_CONTRACT"
}

wait_for_health() {
''',
)
replace_exact(
    "scripts/immutable_deploy.sh",
    '''run_bounded "$GIT_NETWORK_TIMEOUT_SECONDS" "fetch origin" git fetch --prune origin
git merge --ff-only origin/main
require_single_local_main_branch
''',
    '''run_bounded "$GIT_NETWORK_TIMEOUT_SECONDS" "fetch origin" git fetch --prune origin
git merge --ff-only origin/main
migrate_privacy_export_environment
require_single_local_main_branch
''',
)
replace_exact(
    "deploy/RUNTIME_CONTRACT.md",
    '''The live `/etc/metrotherapy/metrotherapy.env` file is authoritative and is not
replaced by immutable deploys. Before rollout, add the privacy export variables
above to that server-side file; otherwise production readiness must fail closed.
''',
    '''The live `/etc/metrotherapy/metrotherapy.env` file is authoritative and is not
replaced by immutable deploys. Prepare the first rollout without manually editing
secrets:

```bash
cd /root/metrotherapy
git fetch --prune origin
git checkout main
git merge --ff-only origin/main
sudo bash scripts/prepare_privacy_export_rollout.sh
```

The helper takes an exclusive lock, preserves all unrelated bytes, writes a
 timestamped backup, atomically updates only the three privacy-export keys, and
runs `runtime_contract.py` without restarting the service. Later immutable
deploys repeat this idempotent migration automatically after the fast-forward and
before candidate build or runtime switching.
'''.replace("\n timestamped", "\ntimestamped"),
)
replace_exact(
    "docs/IMMUTABLE_RELEASE_DEPLOYMENT.md",
    '''The restore script refuses the production URL, the same database name, system databases, and targets without a drill/test marker.

## Evidence
''',
    '''The restore script refuses the production URL, the same database name, system databases, and targets without a drill/test marker.

### First privacy-export rollout

The authoritative env file is migrated with:

```bash
cd /root/metrotherapy
git fetch --prune origin
git checkout main
git merge --ff-only origin/main
sudo bash scripts/prepare_privacy_export_rollout.sh
```

This command does not restart the service. It creates a timestamped backup,
atomically adds or repairs only `PRIVACY_EXPORT_HTTP_ENABLED`,
`PRIVACY_EXPORT_PUBLIC_BASE_URL`, and `PRIVACY_EXPORT_TOKEN_TTL_MINUTES`, then
runs the offline production runtime contract. Existing valid custom URL and TTL
values are retained. Duplicate managed keys, symlinks, world-writable env files,
and invalid HTTPS/TTL fallbacks fail closed before deployment.

After the first preparation, `scripts/immutable_deploy.sh` repeats the migration
idempotently after every fast-forward and before building or switching a release.

## Evidence
''',
)

subprocess.run(
    [
        "python",
        "-m",
        "py_compile",
        "scripts/migrate_privacy_export_env.py",
    ],
    check=True,
)
subprocess.run(["bash", "-n", "scripts/prepare_privacy_export_rollout.sh"], check=True)
subprocess.run(["bash", "-n", "scripts/immutable_deploy.sh"], check=True)
subprocess.run(
    [
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/test_privacy_export_env_rollout.py",
        "tests/test_runtime_contract.py",
        "tests/test_prod_readiness_messenger_env.py",
        "tests/test_coverage_ratchet_gate.py",
    ],
    check=True,
)
