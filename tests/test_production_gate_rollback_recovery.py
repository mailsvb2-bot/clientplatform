from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy.sh"
PRODUCTION_DEPLOY = ROOT / "scripts" / "clientplatform_production_deploy.py"
RECOVERY = ROOT / "scripts" / "repair_contaminated_current_release.sh"
WRITE_GUARD = ROOT / "scripts" / "install_runtime_write_guard.sh"
BODY_HANDLER = ROOT / "handlers" / "mood_flow" / "body.py"


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


def test_runtime_write_guard_and_recovery_scripts_have_valid_bash_syntax() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    for path in (DEPLOY, RECOVERY, WRITE_GUARD):
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


def test_mood_schedule_rollback_uses_narrow_exception_boundary() -> None:
    tree = ast.parse(BODY_HANDLER.read_text(encoding="utf-8"))
    broad: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            broad.append(node.lineno)
            continue
        names: set[str] = set()
        targets = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
        if names.intersection({"Exception", "BaseException"}):
            broad.append(node.lineno)
    assert broad == []


def test_mood_schedule_releases_marker_on_normalization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from handlers.mood_flow import body

    unmarked: list[tuple[Any, ...]] = []
    monkeypatch.setattr(body, "mark_delivery_once", lambda *_args: True)
    monkeypatch.setattr(
        body,
        "add_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("invalid schedule")),
    )
    monkeypatch.setattr(body, "unmark_delivery", lambda *args: unmarked.append(args))

    with pytest.raises(ValueError, match="invalid schedule"):
        body._persist_post_schedule_sync(
            session_id="17",
            user_id=84,
            kind="home",
            run_at_iso="invalid",
            run_at_epoch=2,
        )

    assert unmarked == [(84, "home", "post_prompt_schedule", body.for_session("17"))]


def test_mood_schedule_duplicate_marker_skips_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from handlers.mood_flow import body

    enqueued: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(body, "mark_delivery_once", lambda *_args: False)
    monkeypatch.setattr(
        body,
        "add_job",
        lambda *args, **kwargs: enqueued.append((args, kwargs)),
    )

    created = body._persist_post_schedule_sync(
        session_id="18",
        user_id=85,
        kind="work",
        run_at_iso="2026-07-22T12:00:00+00:00",
        run_at_epoch=3,
    )

    assert created is False
    assert enqueued == []


def test_failed_switch_is_rescued_to_runtime_compatible_previous_release(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    timeout = shutil.which("timeout")
    git = shutil.which("git")
    assert bash and timeout and git

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Recovery Test")
    _git(repo, "config", "user.email", "recovery@example.test")
    recorded_sha = _commit(repo, "recorded.txt", "recorded\n")
    failed_sha = _commit(repo, "failed.txt", "failed\n")

    runtime = tmp_path / "runtime"
    releases = runtime / "releases"
    recovery_root = runtime / "recovery-releases"
    state = tmp_path / "state"
    releases.mkdir(parents=True)
    recovery_root.mkdir(parents=True)
    state.mkdir()

    failed_release = releases / failed_sha
    failed_release.mkdir()
    recorded_release = recovery_root / "generation-1" / recorded_sha
    recorded_release.mkdir(parents=True)
    (recorded_release / ".valid").write_text("ok\n", encoding="utf-8")

    current = runtime / "current"
    previous = runtime / "previous"
    current.symlink_to(failed_release)
    previous.symlink_to(recorded_release)
    deployed_sha = state / "deployed_sha"
    deployed_sha.write_text(f"{recorded_sha}\n", encoding="utf-8")

    manager = tmp_path / "manager.py"
    manager.write_text(
        """
from __future__ import annotations
import json
import sys
from pathlib import Path

command = sys.argv[1]
path = Path(sys.argv[2]).resolve(strict=True)
if command == "validate":
    raise SystemExit(0 if (path / ".valid").is_file() else 1)
if command == "inspect":
    if not (path / ".valid").is_file():
        raise SystemExit(1)
    print(json.dumps({"path": str(path), "sha": path.name}))
    raise SystemExit(0)
raise SystemExit(2)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    builder = tmp_path / "builder.sh"
    builder.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
    compatibility = tmp_path / "compatibility.sh"
    compatibility.write_text(
        "#!/usr/bin/env bash\nset -Eeuo pipefail\ntest -f \"$1/.valid\"\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "METRO_RUNTIME_ROOT": str(runtime),
            "METRO_RELEASES_DIR": str(releases),
            "METRO_CURRENT_RELEASE_LINK": str(current),
            "METRO_PREVIOUS_RELEASE_LINK": str(previous),
            "DEPLOY_STATE_DIR": str(state),
            "DEPLOYED_SHA_FILE": str(deployed_sha),
            "METRO_RECOVERY_RELEASES_ROOT": str(recovery_root),
            "METRO_RECOVERY_STATE_DIR": str(state / "contaminated"),
            "SYSTEM_PYTHON": sys.executable,
            "RELEASE_MANAGER": str(manager),
            "RELEASE_BUILDER": str(builder),
            "RELEASE_RUNTIME_COMPATIBILITY_CHECKER": str(compatibility),
            "TIMEOUT_BIN": timeout,
            "RELEASE_BUILD_TIMEOUT_SECONDS": "30",
            "RELEASE_COMPAT_TIMEOUT_SECONDS": "30",
        }
    )

    completed = _run(bash, str(RECOVERY), "repair", str(repo), cwd=ROOT, env=env)
    assert completed.returncode == 0, completed.stderr
    assert "CURRENT_RELEASE_PREVIOUS_RESCUED" in completed.stdout
    assert current.resolve(strict=True) == recorded_release.resolve(strict=True)
    assert previous.resolve(strict=True) == recorded_release.resolve(strict=True)
