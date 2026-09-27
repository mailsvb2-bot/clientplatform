from __future__ import annotations

from decimal import Decimal
from services import yandex_billing_health as billing


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int = -1) -> bytes:
        return self.payload


def test_billing_snapshot_is_disabled_without_read_only_credentials(monkeypatch):
    monkeypatch.delenv("YANDEX_BILLING_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("YANDEX_BILLING_IAM_TOKEN", raising=False)

    snapshot = billing.get_yandex_billing_snapshot()

    assert snapshot.configured is False
    assert snapshot.available is False


def test_billing_snapshot_reads_real_balance_without_exposing_token(monkeypatch):
    monkeypatch.setenv("YANDEX_BILLING_ACCOUNT_ID", "billing-1")
    monkeypatch.setenv("YANDEX_BILLING_IAM_TOKEN", "secret-token")
    observed = {}

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["authorization"] = request.headers.get("Authorization")
        observed["timeout"] = timeout
        return _Response(
            b'{"id":"billing-1","currency":"RUB","active":true,"balance":"3993.42"}'
        )

    monkeypatch.setattr(billing.urllib.request, "urlopen", fake_urlopen)

    snapshot = billing.get_yandex_billing_snapshot()

    assert snapshot.configured is True
    assert snapshot.available is True
    assert snapshot.active is True
    assert snapshot.balance == Decimal("3993.42")
    assert snapshot.currency == "RUB"
    assert observed["url"].endswith("/billing/v1/billingAccounts/billing-1")
    assert observed["authorization"] == "Bearer secret-token"
    assert "secret-token" not in repr(snapshot)


def test_balance_threshold_only_fires_on_downward_crossing(monkeypatch):
    monkeypatch.setenv("YANDEX_BILLING_ALERT_THRESHOLDS", "5000,2000,1000,500,100,0")

    assert billing.crossed_balance_threshold(Decimal("450"), Decimal("1500")) == Decimal("500")
    assert billing.crossed_balance_threshold(Decimal("450"), Decimal("400")) is None
    assert billing.crossed_balance_threshold(Decimal("6000"), None) is None
