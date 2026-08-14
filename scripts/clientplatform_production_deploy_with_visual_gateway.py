from __future__ import annotations

"""Deploy the versioned Visual Gateway before the canonical ClientPlatform deploy.

The wrapper deliberately leaves the existing production deploy implementation intact.
On the first gateway rollout there is no previous gateway image to restore, so a healthy
new gateway remains in place if the application deploy rolls back: the new Compose
contract routes both the current and rollback app through that internal gateway. On
later rollouts the previous gateway image is restored when the application deploy
fails.
"""

import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

from scripts.clientplatform_prepare_production_env import prepare

DEPLOY_DIR = ROOT / "deploy" / "clientplatform"
ENV_FILE = DEPLOY_DIR / "clientplatform.env"
CORE_DEPLOY = ROOT / "scripts" / "clientplatform_production_deploy.py"
GATEWAY_IMAGE = "clientplatform-production-visual-gateway"
GATEWAY_SERVICE = "visual-gateway"
GATEWAY_HEALTH_TIMEOUT_SECONDS = 120


class VisualGatewayDeploymentError(RuntimeError):
    """Sanitized Visual Gateway deployment failure."""


def _run(
    command: Sequence[str],
    *,
    cwd: Path = DEPLOY_DIR,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    executable = Path(command[0]).name if command else "command"
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            text=True,
            capture_output=capture,
        )
    except OSError as exc:
        raise VisualGatewayDeploymentError(
            f"visual_gateway_command_failed:{executable}:exec"
        ) from exc
    if check and completed.returncode != 0:
        raise VisualGatewayDeploymentError(
            f"visual_gateway_command_failed:{executable}:{completed.returncode}"
        )
    return completed


def _compose() -> list[str]:
    command = ["docker", "compose"]
    if (DEPLOY_DIR / ".env").is_file():
        command.extend(["--env-file", ".env"])
    command.extend(
        [
            "--env-file",
            "clientplatform.env",
            "-f",
            "compose.production.yml",
        ]
    )
    return command


def _container_id(compose: Sequence[str]) -> str:
    completed = _run(
        [*compose, "ps", "-q", GATEWAY_SERVICE],
        capture=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _container_image(compose: Sequence[str]) -> str | None:
    container = _container_id(compose)
    if not container:
        return None
    completed = _run(
        ["docker", "inspect", "--format", "{{.Image}}", container],
        capture=True,
        check=False,
    )
    image = completed.stdout.strip()
    if completed.returncode != 0 or not image.startswith("sha256:"):
        return None
    return image


def _gateway_health(compose: Sequence[str]) -> str:
    container = _container_id(compose)
    if not container:
        return "missing"
    completed = _run(
        [
            "docker",
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}",
            container,
        ],
        capture=True,
        check=False,
    )
    if completed.returncode != 0:
        return "missing"
    return completed.stdout.strip() or "missing"


def _wait_for_gateway(
    compose: Sequence[str],
    *,
    timeout_seconds: int = GATEWAY_HEALTH_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + max(1, timeout_seconds)
    last = "missing"
    while time.monotonic() < deadline:
        last = _gateway_health(compose)
        if last == "healthy":
            return
        if last == "unhealthy":
            raise VisualGatewayDeploymentError("visual_gateway_unhealthy")
        time.sleep(2)
    raise VisualGatewayDeploymentError(f"visual_gateway_health_timeout:{last}")


def _restore_gateway(compose: Sequence[str], previous_image: str) -> None:
    _run(["docker", "image", "tag", previous_image, GATEWAY_IMAGE])
    _run(
        [
            *compose,
            "up",
            "-d",
            "--no-build",
            "--force-recreate",
            GATEWAY_SERVICE,
        ]
    )
    _wait_for_gateway(compose)
    print("CLIENTPLATFORM_VISUAL_GATEWAY_ROLLBACK_OK")


def _deploy_gateway(compose: Sequence[str]) -> str | None:
    previous_image = _container_image(compose)
    try:
        _run([*compose, "config", "--quiet"])
        _run([*compose, "build", GATEWAY_SERVICE])
        _run([*compose, "up", "-d", "--force-recreate", GATEWAY_SERVICE])
        _wait_for_gateway(compose)
    except VisualGatewayDeploymentError:
        if previous_image:
            _restore_gateway(compose, previous_image)
        raise
    return previous_image


def _tag_release_image(compose: Sequence[str]) -> str:
    image = _container_image(compose)
    if not image:
        raise VisualGatewayDeploymentError("visual_gateway_image_missing_after_deploy")
    sha = _run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture=True).stdout.strip().lower()
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise VisualGatewayDeploymentError("invalid_release_git_sha")
    _run(["docker", "image", "tag", image, f"{GATEWAY_IMAGE}:release-{sha}"])
    return image


def main() -> int:
    if not ENV_FILE.is_file():
        raise VisualGatewayDeploymentError("clientplatform_production_env_missing")
    prepare(ENV_FILE)
    compose = _compose()
    previous_image = _deploy_gateway(compose)
    print("CLIENTPLATFORM_VISUAL_GATEWAY_CAPABILITIES_OK:contract=1.0")

    try:
        completed = subprocess.run(
            [sys.executable, str(CORE_DEPLOY), *sys.argv[1:]],
            cwd=ROOT,
            check=False,
        )
    except OSError as exc:
        if previous_image:
            _restore_gateway(compose, previous_image)
        raise VisualGatewayDeploymentError("core_deploy_command_failed:exec") from exc

    if completed.returncode != 0:
        if previous_image:
            _restore_gateway(compose, previous_image)
        else:
            # First rollout: keep the healthy new gateway because the new Compose
            # contract also routes the core deploy's rollback app through it.
            _wait_for_gateway(compose)
            print("CLIENTPLATFORM_VISUAL_GATEWAY_FIRST_ROLLOUT_RETAINED")
        return completed.returncode

    image = _tag_release_image(compose)
    print(f"CLIENTPLATFORM_VISUAL_GATEWAY_DEPLOY_OK:image={image}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VisualGatewayDeploymentError as exc:
        print(f"CLIENTPLATFORM_VISUAL_GATEWAY_DEPLOY_FAILED:{exc}", file=sys.stderr)
        raise SystemExit(1)
