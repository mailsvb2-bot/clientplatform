from __future__ import annotations

"""Safely update the dedicated Docker Compose ClientPlatform production deployment."""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Sequence

from scripts.clientplatform_prepare_production_env import prepare

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = ROOT / "deploy" / "clientplatform"
APP_CONTAINER = "clientplatform-production-app-1"
POSTGRES_CONTAINER = "clientplatform-production-postgres-1"
APP_IMAGE = "clientplatform-production-app"
LOCK_PATH = Path("/run/lock/clientplatform-production-deploy.lock")
EVIDENCE_DIR = Path("/var/lib/clientplatform/deploy-evidence")
LOCAL_BACKUP_DIR = Path("/var/backups/clientplatform/predeploy")


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


def _container_image(container: str) -> str:
    completed = _run(
        ["docker", "inspect", "--format", "{{.Image}}", container],
        capture=True,
    )
    image = completed.stdout.strip()
    if not image.startswith("sha256:"):
        raise DeploymentError("current_app_image_missing")
    return image


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


def _ready() -> bool:
    completed = _run(
        [
            "docker",
            "exec",
            APP_CONTAINER,
            "python",
            "-c",
            (
                "import json,urllib.request;"
                "d=json.load(urllib.request.urlopen('http://127.0.0.1:8182/readyz',timeout=3));"
                "print('1' if d.get('ok') is True else '0')"
            ),
        ],
        capture=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "1"


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


def _wait_for_readiness(timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = _run(
            ["docker", "inspect", "--format", "{{.State.Status}}", APP_CONTAINER],
            capture=True,
            check=False,
        )
        if state.returncode == 0 and state.stdout.strip() == "running":
            if _ready() and _runtime_markers():
                return
        time.sleep(3)
    raise DeploymentError("production_readiness_timeout")


def _external_https(domain: str) -> None:
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


def _rollback(
    *,
    compose: Sequence[str],
    rollback_tag: str,
    domain: str,
    timeout_seconds: int,
) -> None:
    _run(["docker", "image", "tag", rollback_tag, f"{APP_IMAGE}:latest"])
    _run([*compose, "up", "-d", "--no-build", "--force-recreate", "app", "caddy"])
    try:
        _wait_for_readiness(timeout_seconds)
        _external_https(domain)
    except Exception as exc:  # validator: allow-wide-except - every rollback gate is mandatory
        raise DeploymentError("rollback_not_ready") from exc


def deploy(*, allow_local_backup: bool, timeout_seconds: int) -> Path:
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise DeploymentError("production_deploy_requires_root")
    env_file = DEPLOY_DIR / "clientplatform.env"
    prepare(env_file)
    values = _env_values(env_file)
    domain = str(values.get("CLIENTPLATFORM_DOMAIN", "") or "").strip()
    if not domain or domain.endswith("your-domain.ru"):
        raise DeploymentError("production_domain_missing")

    target_sha = _git_sha()
    compose = _compose()
    _run([*compose, "config", "--quiet"])
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

    previous_image = _container_image(APP_CONTAINER)
    rollback_tag = f"{APP_IMAGE}:rollback-{_utc_stamp()}"
    _run(["docker", "image", "tag", previous_image, rollback_tag])
    changed = False
    try:
        _run([*compose, "build", "app", "backup"])
        changed = True
        _run([*compose, "up", "-d", "--force-recreate", "app", "caddy"])
        _wait_for_readiness(timeout_seconds)
        _external_https(domain)
    except Exception as deployment_error:  # validator: allow-wide-except - rollback must cover every failed gate
        if changed:
            try:
                _rollback(
                    compose=compose,
                    rollback_tag=rollback_tag,
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
                    "backup_mode": backup_mode,
                    "backup_reference": backup_reference,
                    "domain": domain,
                    "failure_class": type(deployment_error).__name__,
                    "completed_at": _completed_at(),
                }
            )
            print(f"CLIENTPLATFORM_PRODUCTION_ROLLBACK_OK:{rollback_evidence}")
        raise

    evidence = _write_evidence(
        {
            "ok": True,
            "operation": "production_deploy",
            "target_sha": target_sha,
            "previous_image": previous_image,
            "rollback_tag": rollback_tag,
            "backup_mode": backup_mode,
            "backup_reference": backup_reference,
            "domain": domain,
            "completed_at": _completed_at(),
        }
    )
    print(f"CLIENTPLATFORM_PRODUCTION_DEPLOY_OK:{evidence}")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-backup", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=240)
    args = parser.parse_args()
    timeout_seconds = max(60, min(int(args.timeout_seconds), 900))
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        deploy(
            allow_local_backup=bool(args.allow_local_backup),
            timeout_seconds=timeout_seconds,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
