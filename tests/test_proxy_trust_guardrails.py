from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import settings as settings_module
from runtime import payment_webhook_admission as admission


def test_global_proxy_trust_without_cidrs_does_not_trust_public_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    monkeypatch.delenv("PAYMENT_WEBHOOK_TRUSTED_PROXY_CIDRS", raising=False)
    request = SimpleNamespace(
        remote="198.51.100.20",
        headers={"X-Forwarded-For": "203.0.113.10"},
    )

    assert admission._client_key(request) == "peer:198.51.100.20"


def test_prod_proxy_trust_requires_cidr_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    monkeypatch.delenv("PAYMENT_WEBHOOK_TRUSTED_PROXY_CIDRS", raising=False)

    with pytest.raises(
        SystemExit,
        match="requires PAYMENT_WEBHOOK_TRUSTED_PROXY_CIDRS",
    ):
        settings_module._validate_trusted_proxy_env()


def test_prod_proxy_trust_rejects_invalid_cidr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    monkeypatch.setenv("PAYMENT_WEBHOOK_TRUSTED_PROXY_CIDRS", "not-a-network")

    with pytest.raises(SystemExit, match="invalid networks"):
        settings_module._validate_trusted_proxy_env()


def test_prod_proxy_trust_accepts_explicit_cidrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    monkeypatch.setenv(
        "PAYMENT_WEBHOOK_TRUSTED_PROXY_CIDRS",
        "10.20.0.0/16,2001:db8:1::/64",
    )

    settings_module._validate_trusted_proxy_env()
