from __future__ import annotations

"""Post-deploy verification bundle for the ClientPlatform service.

This script is intentionally explicit and conservative. It combines the checks
that were previously run manually after deploy into one repeatable command:

- optional pytest run;
- production validator;
- smoke bootstrap;
- storage/legacy SQLite ambiguity audit;
- disaster-recovery backup status;
- DB-backed durable-job/idempotency probe;
- canonical ClientPlatform transactional sales smoke with rollback proof;
- live Telegram Bot API transport smoke without user impersonation;
- optional Postgres restore drill;
- local health/readiness HTTP probes.

It does not modify systemd units, does not contact payment providers, does not delete
legacy SQLite files, and does not send Telegram messages unless explicitly asked
with --telegram-live-send.
"""

import argparse
import json
import os
import shlex
# Reviewed: operator post-deploy verifier invokes fixed local probes without shell.
import subprocess  # nosec B404
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = Path("/etc/clientplatform/clientplatform.env")


def _load_env_file(path: str | Path | None) -> dict[str, str]:
    if not path:
        return {}
    env_path = Path(path)
    if not env_path.exists():
        return {}
    loaded: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        try:
            parts = shlex.split(value, posix=True)
            loaded[key] = parts[0] if len(parts) == 1 else value
        except ValueError:
            loaded[key] = value.strip('"').strip("'")
    return loaded


