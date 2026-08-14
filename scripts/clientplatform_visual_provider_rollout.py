from __future__ import annotations

"""Canonical provider-gateway stage for the ClientPlatform production deploy."""

import argparse
import fcntl
import os
import sys
import time

from scripts import clientplatform_production_deploy as core

PROVIDER_SERVICE = "visual-provider-gateway"
PROVIDER_CONTAINER = "clientplatform-production-visual-provider-gateway-1"
PROVIDER_IMAGE = "clientplatform-production-visual-provider-gateway"


def _health() -> str:
    completed = core._run(
        [
            "docker",
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}",
            PROVIDER_CONTAINER,
        ],
        capture=True,
        check=False,
    )
    if completed.returncode != 0:
        return "missing"
    return completed.stdout.strip() or "missing"


def _authenticated_provider_probe() -> bool:
    completed = core._run(
        [
            "docker",
            "exec",
            PROVIDER_CONTAINER,
            "python",
            "-c",
            (
                "import json,os,urllib.request;"
                "token=os.environ['VISUAL_GATEWAY_UPSTREAM_TOKEN'];"
                "request=urllib.request.Request("
                "'http://127.0.0.1:8097/v1/providers',"
                "headers={'Authorization':'Bearer '+token});"
                "payload=json.load(urllib.request.urlopen(request,timeout=3));"
                "assert payload.get('client_id')=='clientplatform';"
                "assert payload.get('enabled') is True;"
                "print('1')"
            ),
        ],
        capture=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "1"


def _wait_for_provider(timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last = "missing"
    while time.monotonic() < deadline:
        last = _health()
        if last == "healthy" and _authenticated_provider_probe():
            return
        if last == "unhealthy":
            raise core.DeploymentError("visual_provider_gateway_unhealthy")
        time.sleep(2)
    raise core.DeploymentError(f"visual_provider_gateway_readiness_timeout:{last}")


def _restore_provider(compose: list[str], rollback_tag: str, timeout_seconds: int) -> None:
    if not rollback_tag:
        return
    core._run(["docker", "image", "tag", rollback_tag, f"{PROVIDER_IMAGE}:latest"])
    core._run(
        [
            *compose,
            "up",
            "-d",
            "--no-build",
            "--no-deps",
            "--force-recreate",
            PROVIDER_SERVICE,
        ]
    )
    _wait_for_provider(timeout_seconds)


def _remove_first_rollout_provider(compose: list[str]) -> None:
    completed = core._run(
        [*compose, "rm", "--force", "--stop", PROVIDER_SERVICE],
        check=False,
    )
    if completed.returncode != 0:
        raise core.DeploymentError("visual_provider_gateway_first_rollout_cleanup_failed")


def _prepare_provider(timeout_seconds: int) -> tuple[str, str, bool]:
    target_sha = core._git_sha()
    os.environ["CLIENTPLATFORM_BUILD_VCS_REF"] = target_sha

    env_file = core.DEPLOY_DIR / "clientplatform.env"
    core.prepare(env_file)
    compose = core._compose()
    core._run([*compose, "config", "--quiet"])

    existed = core._container_exists(PROVIDER_CONTAINER)
    previous_image = core._optional_container_image(PROVIDER_CONTAINER) if existed else ""
    rollback_tag = (
        f"{PROVIDER_IMAGE}:rollback-provider-{core._utc_stamp()}"
        if previous_image
        else ""
    )
    if previous_image:
        core._run(["docker", "image", "tag", previous_image, rollback_tag])

    try:
        core._run([*compose, "build", PROVIDER_SERVICE])
        core._run(
            [
                *compose,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                PROVIDER_SERVICE,
            ]
        )
        _wait_for_provider(timeout_seconds)
    except Exception:
        if rollback_tag:
            _restore_provider(compose, rollback_tag, timeout_seconds)
        else:
            _remove_first_rollout_provider(compose)
        raise

    return target_sha, rollback_tag, existed


def rollout(
    *,
    allow_local_backup: bool,
    timeout_seconds: int,
    recover_unavailable_baseline: bool,
) -> None:
    target_sha, provider_rollback_tag, provider_existed = _prepare_provider(timeout_seconds)
    try:
        evidence = core.deploy(
            allow_local_backup=allow_local_backup,
            timeout_seconds=timeout_seconds,
            recover_unavailable_baseline=recover_unavailable_baseline,
        )
    except Exception:
        if provider_existed and provider_rollback_tag:
            _restore_provider(core._compose(), provider_rollback_tag, timeout_seconds)
        raise

    provider_image = core._container_image(PROVIDER_CONTAINER)
    core._run(
        [
            "docker",
            "image",
            "tag",
            provider_image,
            f"{PROVIDER_IMAGE}:release-{target_sha}",
        ]
    )
    print(f"CLIENTPLATFORM_VISUAL_PROVIDER_GATEWAY_OK:{target_sha}:{provider_image}")
    print(f"CLIENTPLATFORM_PRODUCTION_DEPLOY_EVIDENCE:{evidence}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-backup", action="store_true")
    parser.add_argument("--recover-unavailable-baseline", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=240)
    args = parser.parse_args()
    timeout_seconds = max(60, min(int(args.timeout_seconds), 900))

    try:
        core.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with core.LOCK_PATH.open("a+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            rollout(
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
    except core.DeploymentError as exc:
        print(f"CLIENTPLATFORM_PRODUCTION_DEPLOY_FAILED:{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
