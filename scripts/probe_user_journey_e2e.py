from __future__ import annotations

"""Synthetic user-journey E2E probe.

No Telegram sends and no provider network calls. The probe does write synthetic
rows to the configured database, so mutation must be explicitly authorized. It
runs one reserved synthetic user through the local critical path:

1. demo mood session: pre-score -> demo sent/ack -> post-score;
2. synthetic YooKassa webhook replay: tokens + premium entitlement side effects;
3. paid scheduled practice: pre-score marker -> token reserve/consume -> audio marker -> post-score;
4. exact cleanup, residual verification and probe-ledger evidence.
"""

import argparse
import json
import os
import shlex
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = Path("/etc/metrotherapy/metrotherapy.env")
DEFAULT_PACKAGE_ID = "practice_personal_month"
PROBE_TYPE = "synthetic_user_journey_e2e_probe"
PROVIDER = "yookassa"
PROBE_SOURCE = "synthetic_user_journey_e2e"
PAYMENT_ID_PREFIX = "synthetic-probe-user-journey"
CLEANUP_MAX_ATTEMPTS = 5
CLEANUP_SETTLE_SECONDS = 0.5


@dataclass(frozen=True)
class UserJourneyProbeResult:
    ok: bool
    run_id: str
    user_id: int
    payment_id: str
    demo_session_id: int
    paid_session_id: int
    wallet_delta_after_payment: int
    available_tokens_after_paid_audio: int
    used_tokens_after_paid_audio: int
    entitlement_rows_delta: int
    outbox_rows_delta: int
    consultation_rows_delta: int
    demo_ack_ok: bool
    paid_reservation_reason: str
    cleanup_status: str
    residual_rows: int
    rows_touched: int
    problems: list[str]


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


def _apply_env(values: dict[str, str]) -> None:
    for key, value in values.items():
        os.environ.setdefault(str(key), str(value))


def _imports() -> dict[str, Any]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from core.time_utils import utcnow_iso
    from services.audio_anchor import pick_for_slot
    from services.db import db, mark_delivery_once, was_delivered
    from services.demo_analytics import record_demo_ack, record_demo_sent
    from services.idempotency_keys import for_pre_score, for_session
    from services.mood import create_session, get_session, mark_audio_sent, set_post, set_pre
    from services.payments.reconciliation import record_yookassa_webhook
    from services.practice_token_contract import package_by_id
    from services.practice_tokens import check_and_reserve_for_audio, finalize_audio_access, get_wallet
    from services.probe_ledger import assert_synthetic_user_id, finish_probe_run, start_probe_run
    from services.probe_safety import (
        ProbeMutationAuthorizationRequired,
        mutation_authorized,
        new_synthetic_user_id,
        require_live_db_mutation,
        safe_probe_error_code,
    )
    from services.schema import init_db

    return {
        "utcnow_iso": utcnow_iso,
        "pick_for_slot": pick_for_slot,
        "db": db,
        "mark_delivery_once": mark_delivery_once,
        "was_delivered": was_delivered,
        "record_demo_ack": record_demo_ack,
        "record_demo_sent": record_demo_sent,
        "for_pre_score": for_pre_score,
        "for_session": for_session,
        "create_session": create_session,
        "get_session": get_session,
        "mark_audio_sent": mark_audio_sent,
        "set_post": set_post,
        "set_pre": set_pre,
        "record_yookassa_webhook": record_yookassa_webhook,
        "package_by_id": package_by_id,
        "check_and_reserve_for_audio": check_and_reserve_for_audio,
        "finalize_audio_access": finalize_audio_access,
        "get_wallet": get_wallet,
        "assert_synthetic_user_id": assert_synthetic_user_id,
        "finish_probe_run": finish_probe_run,
        "start_probe_run": start_probe_run,
        "ProbeMutationAuthorizationRequired": ProbeMutationAuthorizationRequired,
        "mutation_authorized": mutation_authorized,
        "new_synthetic_user_id": new_synthetic_user_id,
        "require_live_db_mutation": require_live_db_mutation,
        "safe_probe_error_code": safe_probe_error_code,
        "init_db": init_db,
    }


def _delete_with_count(conn: Any, sql: str, params: tuple[Any, ...]) -> int:
    cur = conn.execute(sql, params)
    return max(int(getattr(cur, "rowcount", 0) or 0), 0)


