from __future__ import annotations

import json
import os
# Reviewed: observability check invokes fixed local system probes without shell.
import subprocess  # nosec B404
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _run(cmd: list[str], *, timeout: float = 10) -> tuple[int, str]:
    try:
        # Reviewed: fixed command lists for local system probes; no shell.
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)  # nosec B603
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _http_json(url: str, *, timeout: float = 5) -> tuple[bool, str, dict[str, Any] | None]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = getattr(response, "status", 0)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Ops boundary: a network probe must report degraded health instead of
        # crashing before the remaining checks can run.
        return False, f"{type(exc).__name__}: {exc}", None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    return status == 200, f"status={status}", payload


def _service_active(service: str) -> CheckResult:
    code, out = _run(["systemctl", "is-active", service])
    value = out.splitlines()[0].strip() if out else ""
    return CheckResult(f"service:{service}:active", code == 0 and value == "active", value or f"code={code}")


def _service_disabled(service: str) -> CheckResult:
    code, out = _run(["systemctl", "is-enabled", service])
    value = out.splitlines()[0].strip() if out else ""
    normalized = value.lower()

    # The old clientplatform-bot.service was a duplicate runtime hazard when it
    # existed next to clientplatform.service. If the legacy unit is absent, that is
    # also a safe state: there is no second process owner to disable or mask.
    if normalized in {"disabled", "masked", "not-found"}:
        return CheckResult(f"service:{service}:disabled", True, normalized)
    if code != 0 and ("not-found" in normalized or "no such file" in normalized):
        return CheckResult(f"service:{service}:disabled", True, "not-found")
    return CheckResult(f"service:{service}:disabled", False, value or f"code={code}")


def _main_pid(service: str) -> str:
    code, out = _run(["systemctl", "show", "-p", "MainPID", "--value", service])
    if code != 0:
        return "0"
    return out.splitlines()[0].strip() if out else "0"


def _rss_kb(pid: str) -> int:
    if not pid or pid == "0":
        return 0
    code, out = _run(["ps", "-o", "rss=", "-p", pid])
    if code != 0 or not out.strip():
        return 0
    try:
        return int(out.strip().split()[0])
    except (ValueError, IndexError):
        return 0


def _health_check() -> CheckResult:
    ok, detail, payload = _http_json("http://127.0.0.1:8082/healthz")
    if not ok or not payload:
        return CheckResult("healthz", False, detail)
    problems: list[str] = []
    if payload.get("ok") is not True:
        problems.append("ok=false")
    if payload.get("db_engine") != "postgres":
        problems.append(f"db_engine={payload.get('db_engine')}")
    if payload.get("telegram_transport") != "polling":
        problems.append(f"telegram_transport={payload.get('telegram_transport')}")
    if payload.get("telegram_webhook_enabled") is not False:
        problems.append("telegram_webhook_enabled=true")
    return CheckResult("healthz", not problems, ";".join(problems) if problems else detail)


def _runtime_memory_check() -> CheckResult:
    pid = _main_pid("clientplatform.service")
    rss = _rss_kb(pid)
    if rss <= 0:
        return CheckResult("runtime:rss", False, f"pid={pid} rss={rss}")
    return CheckResult("runtime:rss", True, f"pid={pid} rss_kb={rss}")


def _collect() -> list[CheckResult]:
    return [
        _service_active("clientplatform.service"),
        _service_disabled("clientplatform-bot.service"),
        _health_check(),
        _runtime_memory_check(),
    ]


def main() -> int:
    results = _collect()
    print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    failed = [result for result in results if not result.ok]
    if failed:
        print("RUNTIME_OBSERVABILITY: FAILED")
        return 2
    print("RUNTIME_OBSERVABILITY: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
