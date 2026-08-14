from __future__ import annotations

"""Safely update the dedicated Docker Compose ClientPlatform production deployment."""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Sequence

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

from scripts.clientplatform_prepare_production_env import prepare

DEPLOY_DIR = ROOT / "deploy" / "clientplatform"
APP_CONTAINER = "clientplatform-production-app-1"
POSTGRES_CONTAINER = "clientplatform-production-postgres-1"
VISUAL_GATEWAY_CONTAINER = "clientplatform-production-visual-gateway-1"
APP_IMAGE = "clientplatform-production-app"
VISUAL_GATEWAY_IMAGE = "clientplatform-production-visual-gateway"
LOCK_PATH = Path("/run/lock/clientplatform-production-deploy.lock")
EVIDENCE_DIR = Path("/var/lib/clientplatform/deploy-evidence")
LOCAL_BACKUP_DIR = Path("/var/backups/clientplatform/predeploy")
_DEFAULT_TELEGRAM_WEBHOOK_PREFIX = "/telegram-webhook"
_VISUAL_GATEWAY_CAPABILITIES = {
    "contract_version": "1.0",
    "capabilities": ["generation", "render_pack", "usage"],
    "render_formats": ["square", "feed", "story", "landscape"],
}


class DeploymentError(RuntimeError):
    """Sanitized deployment failure safe for operator logs."""


def _run(
    command: Sequence[str],
    *,
    cwd: Path = DEPLOY_DIR,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        text=True,
        capture_output=capture,
    )
    if check and completed.returncode != 0:
        raise DeploymentError(f"command_failed:{Path(command[0]).name}:{completed.returncode}")
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


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _telegram_webhook_prefix(values: dict[str, str] | None = None) -> str:
    source = values
    if source is None:
        source = _env_values(DEPLOY_DIR / "clientplatform.env")
    prefix = str(
        source.get("TELEGRAM_WEBHOOK_PREFIX", _DEFAULT_TELEGRAM_WEBHOOK_PREFIX)
        or _DEFAULT_TELEGRAM_WEBHOOK_PREFIX
    ).strip()
    invalid = (
        len(prefix) > 256
        or not prefix.startswith("/")
        or prefix.startswith("//")
        or prefix == "/"
        or any(character.isspace() for character in prefix)
        or any(character in prefix for character in ("?", "#", "\\"))
    )
    if invalid:
        raise DeploymentError("invalid_telegram_webhook_prefix")
    return prefix


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _completed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_sha() -> str:
    completed = _run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture=True)
    sha = completed.stdout.strip().lower()
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise DeploymentError("invalid_git_sha")
    return sha


def _assert_tracked_worktree_clean() -> None:
    completed = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
            "--ignore-submodules=none",
        ],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise DeploymentError("tracked_worktree_check_failed")
    if any(line.strip() for line in completed.stdout.splitlines()):
        raise DeploymentError("tracked_worktree_dirty")