def _row_count(conn: Any, sql: str, params: tuple[Any, ...]) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def _outbox_prefix(payment_id: str) -> str:
    return f"premium_delivery:{PROVIDER}:{payment_id}:"


def _cleanup_probe_rows(
    *,
    db: Any,
    assert_synthetic_user_id: Any,
    user_id: int,
    payment_id: str,
) -> int:
    assert_synthetic_user_id(int(user_id))
    uid = int(user_id)
    external_uid = str(uid)
    outbox_prefix = _outbox_prefix(payment_id)
    touched = 0
    with db() as conn:
        statements = (
            (
                "DELETE FROM consultation_requests WHERE provider=? AND provider_payment_id=?",
                (PROVIDER, payment_id),
            ),
            (
                "DELETE FROM premium_delivery_outbox WHERE substr(idempotency_key, 1, ?)=?",
                (len(outbox_prefix), outbox_prefix),
            ),
            (
                "DELETE FROM premium_entitlements WHERE provider=? AND provider_payment_id=?",
                (PROVIDER, payment_id),
            ),
            (
                "DELETE FROM payment_token_grants WHERE provider=? AND provider_payment_id=?",
                (PROVIDER, payment_id),
            ),
            (
                "DELETE FROM payments WHERE provider_charge_id=? OR telegram_charge_id=?",
                (payment_id, f"yookassa:{payment_id}"),
            ),
            (
                "DELETE FROM practice_ledger WHERE provider=? AND provider_payment_id=?",
                (PROVIDER, payment_id),
            ),
            ("DELETE FROM practice_reservations WHERE user_id=?", (uid,)),
            ("DELETE FROM payment_token_grants WHERE user_id=?", (uid,)),
            ("DELETE FROM practice_ledger WHERE user_id=?", (uid,)),
            ("DELETE FROM practice_wallets WHERE user_id=?", (uid,)),
            ("DELETE FROM user_practice_preferences WHERE user_id=?", (uid,)),
            ("DELETE FROM premium_delivery_outbox WHERE user_id=?", (uid,)),
            ("DELETE FROM premium_entitlements WHERE user_id=?", (uid,)),
            ("DELETE FROM consultation_requests WHERE user_id=?", (uid,)),
            ("DELETE FROM demo_events WHERE user_id=?", (uid,)),
            ("DELETE FROM mood_sessions WHERE user_id=?", (uid,)),
            ("DELETE FROM subscriptions WHERE user_id=?", (uid,)),
            ("DELETE FROM idempotency WHERE user_id=?", (uid,)),
            ("DELETE FROM events WHERE user_id=?", (uid,)),
            ("DELETE FROM account_audio_completions WHERE account_id=?", (uid,)),
            ("DELETE FROM account_audio_deliveries WHERE account_id=?", (uid,)),
            ("DELETE FROM account_audio_progress WHERE account_id=?", (uid,)),
            (
                "DELETE FROM account_channel_identities WHERE account_id=? OR external_user_id=?",
                (uid, external_uid),
            ),
            ("DELETE FROM accounts WHERE account_id=? OR primary_user_id=?", (uid, uid)),
            ("DELETE FROM users WHERE user_id=?", (uid,)),
        )
        for sql, params in statements:
            touched += _delete_with_count(conn, sql, params)
    return touched


