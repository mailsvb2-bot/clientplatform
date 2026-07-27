from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.probe_auto_audio_dry_run import run_probe
from services.probe_safety import (
    ProbeMutationAuthorizationRequired,
    mutation_authorized,
    require_live_db_mutation,
)
from services.schema import init_db

DEFAULT_USERS = 150
DEFAULT_CONCURRENCY = 16
BASE_SYNTHETIC_USER_ID = -910_001_000


def run_load_probe(
    *,
    users: int = DEFAULT_USERS,
    concurrency: int = DEFAULT_CONCURRENCY,
    slot: str = "morning",
    allow_live_db_mutation: bool,
) -> dict:
    require_live_db_mutation(bool(allow_live_db_mutation))

    users = int(users)
    concurrency = max(1, int(concurrency))
    if users <= 0:
        raise SystemExit("AUTO_AUDIO_LOAD_DRY_RUN_FAILED users must be positive")
    if users > 500:
        raise SystemExit("AUTO_AUDIO_LOAD_DRY_RUN_FAILED users limit is 500")

    init_db()

    started = time.monotonic()
    user_ids = [BASE_SYNTHETIC_USER_ID - idx for idx in range(users)]
    rows_touched = 0
    failures: list[str] = []

    with ThreadPoolExecutor(max_workers=min(concurrency, users)) as pool:
        futures = {
            pool.submit(
                run_probe,
                user_id=user_id,
                slot=slot,
                keep_artifacts=False,
                initialize_schema=False,
                allow_live_db_mutation=True,
            ): user_id
            for user_id in user_ids
        }
        for future in as_completed(futures, timeout=max(60, users * 2)):
            user_id = futures[future]
            try:
                result = future.result()
                rows_touched += int(result.rows_touched)
            except SystemExit as exc:
                failures.append(f"user_id={user_id} error={type(exc).__name__}:{exc}")

    elapsed = round(time.monotonic() - started, 3)
    payload = {
        "ok": not failures,
        "probe": "auto_audio_load_dry_run",
        "users": users,
        "concurrency": min(concurrency, users),
        "slot": slot,
        "elapsed_seconds": elapsed,
        "rows_touched": rows_touched,
        "failures": failures[:10],
    }
    if failures:
        raise SystemExit(
            "AUTO_AUDIO_LOAD_DRY_RUN_FAILED "
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="No-send auto-audio dry-run load probe")
    parser.add_argument(
        "--users",
        type=int,
        default=int(os.getenv("AUTO_AUDIO_LOAD_USERS", str(DEFAULT_USERS))),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("AUTO_AUDIO_LOAD_CONCURRENCY", str(DEFAULT_CONCURRENCY))),
    )
    parser.add_argument(
        "--slot",
        choices=("morning", "evening"),
        default=os.getenv("AUTO_AUDIO_LOAD_SLOT", "morning"),
    )
    parser.add_argument("--allow-live-db-mutation", action="store_true")
    args = parser.parse_args()

    try:
        payload = run_load_probe(
            users=int(args.users),
            concurrency=int(args.concurrency),
            slot=str(args.slot),
            allow_live_db_mutation=mutation_authorized(
                bool(args.allow_live_db_mutation)
            ),
        )
    except ProbeMutationAuthorizationRequired as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "applied": False,
                    "database_touched": False,
                    "error_code": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
