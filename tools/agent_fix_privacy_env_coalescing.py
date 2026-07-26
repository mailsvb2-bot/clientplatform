from __future__ import annotations

import subprocess
from pathlib import Path

path = Path("scripts/immutable_deploy.sh")
source = path.read_text(encoding="utf-8")
old = '''run_bounded "$GIT_NETWORK_TIMEOUT_SECONDS" "fetch origin" git fetch --prune origin
git merge --ff-only origin/main
migrate_privacy_export_environment
require_single_local_main_branch
NEW_SHA="$(git rev-parse HEAD)"
echo "=== immutable deploy source old=$OLD_SOURCE_SHA new=$NEW_SHA ==="
'''
new = '''run_bounded "$GIT_NETWORK_TIMEOUT_SECONDS" "fetch origin" git fetch --prune origin
git merge --ff-only origin/main
require_single_local_main_branch
NEW_SHA="$(git rev-parse HEAD)"
echo "=== immutable deploy source old=$OLD_SOURCE_SHA new=$NEW_SHA ==="
'''
if source.count(old) != 1:
    raise SystemExit("deploy pre-coalescing anchor mismatch")
source = source.replace(old, new, 1)
old = '''fi

mkdir -p "$RUNTIME_ROOT" "$RELEASES_DIR" "$DEPLOY_STATE_DIR"
'''
new = '''fi

migrate_privacy_export_environment
mkdir -p "$RUNTIME_ROOT" "$RELEASES_DIR" "$DEPLOY_STATE_DIR"
'''
if source.count(old) != 1:
    raise SystemExit("deploy post-coalescing anchor mismatch")
path.write_text(source.replace(old, new, 1), encoding="utf-8")

subprocess.run(["bash", "-n", str(path)], check=True)
subprocess.run(
    [
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/test_deploy_coalescing_contract.py",
        "tests/test_privacy_export_env_rollout.py",
    ],
    check=True,
)