def _residual_rows(
    *,
    db: Any,
    assert_synthetic_user_id: Any,
    user_id: int,
    payment_id: str,
) -> int:
    assert_synthetic_user_id(int(user_id))
    uid = int(user_id)
    external_uid = str(uid)
    outbox_prefix = _outbox_prefix(payment_id)
    with db() as conn:
        queries = (
            (
                "SELECT COUNT(*) FROM consultation_requests WHERE provider=? AND provider_payment_id=?",
                (PROVIDER, payment_id),
            ),
            (
                "SELECT COUNT(*) FROM premium_delivery_outbox WHERE substr(idempotency_key, 1, ?)=?",
                (len(outbox_prefix), outbox_prefix),
            ),
            (
                "SELECT COUNT(*) FROM premium_entitlements WHERE provider=? AND provider_payment_id=?",
                (PROVIDER, payment_id),
            ),
            (
                "SELECT COUNT(*) FROM payment_token_grants WHERE provider=? AND provider_payment_id=?",
                (PROVIDER, payment_id),
            ),
            (
                "SELECT COUNT(*) FROM payments WHERE provider_charge_id=? OR telegram_charge_id=?",
                (payment_id, f"yookassa:{payment_id}"),
            ),
            ("SELECT COUNT(*) FROM practice_reservations WHERE user_id=?", (uid,)),
            ("SELECT COUNT(*) FROM payment_token_grants WHERE user_id=?", (uid,)),
            ("SELECT COUNT(*) FROM practice_ledger WHERE user_id=?", (uid,)),
            ("SELECT COUNT(*) FROM practice_wallets WHERE user_id=?", (uid,)),
            ("SELECT COUNT(*) FROM user_practice_preferences WHERE user_id=?", (uid,)),
            ("SELECT COUNT(*) FROM premium_delivery_outbox WHERE user_id=?", (uid,)),
            ("SELECT COUNT(*) FROM premium_entitlements WHERE user_id=?", (uid,)),
            ("SELECT COUNT(*) FROM consultation_requests WHERE user_id=?", (uid,)),
            ("SELECT COUNT(*) FROM demo_events WHERE user_id=?", (uid,)),
            ("SELECT COUNT(*) FROM mood_sessions WHERE user_id=?", (uid,)),
            ("SELECT COUNT(*) FROM subscriptions WHERE user_id=?", (uid,)),
            ("SELECT COUNT(*) FROM idempotency WHERE user_id=?", (uid,)),
            ("SELECT COUNT(*) FROM events WHERE user_id=?", (uid,)),
            ("SELECT COUNT(*) FROM account_audio_completions WHERE account_id=?", (uid,)),
            ("SELECT COUNT(*) FROM account_audio_deliveries WHERE account_id=?", (uid,)),
            ("SELECT COUNT(*) FROM account_audio_progress WHERE account_id=?", (uid,)),
            (
                "SELECT COUNT(*) FROM account_channel_identities WHERE account_id=? OR external_user_id=?",
                (uid, external_uid),
            ),
            ("SELECT COUNT(*) FROM accounts WHERE account_id=? OR primary_user_id=?", (uid, uid)),
            ("SELECT COUNT(*) FROM users WHERE user_id=?", (uid,)),
        )
        total = 0
        for sql, params in queries:
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
    """Delete probe artifacts until two consecutive residual checks are zero.

    The production service can consume a synthetic outbox row while the probe is
    cleaning up and create a dependent account/delivery row just after the first
    delete transaction. A bounded settle-and-retry loop closes that race without
    weakening the residual proof or requiring the live service to be stopped.
    """

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
    wallet = get_wallet(int(user_id))
    return int(wallet.available_tokens), int(wallet.reserved_tokens), int(wallet.used_tokens)


def _payment_payload(*, payment_id: str, user_id: int, package_id: str, amount: str) -> dict[str, Any]:
    return {
        "event": "payment.succeeded",
        "object": {
            "id": payment_id,
            "status": "succeeded",
            "amount": {"value": amount, "currency": "RUB"},
            "metadata": {
                "project": "metrotherapy",
                "user_id": str(int(user_id)),
                "external_user_id": str(int(user_id)),
                "source": "telegram",
                "kind": "tokens",
                "package_id": package_id,
            },
        },
    }


def _payment_side_effect_counts(*, db: Any, payment_id: str) -> dict[str, int]:
    outbox_prefix = _outbox_prefix(payment_id)
    with db() as conn:
        return {
            "entitlements": _row_count(
                conn,
                "SELECT COUNT(*) FROM premium_entitlements WHERE provider=? AND provider_payment_id=?",
                (PROVIDER, payment_id),
            ),
            "outbox": _row_count(
                conn,
                "SELECT COUNT(*) FROM premium_delivery_outbox WHERE substr(idempotency_key, 1, ?)=?",
                (len(outbox_prefix), outbox_prefix),
            ),
            "consultation": _row_count(
                conn,
                "SELECT COUNT(*) FROM consultation_requests WHERE provider=? AND provider_payment_id=?",
                (PROVIDER, payment_id),
            ),
        }


