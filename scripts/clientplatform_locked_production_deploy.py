from __future__ import annotations

"""Run the production deploy under a lock descriptor owned by the outer updater.

The shell updater keeps the descriptor open after this process exits, so the
same production-deploy flock remains held through the post-deploy stability
window and any rollback. Direct invocations of clientplatform_production_deploy
still contend on the same lock path and therefore cannot interleave.
"""

import argparse
import fcntl
import os
import sys
from pathlib import Path

from scripts.clientplatform_production_deploy import (
    LOCK_PATH,
    DeploymentError,
    deploy,
)

_LOCK_FD_ENV = "CLIENTPLATFORM_DEPLOY_LOCK_FD"


def acquire_inherited_deploy_lock(
    fd: int,
    *,
    lock_path: Path = LOCK_PATH,
) -> None:
    """Validate and lock an inherited descriptor without opening a second OFD."""

    if isinstance(fd, bool) or int(fd) < 3:
        raise DeploymentError("production_deploy_lock_fd_invalid")
    descriptor = int(fd)
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = lock_path.stat()
    except OSError as exc:
        raise DeploymentError("production_deploy_lock_fd_invalid") from exc
    if (
        descriptor_stat.st_dev != path_stat.st_dev
        or descriptor_stat.st_ino != path_stat.st_ino
    ):
        raise DeploymentError("production_deploy_lock_fd_mismatch")
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _inherited_lock_fd() -> int:
    raw = str(os.getenv(_LOCK_FD_ENV) or "").strip()
    try:
        fd = int(raw)
    except ValueError as exc:
        raise DeploymentError("production_deploy_lock_fd_missing") from exc
    if fd < 3:
        raise DeploymentError("production_deploy_lock_fd_missing")
    return fd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-backup", action="store_true")
    parser.add_argument("--recover-unavailable-baseline", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=240)
    args = parser.parse_args()
    timeout_seconds = max(60, min(int(args.timeout_seconds), 900))
    try:
        acquire_inherited_deploy_lock(_inherited_lock_fd())
        deploy(
            allow_local_backup=bool(args.allow_local_backup),
            timeout_seconds=timeout_seconds,
            recover_unavailable_baseline=bool(args.recover_unavailable_baseline),
        )
    except BlockingIOError:
        print(
            "CLIENTPLATFORM_PRODUCTION_DEPLOY_FAILED:production_deploy_already_running",
            file=sys.stderr,
        )
        return 1
    except DeploymentError as exc:
        print(f"CLIENTPLATFORM_PRODUCTION_DEPLOY_FAILED:{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
