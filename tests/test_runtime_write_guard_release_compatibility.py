from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "install_runtime_write_guard.sh"
DEPLOY = ROOT / "deploy.sh"
CONTRACT_MARKER = Path("runtime") / "RUNTIME_STATE_CONTRACT_V1"


def _run_guard(
    bash: str,
    *args: str,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bash, str(GUARD), *args],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


@pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="requires POSIX bash",
)
def test_guard_selects_compatibility_for_legacy_and_enforce_for_capable_release(
    tmp_path: Path,
) -> None:
    bash = shutil.which("bash")
    assert bash is not None

    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    legacy = runtime / "recovery-releases" / "generation-1" / ("a" * 40)
    capable = runtime / "releases" / ("b" * 40)
    legacy.mkdir(parents=True)
    capable.mkdir(parents=True)
    capable_marker = capable / CONTRACT_MARKER
    capable_marker.parent.mkdir(parents=True)
    capable_marker.write_text("v1\n", encoding="utf-8")

    dropin = tmp_path / "systemd" / "zzz-runtime-write-guard.conf"
    systemctl_log = tmp_path / "systemctl.log"
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        f"printf '%s\\n' \"$*\" >> {systemctl_log!s}\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "METRO_RUNTIME_ROOT": str(runtime),
            "METRO_WRITABLE_ROOT": str(state),
            "METRO_RUNTIME_WRITE_GUARD_OVERRIDE": str(dropin),
            "SYSTEMCTL": str(fake_systemctl),
        }
    )

    enforced = _run_guard(bash, "enforce", cwd=ROOT, env=env)
    assert enforced.returncode == 0, enforced.stderr
    assert "mode=enforce" in enforced.stdout
    enforced_dropin = dropin.read_text(encoding="utf-8")
    assert f"ReadOnlyPaths={runtime}\n" in enforced_dropin
    assert f"ReadWritePaths={state}\n" in enforced_dropin

    compatibility = _run_guard(
        bash,
        "for-release",
        str(legacy),
        cwd=ROOT,
        env=env,
    )
    assert compatibility.returncode == 0, compatibility.stderr
    assert "mode=compatibility" in compatibility.stdout
    compatibility_dropin = dropin.read_text(encoding="utf-8")
    assert "ReadOnlyPaths=\n" in compatibility_dropin
    assert "ReadWritePaths=\n" in compatibility_dropin
    assert f"ReadOnlyPaths={runtime}\n" not in compatibility_dropin
    assert f"ReadWritePaths={state}\n" not in compatibility_dropin
    assert f"Environment=METRO_DATA_DIR={state / 'data'}\n" in compatibility_dropin
    assert f"Environment=METRO_LOGS_DIR={state / 'logs'}\n" in compatibility_dropin

    capable_result = _run_guard(
        bash,
        "for-release",
        str(capable),
        cwd=ROOT,
        env=env,
    )
    assert capable_result.returncode == 0, capable_result.stderr
    assert "mode=enforce" in capable_result.stdout
    capable_dropin = dropin.read_text(encoding="utf-8")
    assert f"ReadOnlyPaths={runtime}\n" in capable_dropin
    assert f"ReadWritePaths={state}\n" in capable_dropin

    assert systemctl_log.read_text(encoding="utf-8").splitlines() == [
        "daemon-reload",
        "daemon-reload",
        "daemon-reload",
    ]


def test_failed_deploy_selects_guard_for_recovered_release_before_restart() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    function_start = source.index("restore_runtime_after_failure()")
    function_end = source.index(
        "\n}\n\nacquire_deploy_lock",
        function_start,
    )
    recovery_body = source[function_start:function_end]

    repair = 'bash "$RECOVERY_SCRIPT" repair "$SOURCE_DIR"'
    select_guard = 'bash "$WRITE_GUARD_SCRIPT" for-release "$recovered_release"'
    restart = '/usr/bin/systemctl restart "$SERVICE_NAME"'
    assert recovery_body.index(repair) < recovery_body.index(select_guard) < recovery_body.index(restart)

    main_body = source[function_end:]
    enforce = 'bash "$WRITE_GUARD_SCRIPT" enforce'
    immutable = 'bash "$SOURCE_DIR/scripts/immutable_deploy.sh" "$@"'
    assert main_body.index(enforce) < main_body.index(repair) < main_body.index(immutable)


def test_current_release_declares_external_runtime_state_contract() -> None:
    marker = ROOT / CONTRACT_MARKER
    assert marker.is_file()
    text = marker.read_text(encoding="utf-8")
    assert "Runtime state contract v1" in text
    assert "ReadOnlyPaths" in text
