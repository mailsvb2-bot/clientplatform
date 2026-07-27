from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()
PROBE = ROOT / "scripts/probe_user_journey_e2e.py"


def replace_once(old: str, new: str) -> None:
    text = PROBE.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"exact patch mismatch matches={count}")
    PROBE.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "import sqlite3\nimport sys\nimport uuid\n",
    "import sqlite3\nimport sys\nimport time\nimport uuid\n",
)
replace_once(
    'PAYMENT_ID_PREFIX = "synthetic-probe-user-journey"\n',
    'PAYMENT_ID_PREFIX = "synthetic-probe-user-journey"\n'
    'CLEANUP_MAX_ATTEMPTS = 5\n'
    'CLEANUP_SETTLE_SECONDS = 0.5\n',
)
replace_once(
    """        for sql, params in queries:
            total += _row_count(conn, sql, params)
        return total


def _wallet_tokens(get_wallet: Any, user_id: int) -> tuple[int, int, int]:
""",
    """        for sql, params in queries:
            total += _row_count(conn, sql, params)
        return total


def _cleanup_probe_rows_until_stable(
    *,
    db: Any,
    assert_synthetic_user_id: Any,
    user_id: int,
    payment_id: str,
    max_attempts: int = CLEANUP_MAX_ATTEMPTS,
    settle_seconds: float = CLEANUP_SETTLE_SECONDS,
) -> tuple[int, int]:
    \"\"\"Delete probe artifacts until two consecutive residual checks are zero.

    The production service can consume a synthetic outbox row while the probe is
    cleaning up and create a dependent account/delivery row just after the first
    delete transaction. A bounded settle-and-retry loop closes that race without
    weakening the residual proof or requiring the live service to be stopped.
    \"\"\"

    assert_synthetic_user_id(int(user_id))
    attempts = max(1, int(max_attempts))
    delay = max(0.0, float(settle_seconds))
    total_touched = 0
    residual = -1

    for attempt in range(attempts):
        total_touched += _cleanup_probe_rows(
            db=db,
            assert_synthetic_user_id=assert_synthetic_user_id,
            user_id=int(user_id),
            payment_id=payment_id,
        )
        residual = _residual_rows(
            db=db,
            assert_synthetic_user_id=assert_synthetic_user_id,
            user_id=int(user_id),
            payment_id=payment_id,
        )
        if residual == 0:
            if delay <= 0:
                return total_touched, 0
            time.sleep(delay)
            residual = _residual_rows(
                db=db,
                assert_synthetic_user_id=assert_synthetic_user_id,
                user_id=int(user_id),
                payment_id=payment_id,
            )
            if residual == 0:
                return total_touched, 0
        if attempt + 1 < attempts and delay > 0:
            time.sleep(delay)

    return total_touched, max(0, int(residual))


def _wallet_tokens(get_wallet: Any, user_id: int) -> tuple[int, int, int]:
""",
)
replace_once(
    """        touched = rows_touched + _cleanup_probe_rows(
            db=deps["db"],
            assert_synthetic_user_id=deps["assert_synthetic_user_id"],
            user_id=int(user_id),
            payment_id=payment_id,
        )
        residual = _residual_rows(
            db=deps["db"],
            assert_synthetic_user_id=deps["assert_synthetic_user_id"],
            user_id=int(user_id),
            payment_id=payment_id,
        )
        return touched, "clean" if residual == 0 else "residual"
""",
    """        cleanup_touched, residual = _cleanup_probe_rows_until_stable(
            db=deps["db"],
            assert_synthetic_user_id=deps["assert_synthetic_user_id"],
            user_id=int(user_id),
            payment_id=payment_id,
        )
        touched = rows_touched + cleanup_touched
        return touched, "clean" if residual == 0 else "residual"
""",
)
replace_once(
    """        residual_rows = _residual_rows(
            db=deps["db"],
            assert_synthetic_user_id=deps["assert_synthetic_user_id"],
            user_id=resolved_user_id,
            payment_id=payment_id,
        )
        cleanup_status = "kept"
        if not keep_artifacts:
            rows_touched += _cleanup_probe_rows(
                db=deps["db"],
                assert_synthetic_user_id=deps["assert_synthetic_user_id"],
                user_id=resolved_user_id,
                payment_id=payment_id,
            )
            residual_rows = _residual_rows(
                db=deps["db"],
                assert_synthetic_user_id=deps["assert_synthetic_user_id"],
                user_id=resolved_user_id,
                payment_id=payment_id,
            )
            cleanup_status = "clean" if residual_rows == 0 else "residual"
            if residual_rows:
                problems.append(f"cleanup_residual_rows:{residual_rows}")
""",
    """        residual_rows = _residual_rows(
            db=deps["db"],
            assert_synthetic_user_id=deps["assert_synthetic_user_id"],
            user_id=resolved_user_id,
            payment_id=payment_id,
        )
        cleanup_status = "kept"
        if not keep_artifacts:
            cleanup_touched, residual_rows = _cleanup_probe_rows_until_stable(
                db=deps["db"],
                assert_synthetic_user_id=deps["assert_synthetic_user_id"],
                user_id=resolved_user_id,
                payment_id=payment_id,
            )
            rows_touched += cleanup_touched
            cleanup_status = "clean" if residual_rows == 0 else "residual"
            if residual_rows:
                problems.append(f"cleanup_residual_rows:{residual_rows}")
""",
)