def _safe_finish_failure(
    *,
    deps: dict[str, Any],
    run_id: str,
    rows_touched: int,
    cleanup_status: str,
    payment_id: str,
    error: BaseException,
) -> None:
    error_code = deps["safe_probe_error_code"](error)
    try:
        deps["finish_probe_run"](
            run_id=run_id,
            status="failed",
            cleanup_status=cleanup_status,
            rows_touched=rows_touched,
            error=error_code,
            evidence={"payment_id": payment_id, "error_code": error_code},
        )
    except sqlite3.Error:
        return
    except RuntimeError:
        return
    except ValueError:
        return


def _attempt_failure_cleanup(
    *,
    deps: dict[str, Any],
    user_id: int,
    payment_id: str,
    rows_touched: int,
    keep_artifacts: bool,
) -> tuple[int, str]:
    if keep_artifacts:
        return rows_touched, "kept_failed"
    try:
        cleanup_touched, residual = _cleanup_probe_rows_until_stable(
            db=deps["db"],
            assert_synthetic_user_id=deps["assert_synthetic_user_id"],
            user_id=int(user_id),
            payment_id=payment_id,
        )
        touched = rows_touched + cleanup_touched
        return touched, "clean" if residual == 0 else "residual"
    except sqlite3.Error:
        return rows_touched, "cleanup_failed"
    except RuntimeError:
        return rows_touched, "cleanup_failed"
    except ValueError:
        return rows_touched, "cleanup_failed"


def _record_failure(
    *,
    deps: dict[str, Any],
    run_id: str,
    user_id: int,
    payment_id: str,
    rows_touched: int,
    keep_artifacts: bool,
    error: BaseException,
) -> None:
    touched, cleanup_status = _attempt_failure_cleanup(
        deps=deps,
        user_id=user_id,
        payment_id=payment_id,
        rows_touched=rows_touched,
        keep_artifacts=keep_artifacts,
    )
    _safe_finish_failure(
        deps=deps,
        run_id=run_id,
        rows_touched=touched,
        cleanup_status=cleanup_status,
        payment_id=payment_id,
        error=error,
    )


