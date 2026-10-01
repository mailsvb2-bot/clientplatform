from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
from types import SimpleNamespace

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



class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int = -1) -> bytes:
        return self.payload


def test_no_billing_auth_configuration_is_disabled(monkeypatch):
    iam.clear_yandex_billing_iam_cache()
    monkeypatch.delenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", raising=False)
    monkeypatch.delenv("YANDEX_BILLING_AUTHORIZED_KEY_FILE", raising=False)
    monkeypatch.delenv("YANDEX_BILLING_IAM_TOKEN", raising=False)

    result = iam.get_yandex_billing_iam_token()

    assert result.configured is False
    assert result.available is False


def test_authorized_key_can_be_loaded_from_file_and_alt_field_names(monkeypatch, tmp_path):
    key_file = tmp_path / "billing-key.json"
    key_file.write_text(
        json.dumps(
            {
                "key_id": "key-file",
                "serviceAccountId": "sa-file",
                "privateKey": "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", raising=False)
    monkeypatch.setenv("YANDEX_BILLING_AUTHORIZED_KEY_FILE", str(key_file))

    key = iam._load_authorized_key()

    assert key is not None
    assert key.key_id == "key-file"
    assert key.service_account_id == "sa-file"
    assert "\\n" not in key.private_key
    assert "\nabc\n" in key.private_key


def test_authorized_key_file_and_json_parse_failures_are_bounded(monkeypatch, tmp_path):
    monkeypatch.delenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", raising=False)
    monkeypatch.setenv(
        "YANDEX_BILLING_AUTHORIZED_KEY_FILE",
        str(tmp_path / "missing.json"),
    )
    assert iam._load_authorized_key() is None

    monkeypatch.delenv("YANDEX_BILLING_AUTHORIZED_KEY_FILE", raising=False)
    monkeypatch.setenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", "{")
    assert iam._load_authorized_key() is None

    monkeypatch.setenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", "[]")
    assert iam._load_authorized_key() is None


def test_ps256_signer_invokes_openssl_and_cleans_private_tempfile(monkeypatch):
    observed = {}

    def fake_run(args, *, input, stdout, stderr, check, timeout):
        observed["args"] = args
        observed["input"] = input
        observed["path"] = args[-1]
        assert check is False
        assert timeout == 10
        return SimpleNamespace(returncode=0, stdout=b"signed", stderr=b"")

    monkeypatch.setattr(iam.subprocess, "run", fake_run)

    result = iam._sign_ps256(
        b"header.payload",
        "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
    )

    assert result == b"signed"
    assert observed["input"] == b"header.payload"
    assert "rsa_padding_mode:pss" in observed["args"]
    assert not iam.Path(observed["path"]).exists()


def test_ps256_signer_failure_is_bounded(monkeypatch):
    monkeypatch.setattr(
        iam.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b"invalid key",
        ),
    )

    try:
        iam._sign_ps256(
            b"header.payload",
            "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        )
    except RuntimeError as exc:
        assert str(exc) == "yandex_iam_ps256_sign_failed"
    else:
        raise AssertionError("expected bounded signer failure")


def test_parse_expiration_accepts_utc_and_naive_values():
    assert iam._parse_expires_at("1970-01-01T00:16:40Z") == 1000.0
    assert iam._parse_expires_at("1970-01-01T00:16:40") == 1000.0


def test_iam_exchange_success(monkeypatch):
    observed = {}

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["body"] = json.loads(request.data.decode("utf-8"))
        observed["timeout"] = timeout
        return _Response(
            b'{"iamToken":"fresh-token","expiresAt":"1970-01-01T01:00:00Z"}'
        )

    monkeypatch.setattr(iam.urllib.request, "urlopen", fake_urlopen)

    token, expires = iam._exchange_jwt("signed-jwt")

    assert token == "fresh-token"
    assert expires == 3600.0
    assert observed["url"] == "https://iam.api.cloud.yandex.net/iam/v1/tokens"
    assert observed["body"] == {"jwt": "signed-jwt"}
    assert observed["timeout"] == 10


def test_iam_exchange_normalizes_transport_errors(monkeypatch):
    failures = [
        (
            urllib.error.HTTPError(
                "https://iam.api.cloud.yandex.net/iam/v1/tokens",
                403,
                "forbidden",
                hdrs=None,
                fp=None,
            ),
            "yandex_billing_iam_http_403",
        ),
        (
            urllib.error.URLError(ConnectionRefusedError("no route")),
            "yandex_billing_iam_transport_ConnectionRefusedError",
        ),
        (TimeoutError("slow"), "yandex_billing_iam_transport_TimeoutError"),
        (OSError("down"), "yandex_billing_iam_transport_OSError"),
    ]

    for exc, expected in failures:
        def broken(*_args, _exc=exc, **_kwargs):
            raise _exc

        monkeypatch.setattr(iam.urllib.request, "urlopen", broken)
        try:
            iam._exchange_jwt("jwt")
        except RuntimeError as caught:
            assert str(caught) == expected
        else:
            raise AssertionError("expected normalized IAM transport failure")


def test_iam_exchange_rejects_oversized_and_invalid_responses(monkeypatch):
    payloads = [
        (b"x" * (256 * 1024 + 1), "yandex_billing_iam_response_too_large"),
        (b"\xff", "yandex_billing_iam_invalid_response"),
        (b"{", "yandex_billing_iam_invalid_response"),
        (b"[]", "yandex_billing_iam_invalid_response"),
        (b'{"expiresAt":"1970-01-01T01:00:00Z"}', "yandex_billing_iam_missing_token"),
    ]

    for payload, expected in payloads:
        monkeypatch.setattr(
            iam.urllib.request,
            "urlopen",
            lambda *_args, _payload=payload, **_kwargs: _Response(_payload),
        )
        try:
            iam._exchange_jwt("jwt")
        except RuntimeError as caught:
            assert str(caught) == expected
        else:
            raise AssertionError("expected invalid IAM response failure")


def test_get_token_normalizes_signer_and_unknown_runtime_failures(monkeypatch):
    iam.clear_yandex_billing_iam_cache()
    monkeypatch.setenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", _key_json())
    monkeypatch.delenv("YANDEX_BILLING_IAM_TOKEN", raising=False)

    failures = [
        (FileNotFoundError("openssl"), "yandex_billing_iam_signer_unavailable"),
        (
            subprocess.TimeoutExpired(cmd="openssl", timeout=10),
            "yandex_billing_iam_sign_timeout",
        ),
        (RuntimeError("unexpected-detail"), "yandex_billing_iam_unavailable"),
    ]

    for exc, expected in failures:
        iam.clear_yandex_billing_iam_cache()

        def broken(_key, _exc=exc):
            raise _exc

        monkeypatch.setattr(iam, "_create_jwt", broken)
        result = iam.get_yandex_billing_iam_token()
        assert result.available is False
        assert result.error_code == expected

def test_yandex_art_authorized_key_uses_dedicated_source_and_caches(monkeypatch):
    iam.clear_yandex_art_iam_cache()
    monkeypatch.setenv("YANDEX_ART_AUTHORIZED_KEY_JSON", _key_json())
    monkeypatch.delenv("YANDEX_ART_AUTHORIZED_KEY_FILE", raising=False)
    monkeypatch.delenv("YANDEX_ART_IAM_TOKEN", raising=False)
    monkeypatch.setenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", '{"id":"wrong"}')
    monkeypatch.setattr(iam, "_create_jwt", lambda key: f"jwt-{key.key_id}")
    calls = 0

    def exchange(jwt_token):
        nonlocal calls
        calls += 1
        assert jwt_token == "jwt-key-1"
        return "art-iam-token", time.time() + 3600

    monkeypatch.setattr(iam, "_exchange_jwt", exchange)

    first = iam.get_yandex_art_iam_token()
    second = iam.get_yandex_art_iam_token()

    assert first.available is True
    assert first.auth_mode == "authorized_key"
    assert first.token == "art-iam-token"
    assert second.token == "art-iam-token"
    assert calls == 1


def test_yandex_art_authorized_key_can_reuse_billing_source(monkeypatch):
    iam.clear_yandex_art_iam_cache()
    monkeypatch.delenv("YANDEX_ART_AUTHORIZED_KEY_JSON", raising=False)
    monkeypatch.delenv("YANDEX_ART_AUTHORIZED_KEY_FILE", raising=False)
    monkeypatch.delenv("YANDEX_ART_IAM_TOKEN", raising=False)
    monkeypatch.setenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", _key_json())
    monkeypatch.setattr(iam, "_create_jwt", lambda _key: "jwt-token")
    monkeypatch.setattr(
        iam,
        "_exchange_jwt",
        lambda _jwt: ("renewable-art-token", time.time() + 3600),
    )

    result = iam.get_yandex_art_iam_token()

    assert iam.yandex_art_renewable_auth_configured() is True
    assert result.available is True
    assert result.token == "renewable-art-token"


def test_yandex_art_invalid_authorized_key_fails_closed(monkeypatch):
    iam.clear_yandex_art_iam_cache()
    monkeypatch.delenv("YANDEX_ART_IAM_TOKEN", raising=False)
    monkeypatch.setenv("YANDEX_ART_AUTHORIZED_KEY_JSON", '{"id":"broken"}')
    monkeypatch.delenv("YANDEX_ART_AUTHORIZED_KEY_FILE", raising=False)

    result = iam.get_yandex_art_iam_token()

    assert result.configured is True
    assert result.available is False
    assert result.error_code == "yandex_art_invalid_authorized_key"

def _clear_art_auth_env(monkeypatch):
    for name in (
        "YANDEX_ART_IAM_TOKEN",
        "YANDEX_ART_AUTHORIZED_KEY_JSON",
        "YANDEX_ART_AUTHORIZED_KEY_FILE",
        "YANDEX_BILLING_AUTHORIZED_KEY_JSON",
        "YANDEX_BILLING_AUTHORIZED_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_yandex_art_static_iam_token_and_absent_configuration(monkeypatch):
    iam.clear_yandex_art_iam_cache()
    _clear_art_auth_env(monkeypatch)

    absent = iam.get_yandex_art_iam_token()
    assert absent.configured is False
    assert absent.available is False
    assert iam.yandex_art_renewable_auth_configured() is False

    monkeypatch.setenv("YANDEX_ART_IAM_TOKEN", "static-art-token")
    static = iam.get_yandex_art_iam_token()
    assert static.configured is True
    assert static.available is True
    assert static.auth_mode == "static_iam_token"
    assert static.token == "static-art-token"
    assert iam.yandex_art_renewable_auth_configured() is True


def test_yandex_art_iam_signer_failures_are_bounded(monkeypatch):
    iam.clear_yandex_art_iam_cache()
    _clear_art_auth_env(monkeypatch)
    monkeypatch.setenv("YANDEX_ART_AUTHORIZED_KEY_JSON", _key_json())

    failures = [
        (FileNotFoundError("openssl"), "yandex_art_iam_signer_unavailable"),
        (
            subprocess.TimeoutExpired(cmd="openssl", timeout=10),
            "yandex_art_iam_sign_timeout",
        ),
    ]
    for exc, expected in failures:
        iam.clear_yandex_art_iam_cache()

        def broken(_key, _exc=exc):
            raise _exc

        monkeypatch.setattr(iam, "_create_jwt", broken)
        result = iam.get_yandex_art_iam_token()
        assert result.configured is True
        assert result.available is False
        assert result.error_code == expected


def test_yandex_art_iam_exchange_errors_are_normalized(monkeypatch):
    iam.clear_yandex_art_iam_cache()
    _clear_art_auth_env(monkeypatch)
    monkeypatch.setenv("YANDEX_ART_AUTHORIZED_KEY_JSON", _key_json())
    monkeypatch.setattr(iam, "_create_jwt", lambda _key: "jwt-token")

    for raw, expected in (
        ("yandex_billing_iam_http_403", "yandex_art_iam_http_403"),
        ("unexpected-detail", "yandex_art_iam_unavailable"),
    ):
        iam.clear_yandex_art_iam_cache()

        def broken(_jwt, _raw=raw):
            raise RuntimeError(_raw)

        monkeypatch.setattr(iam, "_exchange_jwt", broken)
        result = iam.get_yandex_art_iam_token()
        assert result.configured is True
        assert result.available is False
        assert result.error_code == expected


def test_clear_yandex_art_iam_cache_forces_refresh(monkeypatch):
    iam.clear_yandex_art_iam_cache()
    _clear_art_auth_env(monkeypatch)
    monkeypatch.setenv("YANDEX_ART_AUTHORIZED_KEY_JSON", _key_json())
    monkeypatch.setattr(iam, "_create_jwt", lambda _key: "jwt-token")
    calls = 0

    def exchange(_jwt):
        nonlocal calls
        calls += 1
        return f"art-token-{calls}", time.time() + 3600

    monkeypatch.setattr(iam, "_exchange_jwt", exchange)
    first = iam.get_yandex_art_iam_token()
    cached = iam.get_yandex_art_iam_token()
    iam.clear_yandex_art_iam_cache()
    refreshed = iam.get_yandex_art_iam_token()

    assert first.token == cached.token == "art-token-1"
    assert refreshed.token == "art-token-2"
    assert calls == 2



def test_billing_missing_authorized_key_file_is_explicit(monkeypatch, tmp_path):
    iam.clear_yandex_billing_iam_cache()
    monkeypatch.delenv("YANDEX_BILLING_IAM_TOKEN", raising=False)
    monkeypatch.delenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", raising=False)
    monkeypatch.setenv(
        "YANDEX_BILLING_AUTHORIZED_KEY_FILE",
        str(tmp_path / "missing-authorized-key.json"),
    )

    result = iam.get_yandex_billing_iam_token()

    assert result.configured is True
    assert result.available is False
    assert result.error_code == "yandex_billing_authorized_key_file_missing"


def test_art_missing_billing_fallback_file_is_explicit(monkeypatch, tmp_path):
    iam.clear_yandex_art_iam_cache()
    _clear_art_auth_env(monkeypatch)
    monkeypatch.setenv(
        "YANDEX_BILLING_AUTHORIZED_KEY_FILE",
        str(tmp_path / "missing-authorized-key.json"),
    )

    result = iam.get_yandex_art_iam_token()

    assert result.configured is True
    assert result.available is False
    assert result.error_code == "yandex_art_authorized_key_file_missing"
