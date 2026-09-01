from __future__ import annotations

"""Safe ingress stress probe for ClientPlatform runtime.

Default mode deliberately avoids real user-message events and external network
traffic. It sends concurrent GET requests to local health endpoints only.

When --base-url is explicitly supplied, it also sends:
- public health GET requests;
- ignored VK/MAX POST events that must not trigger outbound messages;
- invalid JSON probes with expected 400 responses when requested.

The script uses bounded gather-based probes and does not start unmanaged
background jobs outside the canonical runtime owners.
"""

import argparse
import asyncio
import json
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ProbeResult:
    target: str
    ok: bool
    status: int
    latency_ms: float
    detail: str = ""


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((percentile / 100.0) * (len(ordered) - 1)))))
    return round(float(ordered[index]), 2)


def _request_sync(
    *,
    target: str,
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    expect_status: set[int] | None = None,
) -> ProbeResult:
    expected = expect_status or {200}
    started = time.perf_counter()
    try:
        request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        with urllib.request.urlopen(request, timeout=10) as response:
            status = int(getattr(response, "status", 0) or 0)
            raw = response.read().decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read().decode("utf-8", "replace")[:300] if exc.fp else ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        latency = (time.perf_counter() - started) * 1000.0
        return ProbeResult(
            target=target,
            ok=False,
            status=0,
            latency_ms=round(latency, 2),
            detail=f"{type(exc).__name__}: {exc}",
        )
    latency = (time.perf_counter() - started) * 1000.0
    return ProbeResult(target=target, ok=status in expected, status=status, latency_ms=round(latency, 2), detail=raw)


async def _bounded_probe(sem: asyncio.Semaphore, **kwargs: Any) -> ProbeResult:
    async with sem:
        return await asyncio.to_thread(_request_sync, **kwargs)


def _vk_ignored_payload(i: int) -> bytes:
    return json.dumps({"type": "group_join", "object": {"user_id": 900000000 + i}}, ensure_ascii=False).encode("utf-8")


def _max_ignored_payload(i: int) -> bytes:
    return json.dumps({"update_type": "bot_started", "marker": f"stress-{i}"}, ensure_ascii=False).encode("utf-8")


def _summarize(results: list[ProbeResult]) -> dict[str, Any]:
    by_target: dict[str, list[ProbeResult]] = {}
    for item in results:
        by_target.setdefault(item.target, []).append(item)

    summary: dict[str, Any] = {}
    for target, items in sorted(by_target.items()):
        latencies = [item.latency_ms for item in items]
        status_counts: dict[str, int] = {}
        for item in items:
            status_counts[str(item.status)] = status_counts.get(str(item.status), 0) + 1
        failed = [item for item in items if not item.ok]
        summary[target] = {
            "total": len(items),
            "ok": len(items) - len(failed),
            "failed": len(failed),
            "status_counts": status_counts,
            "avg_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
            "p50_ms": _percentile(latencies, 50),
            "p95_ms": _percentile(latencies, 95),
            "p99_ms": _percentile(latencies, 99),
            "max_ms": round(max(latencies), 2) if latencies else 0.0,
            "sample_failure": asdict(failed[0]) if failed else None,
        }
    return summary


def _probe_specs(args: argparse.Namespace) -> list[dict[str, Any]]:
    base = (args.base_url or "").strip().rstrip("/")
    headers = {"Content-Type": "application/json"}
    specs: list[dict[str, Any]] = []
    for i in range(args.requests):
        specs.append({"target": "local_health", "method": "GET", "url": args.local_health_url, "expect_status": {200}})
        if args.local_webhook_health_url:
            specs.append({"target": "local_webhook_health", "method": "GET", "url": args.local_webhook_health_url, "expect_status": {200}})
        if not base:
            continue
        specs.append({"target": "public_health", "method": "GET", "url": f"{base}/healthz", "expect_status": {200}})
        specs.append({
            "target": "vk_ignored_post",
            "method": "POST",
            "url": f"{base}/webhooks/vk",
            "body": _vk_ignored_payload(i),
            "headers": headers,
            "expect_status": {200},
        })
        specs.append({
            "target": "max_ignored_post",
            "method": "POST",
            "url": f"{base}/webhooks/max",
            "body": _max_ignored_payload(i),
            "headers": headers,
            "expect_status": {200},
        })
        if args.include_invalid_json:
            specs.append({
                "target": "vk_invalid_json",
                "method": "POST",
                "url": f"{base}/webhooks/vk",
                "body": b"{bad-json",
                "headers": headers,
                "expect_status": {400},
            })
            specs.append({
                "target": "max_invalid_json",
                "method": "POST",
                "url": f"{base}/webhooks/max",
                "body": b"{bad-json",
                "headers": headers,
                "expect_status": {400},
            })
    return specs


async def run(args: argparse.Namespace) -> int:
    sem = asyncio.Semaphore(args.concurrency)
    specs = _probe_specs(args)
    started = time.perf_counter()
    results = await asyncio.gather(*(_bounded_probe(sem, **spec) for spec in specs))
    elapsed = round(time.perf_counter() - started, 2)
    summary = _summarize(list(results))
    failed_targets = {target: data for target, data in summary.items() if data["failed"]}
    report = {
        "ok": not failed_targets,
        "elapsed_sec": elapsed,
        "requests_per_target": args.requests,
        "concurrency": args.concurrency,
        "public_targets_enabled": bool((args.base_url or "").strip()),
        "targets": summary,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failed_targets else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="", help="Optional public base URL. Empty means local health checks only.")
    parser.add_argument("--local-health-url", default="http://127.0.0.1:8082/healthz")
    parser.add_argument("--local-webhook-health-url", default="http://127.0.0.1:8081/healthz")
    parser.add_argument("--requests", type=int, default=25, help="Requests per enabled target")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--include-invalid-json", action="store_true")
    args = parser.parse_args()
    if args.requests <= 0:
        raise SystemExit("--requests must be > 0")
    if args.concurrency <= 0:
        raise SystemExit("--concurrency must be > 0")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