def run_probe(
    *,
    user_id: int | None = None,
    keep_artifacts: bool = False,
    allow_live_db_mutation: bool,
) -> UserJourneyProbeResult:
    deps = _imports()
    deps["require_live_db_mutation"](allow_live_db_mutation)
    resolved_user_id = int(user_id) if user_id is not None else int(deps["new_synthetic_user_id"]())
    deps["assert_synthetic_user_id"](resolved_user_id)
    deps["init_db"]()

    package = deps["package_by_id"](DEFAULT_PACKAGE_ID)
    run_id = uuid.uuid4().hex
    payment_id = f"{PAYMENT_ID_PREFIX}-{run_id[:12]}"
    deps["start_probe_run"](
        probe_type=PROBE_TYPE,
        user_id=resolved_user_id,
        run_id=run_id,
        evidence={
            "payment_id": payment_id,
            "package_id": DEFAULT_PACKAGE_ID,
            "mutation_authorized": True,
        },
    )
    rows_touched = 0
    problems: list[str] = []
    cleanup_status = "not_started"

    try:
        rows_touched += _cleanup_probe_rows(
            db=deps["db"],
            assert_synthetic_user_id=deps["assert_synthetic_user_id"],
            user_id=resolved_user_id,
            payment_id=payment_id,
        )
        with deps["db"]() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users(user_id, work_time, home_time) VALUES(?,?,?)",
                (resolved_user_id, "08:30", "19:30"),
            )
        rows_touched += 1

        local_day = datetime.now(timezone.utc).date().isoformat()
        demo_audio = deps["pick_for_slot"]("morning", 0)
        if demo_audio is None:
            problems.append("no_demo_anchor")
        demo_anchor = int(getattr(demo_audio, "anchor", 0) or 0)
        demo_session_id = deps["create_session"](
            resolved_user_id,
            kind="work",
            source="demo",
            day=local_day,
            slot="morning",
            scheduled_at=f"e2e-demo:{run_id}",
            anchor_id=demo_anchor or None,
        )
        if not deps["set_pre"](demo_session_id, 1):
            problems.append("demo_pre_score_failed")
        demo_scheduled_at = deps["for_session"](demo_session_id)
        deps["mark_delivery_once"](resolved_user_id, "demo", "audio", demo_scheduled_at)
        deps["mark_audio_sent"](demo_session_id)
        if not deps["set_post"](demo_session_id, 3):
            problems.append("demo_post_score_failed")
        deps["record_demo_sent"](resolved_user_id, "work", int(demo_session_id), deps["utcnow_iso"](), 1200)
        demo_ack_ok = deps["record_demo_ack"](
            resolved_user_id,
            "work",
            int(demo_session_id),
            deps["utcnow_iso"](),
        )
        if not demo_ack_ok:
            problems.append("demo_ack_failed")
        demo_session = deps["get_session"](demo_session_id)
        if demo_session is None or int(demo_session.audio_sent) != 1:
            problems.append("demo_audio_not_marked_sent")

        before_available, _before_reserved, before_used = _wallet_tokens(
            deps["get_wallet"],
            resolved_user_id,
        )
        before_counts = _payment_side_effect_counts(db=deps["db"], payment_id=payment_id)
        payload = _payment_payload(
            payment_id=payment_id,
            user_id=resolved_user_id,
            package_id=DEFAULT_PACKAGE_ID,
            amount=f"{package.price_rub}.00",
        )
        first_payment = deps["record_yookassa_webhook"](payload)
        second_payment = deps["record_yookassa_webhook"](payload)
        if not first_payment.ok or not first_payment.inserted:
            problems.append("first_payment_not_applied")
        if not second_payment.ok or second_payment.inserted:
            problems.append("payment_not_idempotent")

        after_available, _after_reserved, _after_used = _wallet_tokens(
            deps["get_wallet"],
            resolved_user_id,
        )
        wallet_delta_after_payment = after_available - before_available
        if wallet_delta_after_payment != int(package.tokens):
            problems.append(f"wallet_delta_mismatch:{wallet_delta_after_payment}")
        after_counts = _payment_side_effect_counts(db=deps["db"], payment_id=payment_id)
        entitlement_rows_delta = after_counts["entitlements"] - before_counts["entitlements"]
        outbox_rows_delta = after_counts["outbox"] - before_counts["outbox"]
        consultation_rows_delta = after_counts["consultation"] - before_counts["consultation"]
        if entitlement_rows_delta <= 0:
            problems.append("missing_entitlement")
        if outbox_rows_delta <= 0:
            problems.append("missing_outbox")
        if consultation_rows_delta <= 0:
            problems.append("missing_consultation")

        paid_audio = deps["pick_for_slot"]("evening", 0)
        if paid_audio is None:
            problems.append("no_paid_anchor")
        paid_anchor = int(getattr(paid_audio, "anchor", 0) or 0)
        paid_scheduled_at = deps["for_pre_score"](resolved_user_id, local_day, "evening")
        deps["mark_delivery_once"](resolved_user_id, "home", "pre_score", paid_scheduled_at)
        if not deps["was_delivered"](resolved_user_id, "home", "pre_score", paid_scheduled_at):
            problems.append("paid_pre_score_marker_missing")
        paid_session_id = deps["create_session"](
            resolved_user_id,
            kind="home",
            source=PROBE_SOURCE,
            day=local_day,
            slot="evening",
            scheduled_at=paid_scheduled_at,
            anchor_id=paid_anchor or None,
        )
        if not deps["set_pre"](paid_session_id, 2):
            problems.append("paid_pre_score_failed")
        decision = deps["check_and_reserve_for_audio"](
            resolved_user_id,
            is_demo=False,
            session_id=paid_session_id,
            audio_anchor=paid_anchor or None,
        )
        paid_reservation_reason = str(decision.reason)
        if not decision.allowed or not decision.reservation_id:
            problems.append(f"paid_audio_not_reserved:{decision.reason}")
        deps["finalize_audio_access"](decision, delivered=True)
        deps["mark_delivery_once"](
            resolved_user_id,
            "home",
            "audio",
            deps["for_session"](paid_session_id),
        )
        deps["mark_audio_sent"](paid_session_id)
        if not deps["set_post"](paid_session_id, 5):
            problems.append("paid_post_score_failed")
        paid_session = deps["get_session"](paid_session_id)
        if paid_session is None or int(paid_session.audio_sent) != 1:
            problems.append("paid_audio_not_marked_sent")
        available_after_paid_audio, reserved_after_paid_audio, used_tokens_after_paid_audio = _wallet_tokens(
            deps["get_wallet"],
            resolved_user_id,
        )
        if reserved_after_paid_audio != 0:
            problems.append(f"reservation_not_consumed:{reserved_after_paid_audio}")
        if used_tokens_after_paid_audio - before_used != 1:
            problems.append(f"used_tokens_delta_mismatch:{used_tokens_after_paid_audio - before_used}")

        residual_rows = _residual_rows(
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

        result = UserJourneyProbeResult(
            ok=not problems,
            run_id=run_id,
            user_id=resolved_user_id,
            payment_id=payment_id,
            demo_session_id=int(demo_session_id),
            paid_session_id=int(paid_session_id),
            wallet_delta_after_payment=int(wallet_delta_after_payment),
            available_tokens_after_paid_audio=int(available_after_paid_audio),
            used_tokens_after_paid_audio=int(used_tokens_after_paid_audio),
            entitlement_rows_delta=int(entitlement_rows_delta),
            outbox_rows_delta=int(outbox_rows_delta),
            consultation_rows_delta=int(consultation_rows_delta),
            demo_ack_ok=bool(demo_ack_ok),
            paid_reservation_reason=paid_reservation_reason,
            cleanup_status=cleanup_status,
            residual_rows=int(residual_rows),
            rows_touched=int(rows_touched),
            problems=problems,
        )
        deps["finish_probe_run"](
            run_id=run_id,
            status="ok" if result.ok else "failed",
            cleanup_status=cleanup_status,
            rows_touched=rows_touched,
            error=";".join(problems) or None,
            evidence=asdict(result),
        )
        return result
    except sqlite3.Error as exc:
        _record_failure(
            deps=deps,
            run_id=run_id,
            user_id=resolved_user_id,
            payment_id=payment_id,
            rows_touched=rows_touched,
            keep_artifacts=keep_artifacts,
            error=exc,
        )
        raise
    except RuntimeError as exc:
        _record_failure(
            deps=deps,
            run_id=run_id,
            user_id=resolved_user_id,
            payment_id=payment_id,
            rows_touched=rows_touched,
            keep_artifacts=keep_artifacts,
            error=exc,
        )
        raise
    except ValueError as exc:
        _record_failure(
            deps=deps,
            run_id=run_id,
            user_id=resolved_user_id,
            payment_id=payment_id,
            rows_touched=rows_touched,
            keep_artifacts=keep_artifacts,
            error=exc,
        )
        raise


def _safe_failure_payload(*, applied: bool, error: BaseException, deps: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "applied": applied,
        "database_touched": applied,
        "error_code": deps["safe_probe_error_code"](error),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run synthetic demo-payment-scheduled-audio user journey probe")
    parser.add_argument("--env-file", default=os.getenv("METROTHERAPY_ENV_FILE", str(DEFAULT_ENV_FILE)))
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument("--allow-live-db-mutation", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    _apply_env(_load_env_file(args.env_file))
    deps = _imports()
    authorized = bool(deps["mutation_authorized"](bool(args.allow_live_db_mutation)))
    try:
        result = run_probe(
            user_id=int(args.user_id) if args.user_id is not None else None,
            keep_artifacts=bool(args.keep_artifacts),
            allow_live_db_mutation=authorized,
        )
    except deps["ProbeMutationAuthorizationRequired"] as exc:
        payload = {
            "ok": False,
            "applied": False,
            "database_touched": False,
            "error_code": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    except sqlite3.Error as exc:
        print(json.dumps(_safe_failure_payload(applied=True, error=exc, deps=deps), ensure_ascii=False, sort_keys=True))
        return 2
    except RuntimeError as exc:
        print(json.dumps(_safe_failure_payload(applied=True, error=exc, deps=deps), ensure_ascii=False, sort_keys=True))
        return 2
    except ValueError as exc:
        print(json.dumps(_safe_failure_payload(applied=True, error=exc, deps=deps), ensure_ascii=False, sort_keys=True))
        return 2

    payload = asdict(result)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "USER_JOURNEY_E2E_OK "
            f"user_id={result.user_id} run_id={result.run_id} cleanup={result.cleanup_status} "
            f"residual={result.residual_rows} rows_touched={result.rows_touched} "
            f"wallet_delta={result.wallet_delta_after_payment} used_tokens={result.used_tokens_after_paid_audio}"
        )
    return 0 if result.ok and result.cleanup_status in {"clean", "kept"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
