from __future__ import annotations

"""Production automated acceptance runner.

The runner reuses the canonical production deployment probes instead of
maintaining a second set of host-port assumptions. Full pytest remains opt-in
for an approved maintenance window.
"""

import argparse
import json
import os
import subprocess  # nosec B404 - fixed repository-owned commands without shell
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Callable

from scripts.clientplatform_production_deploy import (
    APP_CONTAINER,
    _healthy as canonical_healthy,
    _ready as canonical_ready,
    _runtime_markers as canonical_runtime_markers,
    _sales_operations_smoke as canonical_sales_operations_smoke,
)

DEFAULT_PUBLIC_BASE_URL = "https://app.clientplatform.ru"


@dataclass(frozen=True)
class AcceptanceResult:
    name: str
    ok: bool
    detail: str


def _merged_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("APP_ENV", "prod")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if extra:
        env.update(extra)
    return env


def _run(name: str, cmd: list[str], *, timeout: int = 120) -> AcceptanceResult:
    try:
        proc = subprocess.run(  # nosec B603 - fixed argv list, shell is never used
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_merged_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return AcceptanceResult(name=name, ok=False, detail=f"{type(exc).__name__}: {exc}")
    output = (proc.stdout + proc.stderr).strip()
    tail_lines = 20 if proc.returncode == 0 else 60
    tail = "\n".join(output.splitlines()[-tail_lines:]) if output else f"exit={proc.returncode}"
    return AcceptanceResult(name=name, ok=proc.returncode == 0, detail=tail)


def _container_python(*args: str) -> list[str]:
    return ["docker", "exec", APP_CONTAINER, "python", *args]


def _canonical_bool(name: str, probe: Callable[[], bool]) -> AcceptanceResult:
    try:
        ok = probe() is True
    except Exception as exc:  # pragma: no cover - operator boundary
        return AcceptanceResult(name=name, ok=False, detail=f"{type(exc).__name__}")
    return AcceptanceResult(name=name, ok=ok, detail="canonical_probe=true" if ok else "canonical_probe=false")


def _canonical_sales() -> AcceptanceResult:
    try:
        payload = canonical_sales_operations_smoke()
    except Exception as exc:  # pragma: no cover - operator boundary
        return AcceptanceResult(name="clientplatform_sales_smoke", ok=False, detail=f"{type(exc).__name__}")
    return AcceptanceResult(
        name="clientplatform_sales_smoke",
        ok=True,
        detail=(
            f"contract_version={payload.get('contract_version')} "
            f"rollback_clean={payload.get('rollback_clean')}"
        ),
    )


def _public_root(name: str, base_url: str) -> AcceptanceResult:
    url = base_url.rstrip("/") + "/"
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            status = int(getattr(response, "status", 0) or 0)
            body = response.read(256).decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        return AcceptanceResult(name=name, ok=False, detail=f"status={exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return AcceptanceResult(name=name, ok=False, detail=f"{type(exc).__name__}")
    ok = status == 200 and body == "ClientPlatform"
    return AcceptanceResult(name=name, ok=ok, detail=f"status={status} body_exact={body == 'ClientPlatform'}")


def _method_probe(name: str, url: str, *, expected_status: int = 405, expected_allow: str = "POST") -> AcceptanceResult:
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            status = int(getattr(response, "status", 0) or 0)
            allow = response.headers.get("Allow", "")
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        allow = exc.headers.get("Allow", "") if exc.headers else ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return AcceptanceResult(name=name, ok=False, detail=f"{type(exc).__name__}")
    ok = status == expected_status and expected_allow.upper() in allow.upper()
    return AcceptanceResult(name=name, ok=ok, detail=f"status={status} allow={allow}")


def collect_results(*, include_pytest: bool = False, public_base_url: str | None = None) -> list[AcceptanceResult]:
    public_base = (public_base_url or os.getenv("CLIENTPLATFORM_PUBLIC_BOT_BASE_URL") or os.getenv("MESSENGER_PUBLIC_BASE_URL") or DEFAULT_PUBLIC_BASE_URL).rstrip("/")
    results: list[AcceptanceResult] = [
        _run(
            "compileall:project",
            [sys.executable, "-m", "compileall", "-q", "app.py", "main.py", "config", "core", "handlers", "interfaces", "keyboards", "runtime", "scripts", "services", "tests", "tools"],
            timeout=180,
        )
    ]
    if include_pytest:
        results.append(_run("pytest", [sys.executable, "-m", "pytest", "-q"], timeout=300))
    else:
        results.append(AcceptanceResult("pytest:skipped", True, "not executed on live production"))

    results.extend(
        [
            _run("prod_readiness", _container_python("scripts/prod_readiness_check.py"), timeout=120),
            _run("runtime_observability", _container_python("scripts/runtime_observability_check.py"), timeout=60),
            _canonical_bool("http:internal_health", canonical_healthy),
            _canonical_bool("http:internal_ready", canonical_ready),
            _canonical_bool("runtime:markers", canonical_runtime_markers),
            _canonical_sales(),
            _public_root("http:public_root", public_base),
            _method_probe("http:vk_webhook_get_rejected", f"{public_base}/webhooks/vk"),
            _method_probe("http:max_webhook_get_rejected", f"{public_base}/webhooks/max"),
        ]
    )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run automated production acceptance checks")
    parser.add_argument("--include-pytest", action="store_true", help="Run full pytest in an approved maintenance window")
    parser.add_argument("--public-base-url", default=None, help=f"Public backend origin (default: {DEFAULT_PUBLIC_BASE_URL})")
    args = parser.parse_args()
    results = collect_results(include_pytest=bool(args.include_pytest), public_base_url=args.public_base_url)
    print(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2))
    failed = [item for item in results if not item.ok]
    if failed:
        print("AUTOMATED ACCEPTANCE: FAILED")
        for item in failed:
            print(f"ERROR: {item.name}: {item.detail}")
        print("LIVE ACCEPTANCE: NOT PROVEN")
        return 2
    note = "pytest included" if args.include_pytest else "pytest skipped"
    print(f"AUTOMATED ACCEPTANCE: GREEN ({note})")
    print("LIVE ACCEPTANCE: NOT PROVEN")
    print("Required live journeys: Telegram owner entry, VK message, MAX message, customer booking and business payment/refund test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
