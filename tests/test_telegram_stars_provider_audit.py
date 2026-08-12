from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from scripts import telegram_stars_provider_audit as audit


ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = ROOT / "scripts" / "telegram_stars_provider_audit.py"


def _explicit_prices(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_STARS_PRICING_MODE", "explicit")
    monkeypatch.setenv("TELEGRAM_STARS_PRICE_PRACTICE_START_7", "1500")
    monkeypatch.setenv("TELEGRAM_STARS_PRICE_PRACTICE_60", "2500")
    monkeypatch.setenv("TELEGRAM_STARS_PRICE_PRACTICE_ANTISTRESS_60", "5000")
    monkeypatch.setenv("TELEGRAM_STARS_PRICE_PRACTICE_PERSONAL_MONTH", "15000")


def test_provider_audit_uses_native_xtr_and_reports_price_ladder(monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:test-token")
    _explicit_prices(monkeypatch)
    calls: list[tuple[str, dict | None]] = []

    def fake_api_call(_token: str, method: str, payload: dict | None = None):
        calls.append((method, payload))
        if method == "getMe":
            return {"ok": True, "result": {"username": "MetrotherapyBot"}}
        return {"ok": True, "result": "https://t.me/$audit-link"}

    monkeypatch.setattr(audit, "_api_call", fake_api_call)
    message, code = audit.run()

    assert code == 0
    assert message == (
        "status=ok stage=createInvoiceLink bot=@MetrotherapyBot code=200 "
        "error=NONE prices=1500,2500,5000,15000"
    )
    assert calls[1][0] == "createInvoiceLink"
    payload = calls[1][1]
    assert payload is not None
    assert payload["currency"] == "XTR"
    assert payload["prices"] == [{"label": "Audit", "amount": 1}]
    assert "provider_token" not in payload


def test_provider_audit_reports_provider_account_invalid_without_secret(monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:secret-token")
    _explicit_prices(monkeypatch)

    def fake_api_call(_token: str, method: str, payload: dict | None = None):
        del payload
        if method == "getMe":
            return {"ok": True, "result": {"username": "MetrotherapyBot"}}
        return {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: PROVIDER_ACCOUNT_INVALID",
        }

    monkeypatch.setattr(audit, "_api_call", fake_api_call)
    message, code = audit.run()

    assert code == 4
    assert "bot=@MetrotherapyBot" in message
    assert "error=PROVIDER_ACCOUNT_INVALID" in message
    assert "prices=1500,2500,5000,15000" in message
    assert "secret-token" not in message


def test_provider_audit_fails_closed_on_invalid_price_ladder(monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:test-token")
    monkeypatch.setenv("TELEGRAM_STARS_PRICING_MODE", "explicit")
    monkeypatch.setenv("TELEGRAM_STARS_PRICE_PRACTICE_START_7", "0")

    message, code = audit.run()

    assert code == 6
    assert message == "status=error stage=prices bot=unknown code=0 error=INVALID_PRICE_LADDER"


def test_provider_audit_runs_by_absolute_path_outside_repo() -> None:
    env = os.environ.copy()
    env.pop("BOT_TOKEN", None)

    result = subprocess.run(
        [sys.executable, str(AUDIT_SCRIPT)],
        cwd="/tmp",
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout.strip() == "status=error stage=config bot=unknown code=0 error=BOT_TOKEN_MISSING"
    assert result.stderr == ""
