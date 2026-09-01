from __future__ import annotations

"""Strict production readiness gate."""

import argparse
import json
import os
import shlex
# Reviewed: operator production gate invokes fixed local gate commands without shell.
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
PROBE_MUTATION_AUTH_ENV = "CLIENTPLATFORM_PROBE_ALLOW_LIVE_DB_MUTATION"


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


def _merged_env(env_file: str | Path | None) -> dict[str, str]:
    merged = os.environ.copy()
    merged.update(_load_env_file(env_file))
    return merged


def _restore_target_configured(env: Mapping[str, str] | None = None) -> bool:
    values = env or os.environ
    return bool((values.get("CLIENTPLATFORM_RESTORE_DRILL_DATABASE_URL") or values.get("RESTORE_DATABASE_URL") or "").strip())


def _json_probe_name(line: str) -> str:
    text = str(line).strip()
    if not text.startswith("{"):
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("probe") or "")


def _should_drop_misplaced_probe(header: str, line: str) -> bool:
    """Drop a stale copied probe line that appears under the Telegram header."""

    if str(header).strip() != "==> Telegram live smoke":
        return False
    probe = _json_probe_name(line)
    return bool(probe and probe != "telegram_live_smoke")


def _print_clean_output(output: str) -> None:
    pending_header = ""
    for raw_line in str(output or "").splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith("==> "):
            if pending_header:
                print(pending_header, flush=True)
            pending_header = line
            continue
        if pending_header and _should_drop_misplaced_probe(pending_header, line):
            pending_header = ""
            continue
        if pending_header:
            print(pending_header, flush=True)
            pending_header = ""
        print(line, flush=True)
    if pending_header:
        print(pending_header, flush=True)


def _run(cmd: list[str], *, env: Mapping[str, str] | None = None) -> None:
    # Reviewed: commands are statically declared production gate checks and run without shell.
    proc = subprocess.run(  # nosec B603
        cmd,
        cwd=str(ROOT),
        env=dict(env or os.environ),
        text=True,
        capture_output=True,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        if output.strip():
            _print_clean_output(output)
        raise SystemExit(proc.returncode)
    _print_clean_output(output)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the non-bypassable production readiness gate")
    parser.add_argument("--env-file", default=os.getenv("CLIENTPLATFORM_ENV_FILE", "/etc/clientplatform/clientplatform.env"))
    parser.add_argument("--health-url", default=os.getenv("HEALTH_URL", "http://127.0.0.1:8082/health"))
    parser.add_argument("--ready-url", default=os.getenv("READINESS_URL", "http://127.0.0.1:8082/readyz"))
    args = parser.parse_args()
    gate_env = _merged_env(args.env_file)

    if not _restore_target_configured(gate_env):
        raise SystemExit("PRODUCTION_GATE_FAILED restore target is required")

    # This scoped authorization exists only for children of the non-bypassable
    # production gate. Standalone probe invocations still require an explicit
    # CLI flag and fail before DB access otherwise.
    gate_env[PROBE_MUTATION_AUTH_ENV] = "1"

    print("==> handler DB boundary audit", flush=True)
    _run([sys.executable, "scripts/handler_db_boundary_audit.py"], env=gate_env)

    print("==> handler exception boundary audit", flush=True)
    _run([sys.executable, "scripts/handler_exception_boundary_audit.py"], env=gate_env)

    print("==> runtime contract", flush=True)
    _run([sys.executable, "scripts/runtime_contract.py"], env=gate_env)

    _run(
        [
            sys.executable,
            "scripts/post_deploy_verify.py",
            "--env-file",
            str(args.env_file),
            "--health-url",
            str(args.health_url),
            "--ready-url",
            str(args.ready_url),
            "--skip-pytest",
            "--require-disaster-recovery-green",
            "--restore-drill",
        ],
        env=gate_env,
    )

    print("==> postgres job concurrency", flush=True)
    _run([sys.executable, "scripts/probe_postgres_job_concurrency.py"], env=gate_env)

    print("==> postgres messenger outbox concurrency", flush=True)
    _run([sys.executable, "scripts/probe_postgres_messenger_outbox.py"], env=gate_env)

    print("PRODUCTION_GATE_OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