def _run(cmd: list[str], *, env: Mapping[str, str] | None = None) -> str:
    merged_env = os.environ.copy()
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})
    # Reviewed: command lists are fixed local verification probes executed without shell.
    proc = subprocess.run(  # nosec B603
        cmd,
        cwd=str(ROOT),
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SystemExit(
            "POST_DEPLOY_VERIFY_FAILED command="
            + " ".join(cmd)
            + f" exit={proc.returncode}\n"
            + output.strip()
        )
    return output.strip()


def _decode_error_body(exc: urllib.error.HTTPError) -> str:
    return exc.read().decode("utf-8", errors="replace")


def _truncate(value: str, *, limit: int = 1000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...<truncated>"


def _parse_json_body(*, url: str, body: str) -> dict:
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED url={url} invalid_json={_truncate(body, limit=300)}") from exc


def _parse_command_json(*, command_name: str, output: str) -> dict:
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"POST_DEPLOY_VERIFY_FAILED command={command_name} invalid_json={_truncate(output, limit=500)}"
        ) from exc


def _with_path(url: str, path: str) -> str:
    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _alias_urls(url: str, *, aliases: tuple[str, ...]) -> list[str]:
    urls = [str(url)]
    for alias in aliases:
        candidate = _with_path(str(url), alias)
        if candidate not in urls:
            urls.append(candidate)
    return urls


def _http_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # nosec B310
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = _decode_error_body(exc)
        payload = _parse_json_body(url=url, body=body) if body.strip().startswith("{") else None
        if payload is not None:
            raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED url={url} status={exc.code} payload={payload}") from exc
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED url={url} status={exc.code} body={_truncate(body)}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED url={url} err={exc}") from exc
    payload = _parse_json_body(url=url, body=body)
    if payload.get("ok") is not True:
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED url={url} payload={payload}")
    return payload


def _http_json_any(urls: list[str]) -> tuple[dict, str]:
    errors: list[str] = []
    for url in urls:
        try:
            return _http_json(url), url
        except SystemExit as exc:
            errors.append(str(exc))
    raise SystemExit("POST_DEPLOY_VERIFY_FAILED all_probe_urls_failed\n" + "\n".join(errors))


def _verify_sales_smoke_output(output: str) -> dict:
    prefix = "CLIENTPLATFORM_SALES_PRODUCTION_SMOKE_OK:"
    line = next((item.strip() for item in reversed(output.splitlines()) if item.strip().startswith(prefix)), "")
    if not line:
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED sales_smoke missing_success_marker output={_truncate(output, limit=500)}")
    try:
        payload = json.loads(line[len(prefix):])
    except json.JSONDecodeError as exc:
        raise SystemExit("POST_DEPLOY_VERIFY_FAILED sales_smoke invalid_json") from exc
    if payload.get("ok") is not True or payload.get("rollback_clean") is not True:
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED sales_smoke payload={payload}")
    residue = payload.get("residue")
    if not isinstance(residue, dict) or any(int(value or 0) for value in residue.values()):
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED sales_smoke residue={residue}")
    return {
        "ok": True,
        "probe": "clientplatform_sales_transactional",
        "contract_version": payload.get("contract_version"),
        "rollback_clean": True,
        "checks": payload.get("checks") or {},
    }


def _verify_telegram_live_smoke(payload: dict) -> dict:
    if payload.get("ok") is not True:
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED telegram_live_smoke payload={payload}")
    if not payload.get("bot_id"):
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED telegram_live_smoke missing_bot_id payload={payload}")
    if not str(payload.get("bot_username") or "").strip():
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED telegram_live_smoke missing_username payload={payload}")
    if payload.get("transport") != "polling":
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED telegram_live_smoke transport_must_be_polling payload={payload}")
    if payload.get("webhook_url_present") is True:
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED telegram_live_smoke webhook_conflict payload={payload}")
    return {
        "ok": True,
        "probe": "telegram_live_smoke",
        "bot_username": payload.get("bot_username"),
        "transport": payload.get("transport"),
        "webhook_url_present": payload.get("webhook_url_present"),
        "pending_update_count": payload.get("pending_update_count"),
        "send_checked": payload.get("send_checked"),
        "cleanup_status": payload.get("cleanup_status"),
    }


def _verify_disaster_recovery_status(payload: dict, *, require_green: bool = False) -> dict:
    status = str(payload.get("status") or "")
    ok = payload.get("ok") is True
    if require_green and status != "GREEN":
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED disaster_recovery_status payload={payload}")
    return {
        "ok": ok,
        "probe": "disaster_recovery_status",
        "status": status,
        "reason": payload.get("reason"),
        "backup_count": payload.get("backup_count"),
        "latest_backup_size_bytes": payload.get("latest_backup_size_bytes"),
        "restore_target_configured": payload.get("restore_target_configured"),
    }


def _verify_storage_audit(payload: dict) -> dict:
    if payload.get("ok") is not True:
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED storage_audit payload={payload}")
    if payload.get("active_engine") != "postgres":
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED storage_audit active_engine={payload.get('active_engine')}")
    if payload.get("repo_local_sqlite_present") is True:
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED storage_audit repo_local_sqlite_present payload={payload}")
    if payload.get("disallowed_direct_sqlite_connects"):
        raise SystemExit(f"POST_DEPLOY_VERIFY_FAILED storage_audit disallowed_direct_sqlite_connects payload={payload}")
    return {
        "ok": True,
        "probe": "storage_legacy_audit",
        "status": payload.get("status"),
        "active_engine": payload.get("active_engine"),
        "legacy_sqlite_present": payload.get("legacy_sqlite_present"),
        "repo_local_sqlite_present": payload.get("repo_local_sqlite_present"),
        "direct_sqlite_connects": len(payload.get("direct_sqlite_connects") or []),
        "disallowed_direct_sqlite_connects": len(payload.get("disallowed_direct_sqlite_connects") or []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run repeatable post-deploy proof checks")
    parser.add_argument("--skip-pytest", action="store_true", help="Skip pytest for faster repeated local checks")
    parser.add_argument("--skip-sales-smoke", action="store_true", help="Skip the canonical transactional ClientPlatform sales proof")
    parser.add_argument("--skip-telegram-live-smoke", action="store_true", help="Skip live Telegram Bot API reachability smoke")
    parser.add_argument("--telegram-live-send", action="store_true", help="Send and delete a harmless test message to TELEGRAM_LIVE_SMOKE_CHAT_ID/TEST_CHAT_ID")
    parser.add_argument("--telegram-live-chat-id", default=os.getenv("TELEGRAM_LIVE_SMOKE_CHAT_ID", os.getenv("TEST_CHAT_ID", "")))
    parser.add_argument("--skip-storage-audit", action="store_true", help="Skip the storage/legacy SQLite ambiguity audit")
    parser.add_argument("--skip-disaster-recovery-status", action="store_true", help="Skip backup/disaster-recovery status summary")
    parser.add_argument("--require-disaster-recovery-green", action="store_true", help="Fail unless backup status and restore target are GREEN")
    parser.add_argument("--restore-drill", action="store_true", help="Run postgres_restore_drill.py --latest as part of the bundle")
    parser.add_argument("--env-file", default=os.getenv("CLIENTPLATFORM_ENV_FILE", str(DEFAULT_ENV_FILE)))
    parser.add_argument("--health-url", default=os.getenv("HEALTH_URL", "http://127.0.0.1:8182/health"))
    parser.add_argument("--ready-url", default=os.getenv("READINESS_URL", "http://127.0.0.1:8182/readyz"))
    args = parser.parse_args()

    service_env = _load_env_file(args.env_file)
    if service_env:
        print(f"==> loaded env file: {args.env_file} ({len(service_env)} keys)", flush=True)
    else:
        print(f"==> env file not loaded or empty: {args.env_file}", flush=True)

    if not args.skip_pytest:
        print("==> pytest", flush=True)
        print(_run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], env=service_env))

    strict_env = {
        **service_env,
        "APP_ENV": "prod",
        "VALIDATOR_RELEASE_MODE": "1",
        "VALIDATOR_GUARDRAILS_STRICT": "1",
    }

    print("==> prod validator", flush=True)
    print(_run([sys.executable, "scripts/validate_project.py"], env=strict_env))

    print("==> smoke", flush=True)
    print(_run([sys.executable, "scripts/smoke.py"], env=strict_env))

    if not args.skip_storage_audit:
        print("==> storage legacy audit", flush=True)
        storage_output = _run([sys.executable, "scripts/storage_legacy_audit.py", "--json", "--strict"], env=service_env)
        print(json.dumps(_verify_storage_audit(_parse_command_json(command_name="storage legacy audit", output=storage_output)), ensure_ascii=False))

    if not args.skip_disaster_recovery_status:
        print("==> disaster recovery status", flush=True)
        recovery_output = _run([sys.executable, "scripts/disaster_recovery_status.py", "--json"], env=service_env)
        print(
            json.dumps(
                _verify_disaster_recovery_status(
                    _parse_command_json(command_name="disaster recovery status", output=recovery_output),
                    require_green=bool(args.require_disaster_recovery_green),
                ),
                ensure_ascii=False,
            )
        )

    print("==> durable job pipeline probe", flush=True)
    print(_run([sys.executable, "scripts/probe_scheduler_job_live.py"], env=service_env))

    if not args.skip_sales_smoke:
        print("==> ClientPlatform transactional sales smoke", flush=True)
        sales_output = _run(
            [sys.executable, "scripts/clientplatform_sales_production_smoke.py"],
            env=service_env,
        )
        print(json.dumps(_verify_sales_smoke_output(sales_output), ensure_ascii=False))

    if not args.skip_telegram_live_smoke:
        print("==> Telegram live smoke", flush=True)
        telegram_cmd = [sys.executable, "scripts/probe_telegram_live_smoke.py", "--json"]
        if args.telegram_live_send:
            telegram_cmd.append("--allow-send")
            if str(args.telegram_live_chat_id or "").strip():
                telegram_cmd.extend(["--chat-id", str(args.telegram_live_chat_id)])
        telegram_output = _run(telegram_cmd, env=service_env)
        print(json.dumps(_verify_telegram_live_smoke(_parse_command_json(command_name="Telegram live smoke", output=telegram_output)), ensure_ascii=False))

    if args.restore_drill:
        print("==> postgres restore drill", flush=True)
        print(_run([sys.executable, "scripts/postgres_restore_drill.py", "--latest"], env=service_env))

    print("==> health", flush=True)
    health, health_url = _http_json_any(_alias_urls(str(args.health_url), aliases=("/health", "/healthz")))
    print(
        json.dumps(
            {"ok": health.get("ok"), "probe": health.get("probe"), "db_engine": health.get("db_engine"), "url": health_url},
            ensure_ascii=False,
        )
    )

    print("==> ready", flush=True)
    ready, ready_url = _http_json_any(_alias_urls(str(args.ready_url), aliases=("/readyz", "/ready")))
    print(
        json.dumps(
            {
                "ok": ready.get("ok"),
                "probe": ready.get("probe"),
                "db_ready": ready.get("db_ready"),
                "schema_ready": ready.get("schema_ready"),
                "clientplatform_dispatch_ready": ready.get("clientplatform_dispatch_ready"),
                "webhook_ready": ready.get("webhook_ready"),
                "url": ready_url,
            },
            ensure_ascii=False,
        )
    )

    print("POST_DEPLOY_VERIFY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
