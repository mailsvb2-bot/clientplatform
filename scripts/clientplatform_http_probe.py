from __future__ import annotations

"""Bounded synthetic, replay and load smoke probes for ClientPlatform production."""

import argparse
import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_MAX_REPLAY_BYTES = 1_048_576
_MAX_REPLAY_EVENTS = 100
_MAX_LOAD_REQUESTS = 2_000
_MAX_CONCURRENCY = 32


def _request(
    url: str,
    *,
    method: str = "GET",
    payload: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, bytes, float]:
    request = Request(url=url, data=payload, headers=headers or {}, method=method)
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - operator-supplied production URL is validated by preflight
            body = response.read(1_048_576)
            return int(response.status), body, time.monotonic() - started
    except HTTPError as exc:
        body = exc.read(1_048_576)
        return int(exc.code), body, time.monotonic() - started


def _join(base: str, path: str) -> str:
    return str(base or "").rstrip("/") + "/" + str(path or "").lstrip("/")


def synthetic_journey(
    *,
    health_base_url: str,
    public_base_url: str,
    webhook_prefix: str,
    telegram_transport: str = "polling",
    timeout: float = 10.0,
) -> dict[str, Any]:
    transport = str(telegram_transport or "").strip().lower()
    if transport not in {"polling", "webhook"}:
        raise ValueError("telegram transport must be polling or webhook")

    results: dict[str, Any] = {
        "probe": "synthetic",
        "ok": True,
        "telegram_transport": transport,
    }
    for endpoint in ("healthz", "readyz"):
        status, _, elapsed = _request(_join(health_base_url, endpoint), timeout=timeout)
        results[endpoint] = {"status": status, "elapsed_ms": round(elapsed * 1000, 2)}
        if status != 200:
            results["ok"] = False

    root_status, root_body, root_elapsed = _request(
        _join(public_base_url, "/"),
        timeout=timeout,
    )
    root_body_exact = root_body.strip() == b"ClientPlatform"
    results["public_root"] = {
        "status": root_status,
        "elapsed_ms": round(root_elapsed * 1000, 2),
        "body_exact": root_body_exact,
    }
    if root_status != 200 or not root_body_exact:
        results["ok"] = False

    payload = json.dumps(
        {
            "update_id": 9_999_999_999,
            "message": {"message_id": 1, "chat": {"id": 1}, "text": "/start"},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    status, _, elapsed = _request(
        _join(public_base_url, webhook_prefix),
        method="POST",
        payload=payload,
        headers={
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": "intentionally-invalid-synthetic-secret",
        },
        timeout=timeout,
    )
    if transport == "polling":
        results["telegram_webhook_absent"] = {
            "status": status,
            "elapsed_ms": round(elapsed * 1000, 2),
        }
        if status != 404:
            results["ok"] = False
    else:
        results["invalid_webhook_secret"] = {
            "status": status,
            "elapsed_ms": round(elapsed * 1000, 2),
        }
        if status not in {401, 403}:
            results["ok"] = False
    return results


def _load_replay_events(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_size > _MAX_REPLAY_BYTES:
        raise ValueError("replay fixture exceeds 1 MiB")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("replay fixture is empty")
    if text.startswith("["):
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise ValueError("replay JSON must be an array")
        events = payload
    else:
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not events or len(events) > _MAX_REPLAY_EVENTS:
        raise ValueError("replay fixture must contain 1..100 events")
    if not all(isinstance(event, dict) for event in events):
        raise ValueError("every replay event must be an object")
    return events


def replay_webhooks(
    *,
    public_base_url: str,
    webhook_prefix: str,
    webhook_secret: str,
    fixture: Path,
    repetitions: int = 2,
    timeout: float = 10.0,
) -> dict[str, Any]:
    if len(webhook_secret) < 32:
        raise ValueError("webhook secret must contain at least 32 characters")
    if repetitions < 2 or repetitions > 5:
        raise ValueError("repetitions must be between 2 and 5")
    events = _load_replay_events(fixture)
    statuses: list[int] = []
    latencies: list[float] = []
    for _ in range(repetitions):
        for event in events:
            body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            status, _, elapsed = _request(
                _join(public_base_url, webhook_prefix),
                method="POST",
                payload=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Telegram-Bot-Api-Secret-Token": webhook_secret,
                },
                timeout=timeout,
            )
            statuses.append(status)
            latencies.append(elapsed)
    return {
        "probe": "webhook-replay",
        "ok": all(status == 200 for status in statuses),
        "events": len(events),
        "repetitions": repetitions,
        "requests": len(statuses),
        "statuses": sorted(set(statuses)),
        "max_elapsed_ms": round(max(latencies) * 1000, 2),
    }


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def load_smoke(
    *,
    health_base_url: str,
    requests: int,
    concurrency: int,
    max_p95_ms: float,
    timeout: float = 5.0,
) -> dict[str, Any]:
    if requests < 1 or requests > _MAX_LOAD_REQUESTS:
        raise ValueError("requests must be between 1 and 2000")
    if concurrency < 1 or concurrency > _MAX_CONCURRENCY:
        raise ValueError("concurrency must be between 1 and 32")
    url = _join(health_base_url, "readyz")

    def one(_: int) -> tuple[int, float]:
        try:
            status, _, elapsed = _request(url, timeout=timeout)
            return status, elapsed
        except (URLError, TimeoutError, OSError):
            return 0, timeout

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(one, range(requests)))
    statuses = [status for status, _ in results]
    latencies = [elapsed for _, elapsed in results]
    p95_ms = _percentile(latencies, 0.95) * 1000
    return {
        "probe": "load-smoke",
        "ok": all(status == 200 for status in statuses) and p95_ms <= max_p95_ms,
        "requests": requests,
        "concurrency": concurrency,
        "successes": sum(status == 200 for status in statuses),
        "p50_ms": round(statistics.median(latencies) * 1000, 2),
        "p95_ms": round(p95_ms, 2),
        "max_p95_ms": max_p95_ms,
        "statuses": sorted(set(statuses)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    synthetic = subparsers.add_parser("synthetic")
    synthetic.add_argument("--health-base-url", default="http://127.0.0.1:8182")
    synthetic.add_argument("--public-base-url", default=os.getenv("CLIENTPLATFORM_PUBLIC_BASE_URL", ""))
    synthetic.add_argument("--webhook-prefix", default=os.getenv("TELEGRAM_WEBHOOK_PREFIX", "/telegram-webhook"))
    synthetic.add_argument(
        "--telegram-transport",
        choices=("polling", "webhook"),
        default=os.getenv("TELEGRAM_TRANSPORT", "polling"),
    )

    replay = subparsers.add_parser("replay")
    replay.add_argument("fixture", type=Path)
    replay.add_argument("--public-base-url", default=os.getenv("CLIENTPLATFORM_PUBLIC_BASE_URL", ""))
    replay.add_argument("--webhook-prefix", default=os.getenv("TELEGRAM_WEBHOOK_PREFIX", "/telegram-webhook"))
    replay.add_argument("--webhook-secret", default=os.getenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", ""))
    replay.add_argument("--repetitions", type=int, default=2)

    load = subparsers.add_parser("load-smoke")
    load.add_argument("--health-base-url", default="http://127.0.0.1:8182")
    load.add_argument("--requests", type=int, default=200)
    load.add_argument("--concurrency", type=int, default=8)
    load.add_argument("--max-p95-ms", type=float, default=500.0)

    args = parser.parse_args()
    if args.command == "synthetic":
        result = synthetic_journey(
            health_base_url=args.health_base_url,
            public_base_url=args.public_base_url,
            webhook_prefix=args.webhook_prefix,
            telegram_transport=args.telegram_transport,
        )
    elif args.command == "replay":
        result = replay_webhooks(
            public_base_url=args.public_base_url,
            webhook_prefix=args.webhook_prefix,
            webhook_secret=args.webhook_secret,
            fixture=args.fixture,
            repetitions=args.repetitions,
        )
    else:
        result = load_smoke(
            health_base_url=args.health_base_url,
            requests=args.requests,
            concurrency=args.concurrency,
            max_p95_ms=args.max_p95_ms,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
