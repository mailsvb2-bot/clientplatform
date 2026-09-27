from __future__ import annotations

import base64
import json
import time

from services import yandex_iam_token as iam


def _key_json() -> str:
    return json.dumps(
        {
            "id": "key-1",
            "service_account_id": "sa-1",
            "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        }
    )


def _decode_segment(value: str) -> dict[str, object]:
    padded = value + "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def test_static_iam_token_remains_compatibility_fallback(monkeypatch):
    iam.clear_yandex_billing_iam_cache()
    monkeypatch.delenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", raising=False)
    monkeypatch.delenv("YANDEX_BILLING_AUTHORIZED_KEY_FILE", raising=False)
    monkeypatch.setenv("YANDEX_BILLING_IAM_TOKEN", "legacy-token")

    result = iam.get_yandex_billing_iam_token()

    assert result.available is True
    assert result.auth_mode == "static_iam_token"
    assert result.token == "legacy-token"


def test_invalid_authorized_key_fails_closed(monkeypatch):
    iam.clear_yandex_billing_iam_cache()
    monkeypatch.setenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", '{"id":"x"}')
    monkeypatch.delenv("YANDEX_BILLING_IAM_TOKEN", raising=False)

    result = iam.get_yandex_billing_iam_token()

    assert result.configured is True
    assert result.available is False
    assert result.error_code == "yandex_billing_invalid_authorized_key"


def test_jwt_uses_ps256_service_account_contract(monkeypatch):
    key = iam._AuthorizedKey(
        key_id="key-1",
        service_account_id="sa-1",
        private_key="-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
    )
    monkeypatch.setattr(iam, "_sign_ps256", lambda signing_input, private_key: b"sig")

    token = iam._create_jwt(key, now_epoch=1000)
    header_raw, payload_raw, signature = token.split(".")

    header = _decode_segment(header_raw)
    payload = _decode_segment(payload_raw)
    assert header == {"alg": "PS256", "kid": "key-1", "typ": "JWT"}
    assert payload["iss"] == "sa-1"
    assert payload["aud"] == "https://iam.api.cloud.yandex.net/iam/v1/tokens"
    assert payload["iat"] == 1000
    assert payload["exp"] == 4600
    assert signature == base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode("ascii")


def test_authorized_key_exchanges_and_caches_iam_token(monkeypatch):
    iam.clear_yandex_billing_iam_cache()
    monkeypatch.setenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", _key_json())
    monkeypatch.delenv("YANDEX_BILLING_IAM_TOKEN", raising=False)
    monkeypatch.setattr(iam, "_create_jwt", lambda _key: "jwt-token")
    calls = 0

    def exchange(jwt_token):
        nonlocal calls
        calls += 1
        assert jwt_token == "jwt-token"
        return "iam-token", time.time() + 3600

    monkeypatch.setattr(iam, "_exchange_jwt", exchange)

    first = iam.get_yandex_billing_iam_token()
    second = iam.get_yandex_billing_iam_token()

    assert first.available is True
    assert first.auth_mode == "authorized_key"
    assert first.token == "iam-token"
    assert second.token == "iam-token"
    assert calls == 1


def test_authorized_key_refreshes_near_expiry(monkeypatch):
    iam.clear_yandex_billing_iam_cache()
    monkeypatch.setenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", _key_json())
    monkeypatch.delenv("YANDEX_BILLING_IAM_TOKEN", raising=False)
    monkeypatch.setattr(iam, "_create_jwt", lambda _key: "jwt-token")
    calls = 0

    def exchange(_jwt):
        nonlocal calls
        calls += 1
        return f"iam-{calls}", time.time() + 120

    monkeypatch.setattr(iam, "_exchange_jwt", exchange)

    first = iam.get_yandex_billing_iam_token()
    second = iam.get_yandex_billing_iam_token()

    assert first.token == "iam-1"
    assert second.token == "iam-2"
    assert calls == 2


def test_authorized_key_exchange_failure_is_bounded(monkeypatch):
    iam.clear_yandex_billing_iam_cache()
    monkeypatch.setenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", _key_json())
    monkeypatch.delenv("YANDEX_BILLING_IAM_TOKEN", raising=False)
    monkeypatch.setattr(iam, "_create_jwt", lambda _key: "jwt-token")

    def broken(_jwt):
        raise RuntimeError("yandex_billing_iam_http_403")

    monkeypatch.setattr(iam, "_exchange_jwt", broken)

    result = iam.get_yandex_billing_iam_token()

    assert result.available is False
    assert result.error_code == "yandex_billing_iam_http_403"
    assert "jwt-token" not in repr(result)


def test_parse_expiration_falls_back_when_provider_value_is_invalid(monkeypatch):
    monkeypatch.setattr(iam.time, "time", lambda: 1000.0)

    assert iam._parse_expires_at("") == 4000.0
    assert iam._parse_expires_at("not-a-date") == 4000.0
