from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_SCRIPT = ROOT / "scripts" / "repair_contaminated_current_release.sh"


def test_root_deploy_no_longer_drives_legacy_release_recovery() -> None:
    wrapper = (ROOT / "deploy.sh").read_text(encoding="utf-8")

    assert wrapper.startswith("#!/usr/bin/env bash\n")
    assert 'exec python3 "$ROOT/scripts/clientplatform_production_deploy.py" "$@"' in wrapper
    assert "repair_contaminated_current_release.sh" not in wrapper
    assert "immutable_deploy.sh" not in wrapper
    assert "install_runtime_write_guard.sh" not in wrapper


def test_recovery_script_is_fail_closed_and_does_not_restart_the_live_service() -> None:
    script = RECOVERY_SCRIPT.read_text(encoding="utf-8")

    assert "CURRENT_RELEASE_RECOVERY_READY" in script
    assert "CURRENT_RELEASE_ROLLBACK_REBUILT" in script
    assert "CURRENT_RELEASE_PREVIOUS_RESCUED" in script
    assert "CURRENT_RELEASE_MARKER_STALE" in script
    assert "CURRENT_RELEASE_RUNTIME_INCOMPATIBLE" in script
    assert "CONTAMINATED_RELEASE_REMOVED" in script
    assert 'git -C "$SOURCE_DIR" cat-file -e "$sha^{commit}"' in script
    assert "build_clean_recovery_release" in script
    assert "switch_to_recovery_target" in script
    assert "runtime_compatible_release" in script
    assert 'validate_release "$recovery_dir"' in script
    assert 'atomic_point_current_to "$recovery_path"' in script
    assert 'atomic_point_current_to "$failed_path" || true' in script
    assert 'inspect "$CURRENT_LINK" --required' in script
    assert "systemctl stop" not in script
    assert "systemctl restart" not in script
    assert "git reset" not in script
    assert "rm -rf --one-file-system" in script


@pytest.mark.skipif(
    os.name != "posix"
    or shutil.which("bash") is None
    or shutil.which("git") is None
    or shutil.which("timeout") is None,
    reason="requires POSIX git/bash/timeout",
)
def test_contaminated_current_is_repointed_to_clean_recovery_and_cleaned(tmp_path: Path) -> None:
    source = tmp_path / "source"
    scripts = source / "scripts"
    scripts.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=source, check=True)
    (source / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=source, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()

    manager = scripts / "fake_manager.py"
    manager.write_text(
        """from __future__ import annotations
import json
import sys
from pathlib import Path

command = sys.argv[1]
path = Path(sys.argv[2])
if command == 'validate':
    if not (path / 'VALID').is_file():
        raise SystemExit(1)
    print(json.dumps({'sha': path.name, 'path': str(path.resolve())}))
elif command == 'inspect':
    target = path.resolve(strict=True)
    if not (target / 'VALID').is_file():
        raise SystemExit(1)
    print(json.dumps({'sha': target.name, 'path': str(target)}))
else:
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    builder = scripts / "fake_builder.sh"
    builder.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
sha="$1"
mkdir -p "$RELEASES_DIR/$sha"
printf 'valid\n' > "$RELEASES_DIR/$sha/VALID"
""",
        encoding="utf-8",
    )
    compatibility = scripts / "fake_compatibility.sh"
    compatibility.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
test -f "$1/VALID"
""",
        encoding="utf-8",
    )

    runtime = tmp_path / "runtime"
    releases = runtime / "releases"
    releases.mkdir(parents=True)
    contaminated = releases / sha
    contaminated.mkdir()
    (contaminated / "MUTATED").write_text("legacy runtime write\n", encoding="utf-8")
    current = runtime / "current"
    previous = runtime / "previous"
    current.symlink_to(contaminated)
    state = tmp_path / "deploy-state"
    state.mkdir()
    (state / "deployed_sha").write_text(f"{sha}\n", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "METRO_RUNTIME_ROOT": str(runtime),
            "METRO_RELEASES_DIR": str(releases),
            "METRO_CURRENT_RELEASE_LINK": str(current),
            "METRO_PREVIOUS_RELEASE_LINK": str(previous),
            "DEPLOY_STATE_DIR": str(state),
            "DEPLOYED_SHA_FILE": str(state / "deployed_sha"),
            "SYSTEM_PYTHON": sys.executable,
            "RELEASE_MANAGER": str(manager),
            "RELEASE_BUILDER": str(builder),
            "RELEASE_RUNTIME_COMPATIBILITY_CHECKER": str(compatibility),
            "TIMEOUT_BIN": shutil.which("timeout") or "/usr/bin/timeout",
            "RELEASE_BUILD_TIMEOUT_SECONDS": "30",
            "RELEASE_COMPAT_TIMEOUT_SECONDS": "30",
            "SHARED_AUDIO_DIR": str(tmp_path / "audio"),
        }
    )

    repair = subprocess.run(
        ["bash", str(RECOVERY_SCRIPT), "repair", str(source)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    repaired = current.resolve()
    assert "CURRENT_RELEASE_RECOVERY_READY" in repair.stdout
    assert repaired != contaminated
    assert repaired.name == sha
    assert (repaired / "VALID").is_file()
    assert contaminated.is_dir()

    candidate = releases / ("b" * 40)
    candidate.mkdir()
    (candidate / "VALID").write_text("valid\n", encoding="utf-8")
    current.unlink()
    current.symlink_to(candidate)
    previous.symlink_to(repaired)

    first_cleanup = subprocess.run(
        ["bash", str(RECOVERY_SCRIPT), "cleanup", str(source)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "CONTAMINATED_RELEASE_REMOVED" in first_cleanup.stdout
    assert not contaminated.exists()
    assert repaired.exists()

    previous.unlink()
    previous.symlink_to(candidate)
    second_cleanup = subprocess.run(
        ["bash", str(RECOVERY_SCRIPT), "cleanup", str(source)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "UNREFERENCED_RECOVERY_RELEASE_REMOVED" in second_cleanup.stdout
    assert not repaired.exists()
