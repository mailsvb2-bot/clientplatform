from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_PATH = ROOT / "deploy.sh"
PRODUCTION_DEPLOY_PATH = ROOT / "scripts" / "clientplatform_production_deploy.py"


def _read(path: Path, label: str, problems: list[str]) -> str:
    if not path.is_file():
        problems.append(f"missing {label}: {path}")
        return ""
    return path.read_text(encoding="utf-8")


def deploy_governance_problems(
    path: Path = DEPLOY_PATH,
    production_path: Path = PRODUCTION_DEPLOY_PATH,
    remote_path: Path | None = None,
) -> list[str]:
    """Validate the single ClientPlatform production deploy contour.

    ``remote_path`` is retained only for source compatibility with older callers.
    Production deploy no longer performs GitHub topology/fetch/merge operations;
    source synchronization is deliberately outside the runtime deployment boundary.
    """

    del remote_path
    problems: list[str] = []
    wrapper = _read(path, "deploy wrapper", problems)
    production = _read(production_path, "ClientPlatform production deploy", problems)
    combined = "\n".join((wrapper, production))

    forbidden = {
        "git push": "production deploy must not push to GitHub",
        "commit --allow-empty": "production deploy must not manufacture audit commits",
        "git reset --hard": "runtime rollback must not mutate the source checkout",
        "git checkout": "production deploy must not change source branches",
        "fetch --prune": "production deploy must not fetch source from GitHub",
        "merge --ff-only": "production deploy must not merge source from GitHub",
        "/root/clientplatform": "obsolete source runtime remains",
        "/etc/clientplatform": "obsolete production config remains",
        "clientplatform.service": "obsolete systemd identity remains",
        "scripts/immutable_deploy.sh": "legacy immutable deploy entrypoint remains active",
        "run_deploy_worker.sh": "legacy deploy webhook worker remains active",
    }
    for needle, message in forbidden.items():
        if needle in combined:
            problems.append(message)

    wrapper_required = {
        "scripts/clientplatform_production_deploy.py": (
            "deploy wrapper does not delegate to ClientPlatform production deploy"
        ),
        "exec python3": "deploy wrapper must exec the canonical Python deploy",
    }
    for needle, message in wrapper_required.items():
        if needle not in wrapper:
            problems.append(message)

    production_required = {
        'DEPLOY_DIR = ROOT / "deploy" / "clientplatform"': (
            "canonical ClientPlatform deploy directory is missing"
        ),
        'LOCK_PATH = Path("/run/lock/clientplatform-production-deploy.lock")': (
            "canonical production deploy lock is missing"
        ),
        "fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)": (
            "production deploy singleton lock is missing"
        ),
        "_assert_tracked_worktree_clean()": (
            "production deploy does not fail closed on tracked source changes"
        ),
        "prepare(env_file)": "canonical production env preparation is missing",
        "_encrypted_backup(compose)": "encrypted pre-deploy backup path is missing",
        "_local_backup(target_sha)": "explicit emergency local backup path is missing",
        "_wait_for_readiness(timeout_seconds)": "readiness gate is missing",
        "_external_https(domain)": "external HTTPS proof is missing",
        "_rollback(": "rollback implementation is missing",
        "_write_evidence(": "deployment evidence writer is missing",
        "CLIENTPLATFORM_PRODUCTION_DEPLOY_OK": "deployment success evidence marker is missing",
        "CLIENTPLATFORM_PRODUCTION_ROLLBACK_OK": "rollback evidence marker is missing",
    }
    for needle, message in production_required.items():
        if needle not in production:
            problems.append(message)

    backup_pos = production.find("backup_reference =")
    change_pos = production.find("changed = False")
    readiness_pos = production.find("_wait_for_readiness(timeout_seconds)", change_pos)
    success_pos = production.find("CLIENTPLATFORM_PRODUCTION_DEPLOY_OK")
    if min(backup_pos, change_pos, readiness_pos, success_pos) < 0 or not (
        backup_pos < change_pos < readiness_pos < success_pos
    ):
        problems.append(
            "deploy order must be backup, runtime change, readiness proof, then success evidence"
        )

    return problems


def main() -> int:
    problems = deploy_governance_problems()
    if problems:
        print("DEPLOY_GOVERNANCE_FAILED")
        for problem in problems:
            print(problem)
        return 1
    print(
        "DEPLOY_GOVERNANCE_OK single_runtime=1 singleton_lock=1 "
        "predeploy_backup=1 readiness_gate=1 rollback=1 github_writes=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