def _container_exists(container: str) -> bool:
    completed = _run(
        ["docker", "inspect", "--format", "{{.Id}}", container],
        capture=True,
        check=False,
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _container_running() -> bool:
    completed = _run(
        ["docker", "inspect", "--format", "{{.State.Status}}", APP_CONTAINER],
        capture=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "running"


def _container_image(container: str) -> str:
    completed = _run(
        ["docker", "inspect", "--format", "{{.Image}}", container],
        capture=True,
    )
    image = completed.stdout.strip()
    if not image.startswith("sha256:"):
        raise DeploymentError("container_image_missing")
    return image


def _optional_container_image(container: str) -> str:
    completed = _run(
        ["docker", "inspect", "--format", "{{.Image}}", container],
        capture=True,
        check=False,
    )
    image = completed.stdout.strip()
    if completed.returncode != 0 or not image.startswith("sha256:"):
        return ""
    return image


def _visual_gateway_health() -> str:
    completed = _run(
        [
            "docker",
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}",
            VISUAL_GATEWAY_CONTAINER,
        ],
        capture=True,
        check=False,
    )
    if completed.returncode != 0:
        return "missing"
    return completed.stdout.strip() or "missing"


def _visual_gateway_capabilities() -> bool:
    expected = json.dumps(_VISUAL_GATEWAY_CAPABILITIES, ensure_ascii=True, separators=(",", ":"))
    completed = _run(
        [
            "docker",
            "exec",
            VISUAL_GATEWAY_CONTAINER,
            "python",
            "-c",
            (
                "import json,os,urllib.request;"
                "token=os.environ['VISUAL_GATEWAY_TOKEN'];"
                "request=urllib.request.Request("
                "'http://127.0.0.1:8080/v1/capabilities',"
                "headers={'Authorization':'Bearer '+token});"
                "payload=json.load(urllib.request.urlopen(request,timeout=3));"
                f"expected=json.loads({expected!r});"
                "print('1' if payload==expected else '0')"
            ),
        ],
        capture=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "1"


def _wait_for_visual_gateway(timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last = "missing"
    while time.monotonic() < deadline:
        last = _visual_gateway_health()
        if last == "healthy" and _visual_gateway_capabilities():
            return
        if last == "unhealthy":
            raise DeploymentError("visual_gateway_unhealthy")
        time.sleep(2)
    raise DeploymentError(f"visual_gateway_readiness_timeout:{last}")


def _sha256_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _local_backup(target_sha: str) -> Path:
    LOCAL_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(LOCAL_BACKUP_DIR, 0o700)
    target = LOCAL_BACKUP_DIR / f"clientplatform-{_utc_stamp()}-{target_sha[:12]}.dump"
    with target.open("xb") as handle:
        completed = subprocess.run(
            [
                "docker",
                "exec",
                POSTGRES_CONTAINER,
                "pg_dump",
                "-U",
                "clientplatform_admin",
                "-d",
                "clientplatform",
                "--format=custom",
                "--compress=9",
                "--no-owner",
                "--no-acl",
            ],
            cwd=DEPLOY_DIR,
            check=False,
            stdout=handle,
            stderr=subprocess.DEVNULL,
        )
    if completed.returncode != 0 or target.stat().st_size <= 0:
        target.unlink(missing_ok=True)
        raise DeploymentError("local_predeploy_backup_failed")
    os.chmod(target, 0o600)
    with target.open("rb") as handle:
        digest = _sha256_stream(handle)
    checksum = target.with_suffix(target.suffix + ".sha256")
    checksum.write_text(f"{digest}  {target.name}\n", encoding="ascii")
    os.chmod(checksum, 0o600)
    return target


def _encrypted_backup(compose: Sequence[str]) -> str:
    _run([*compose, "--profile", "operations", "build", "backup"])
    completed = _run(
        [*compose, "--profile", "operations", "run", "--rm", "backup"],
        capture=True,
    )
    marker = "CLIENTPLATFORM_ENCRYPTED_BACKUP_OK:"
    for line in completed.stdout.splitlines():
        if line.startswith(marker):
            return line.removeprefix(marker).strip()
    raise DeploymentError("encrypted_predeploy_backup_marker_missing")


def _http_probe(path: str) -> bool:
    completed = _run(
        [
            "docker",
            "exec",
            APP_CONTAINER,
            "python",
            "-c",
            (
                "import json,urllib.request;"
                f"d=json.load(urllib.request.urlopen('http://127.0.0.1:8182{path}',timeout=3));"
                "print('1' if d.get('ok') is True else '0')"
            ),
        ],
        capture=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "1"


def _healthy() -> bool:
    return _http_probe("/healthz")


def _ready() -> bool:
    return _http_probe("/readyz")


def _runtime_markers() -> bool:
    completed = _run(
        ["docker", "logs", "--since", "10m", APP_CONTAINER],
        capture=True,
        check=False,
    )
    payload = completed.stdout + completed.stderr
    return (
        "CLIENTPLATFORM_PRODUCTION_PREFLIGHT_OK" in payload
        and "CLIENTPLATFORM_BOT_GATEWAY_PREFLIGHT_OK" in payload
        and "clientplatform dispatch runtime started" in payload
    )


def _wait_for_startup(timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _container_running() and _healthy() and _runtime_markers():
            return
        time.sleep(3)
    raise DeploymentError("production_startup_timeout")


def _wait_for_baseline_readiness(timeout_seconds: int) -> None:
    """Check persistent state only; startup log markers intentionally age out."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _container_running() and _ready():
            return
        time.sleep(3)
    raise DeploymentError("production_baseline_readiness_timeout")


def _wait_for_readiness(timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _container_running() and _ready() and _runtime_markers():
            return
        time.sleep(3)
    raise DeploymentError("production_readiness_timeout")


def _external_root(domain: str) -> None:
    completed = _run(
        [
            "curl",
            "--proto",
            "=https",
            "--tlsv1.2",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "20",
            f"https://{domain}/",
        ],
        capture=True,
        check=False,
    )
    if completed.returncode != 0 or completed.stdout.strip() != "ClientPlatform":
        raise DeploymentError("external_https_proof_failed")


def _external_polling_absence(domain: str, webhook_prefix: str) -> None:
    completed = _run(
        [
            "curl",
            "--proto",
            "=https",
            "--tlsv1.2",
            "--silent",
            "--show-error",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            "--max-time",
            "20",
            "--request",
            "POST",
            "--header",
            "Content-Type: application/json",
            "--header",
            "X-Telegram-Bot-Api-Secret-Token: intentionally-invalid-deploy-proof",
            "--data-binary",
            "{}",
            f"https://{domain}{webhook_prefix}",
        ],
        capture=True,
        check=False,
    )
    if completed.returncode != 0 or completed.stdout.strip() != "404":
        raise DeploymentError("external_telegram_webhook_absence_failed")


def _external_https(domain: str) -> None:
    _external_root(domain)
    _external_polling_absence(domain, _telegram_webhook_prefix())


def _write_evidence(payload: dict[str, Any]) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(EVIDENCE_DIR, 0o700)
    target = EVIDENCE_DIR / f"deploy-{_utc_stamp()}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    latest = EVIDENCE_DIR / "latest.json"
    latest_tmp = EVIDENCE_DIR / "latest.json.tmp"
    latest_tmp.write_bytes(target.read_bytes())
    os.chmod(latest_tmp, 0o600)
    os.replace(latest_tmp, latest)
    return target


def _restore_visual_gateway(
    *,
    compose: Sequence[str],
    rollback_tag: str,
    timeout_seconds: int,
) -> None:
    if rollback_tag:
        _run(["docker", "image", "tag", rollback_tag, f"{VISUAL_GATEWAY_IMAGE}:latest"])
        _run(
            [
                *compose,
                "up",
                "-d",
                "--no-build",
                "--force-recreate",
                "visual-gateway",
            ]
        )
    _wait_for_visual_gateway(timeout_seconds)


def _remove_first_rollout_visual_gateway(compose: Sequence[str]) -> None:
    completed = _run(
        [*compose, "rm", "--force", "--stop", "visual-gateway"],
        check=False,
    )
    if completed.returncode != 0:
        raise DeploymentError("visual_gateway_first_rollout_cleanup_failed")


def _rollback(
    *,
    compose: Sequence[str],
    rollback_tag: str,
    domain: str,
    timeout_seconds: int,
    visual_gateway_rollback_tag: str | None = None,
) -> None:
    if visual_gateway_rollback_tag is not None:
        _restore_visual_gateway(
            compose=compose,
            rollback_tag=visual_gateway_rollback_tag,
            timeout_seconds=timeout_seconds,
        )
    _run(["docker", "image", "tag", rollback_tag, f"{APP_IMAGE}:latest"])
    _run([*compose, "up", "-d", "--no-build", "--force-recreate", "app", "caddy"])
    try:
        # A rollback image can legitimately predate the current startup-marker
        # contract. Validate the restored deployment by persistent readiness and
        # the external HTTPS contract; requiring new-image log markers here would
        # make a healthy legacy rollback impossible.
        _wait_for_baseline_readiness(timeout_seconds)
        _external_https(domain)
    except Exception as exc:  # validator: allow-wide-except - every rollback gate is mandatory
        raise DeploymentError("rollback_not_available") from exc


def deploy(
    *,
    allow_local_backup: bool,
    timeout_seconds: int,
    recover_unavailable_baseline: bool = False,
) -> Path:
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise DeploymentError("production_deploy_requires_root")
    _assert_tracked_worktree_clean()
    env_file = DEPLOY_DIR / "clientplatform.env"
    prepare(env_file)
    values = _env_values(env_file)
    domain = str(values.get("CLIENTPLATFORM_DOMAIN", "") or "").strip()
    if not domain or domain.endswith("your-domain.ru"):
        raise DeploymentError("production_domain_missing")
    webhook_prefix = _telegram_webhook_prefix(values)

    target_sha = _git_sha()
    compose = _compose()
    _run([*compose, "config", "--quiet"])

    app_exists = _container_exists(APP_CONTAINER)
    baseline_ready = False
    if app_exists:
        try:
            _wait_for_baseline_readiness(min(timeout_seconds, 60))
            _external_root(domain)
            baseline_ready = True
        except Exception as exc:  # validator: allow-wide-except - baseline must fail closed by default
            if not recover_unavailable_baseline:
                raise DeploymentError("production_not_ready_before_deploy") from exc

    _run([*compose, "up", "-d", "postgres"])

    age_recipient = str(values.get("CLIENTPLATFORM_BACKUP_AGE_RECIPIENT", "") or "").strip()
    if age_recipient:
        backup_mode = "encrypted"
        backup_reference = _encrypted_backup(compose)
    elif allow_local_backup:
        backup_mode = "local_plaintext_emergency"
        backup_reference = str(_local_backup(target_sha))
    else:
        raise DeploymentError("age_recipient_missing_use_explicit_local_backup_override")

    previous_image = _container_image(APP_CONTAINER) if app_exists else ""
    rollback_tag = f"{APP_IMAGE}:rollback-{_utc_stamp()}" if previous_image else ""
    if previous_image:
        _run(["docker", "image", "tag", previous_image, rollback_tag])

    visual_gateway_exists = _container_exists(VISUAL_GATEWAY_CONTAINER)
    previous_visual_gateway_image = (
        _optional_container_image(VISUAL_GATEWAY_CONTAINER) if visual_gateway_exists else ""
    )
    visual_gateway_rollback_tag = (
        f"{VISUAL_GATEWAY_IMAGE}:rollback-{_utc_stamp()}" if previous_visual_gateway_image else ""
    )
    if previous_visual_gateway_image:
        _run(
            [
                "docker",
                "image",
                "tag",
                previous_visual_gateway_image,
                visual_gateway_rollback_tag,
            ]
        )

    visual_gateway_changed = False
    app_changed = False
    try:
        _run([*compose, "build", "visual-gateway"])
        _run([*compose, "build", "app", "backup"])
        _run([*compose, "up", "-d", "--force-recreate", "visual-gateway"])
        visual_gateway_changed = True
        _wait_for_visual_gateway(timeout_seconds)
        _run([*compose, "up", "-d", "--force-recreate", "app", "caddy"])
        app_changed = True
        _wait_for_readiness(timeout_seconds)
        _external_https(domain)
    except Exception as deployment_error:  # validator: allow-wide-except - rollback must cover every failed gate
        if app_changed and baseline_ready and rollback_tag:
            try:
                _rollback(
                    compose=compose,
                    rollback_tag=rollback_tag,
                    visual_gateway_rollback_tag=visual_gateway_rollback_tag,
                    domain=domain,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as rollback_error:  # validator: allow-wide-except - surface failed recovery distinctly
                raise DeploymentError("deployment_failed_and_rollback_failed") from rollback_error
            rollback_evidence = _write_evidence(
                {
                    "ok": False,
                    "operation": "production_deploy_rollback",
                    "target_sha": target_sha,
                    "previous_image": previous_image,
                    "rollback_tag": rollback_tag,
                    "previous_visual_gateway_image": previous_visual_gateway_image,
                    "visual_gateway_rollback_tag": visual_gateway_rollback_tag,
                    "visual_gateway_contract_version": "1.0",
                    "backup_mode": backup_mode,
                    "backup_reference": backup_reference,
                    "domain": domain,
                    "telegram_transport": "polling",
                    "telegram_webhook_prefix": webhook_prefix,
                    "telegram_webhook_absent": True,
                    "failure_class": type(deployment_error).__name__,
                    "rollback_full_readiness": _ready(),
                    "visual_gateway_ready": _visual_gateway_capabilities(),
                    "completed_at": _completed_at(),
                }
            )
            print(f"CLIENTPLATFORM_PRODUCTION_ROLLBACK_OK:{rollback_evidence}")
        elif visual_gateway_changed and not app_changed:
            try:
                if visual_gateway_rollback_tag:
                    _restore_visual_gateway(
                        compose=compose,
                        rollback_tag=visual_gateway_rollback_tag,
                        timeout_seconds=timeout_seconds,
                    )
                else:
                    _remove_first_rollout_visual_gateway(compose)
            except Exception as rollback_error:  # validator: allow-wide-except - gateway rollback is mandatory
                raise DeploymentError("visual_gateway_deployment_failed_and_rollback_failed") from rollback_error
        if app_changed and recover_unavailable_baseline and not baseline_ready:
            recovery_evidence = _write_evidence(
                {
                    "ok": False,
                    "operation": "production_recovery_failed",
                    "target_sha": target_sha,
                    "previous_image": previous_image,
                    "rollback_tag": rollback_tag,
                    "previous_visual_gateway_image": previous_visual_gateway_image,
                    "visual_gateway_rollback_tag": visual_gateway_rollback_tag,
                    "visual_gateway_contract_version": "1.0",
                    "backup_mode": backup_mode,
                    "backup_reference": backup_reference,
                    "domain": domain,
                    "telegram_transport": "polling",
                    "telegram_webhook_prefix": webhook_prefix,
                    "failure_class": type(deployment_error).__name__,
                    "baseline_ready": False,
                    "rollback_skipped": True,
                    "completed_at": _completed_at(),
                }
            )
            print(
                f"CLIENTPLATFORM_PRODUCTION_RECOVERY_FAILED:{recovery_evidence}",
                file=sys.stderr,
            )
            raise DeploymentError("production_recovery_failed") from deployment_error
        raise

    visual_gateway_image = _container_image(VISUAL_GATEWAY_CONTAINER)
    _run(
        [
            "docker",
            "image",
            "tag",
            visual_gateway_image,
            f"{VISUAL_GATEWAY_IMAGE}:release-{target_sha}",
        ]
    )
    evidence = _write_evidence(
        {
            "ok": True,
            "operation": "production_deploy",
            "target_sha": target_sha,
            "previous_image": previous_image,
            "rollback_tag": rollback_tag,
            "previous_visual_gateway_image": previous_visual_gateway_image,
            "visual_gateway_rollback_tag": visual_gateway_rollback_tag,
            "visual_gateway_image": visual_gateway_image,
            "visual_gateway_contract_version": "1.0",
            "visual_gateway_ready": True,
            "backup_mode": backup_mode,
            "backup_reference": backup_reference,
            "domain": domain,
            "telegram_transport": "polling",
            "telegram_webhook_prefix": webhook_prefix,
            "telegram_webhook_absent": True,
            "baseline_ready": baseline_ready,
            "recovery_mode": bool(app_exists and not baseline_ready),
            "completed_at": _completed_at(),
        }
    )
    print(f"CLIENTPLATFORM_PRODUCTION_DEPLOY_OK:{evidence}")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-backup", action="store_true")
    parser.add_argument("--recover-unavailable-baseline", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=240)
    args = parser.parse_args()
    timeout_seconds = max(60, min(int(args.timeout_seconds), 900))
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOCK_PATH.open("a+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
