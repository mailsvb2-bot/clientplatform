from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import Any

import pytest

from scripts import register_max_webhook as registration


def _config() -> registration.MaxWebhookConfig:
    return registration.MaxWebhookConfig(
        api_base_url=registration.MAX_PLATFORM_API_BASE_URL,
        token="bot-secret-token",
        public_base_url="https://bot.example.test",
        secret="webhook_secret-1",
    )


def _set_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "bot-secret-token")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "webhook_secret-1")
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example.test")
    monkeypatch.delenv("MAX_API_BASE_URL", raising=False)
    monkeypatch.delenv("MAX_CA_BUNDLE", raising=False)


def test_default_cli_is_network_free_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_valid_env(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["register_max_webhook.py"])

    def bomb(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry-run must not call provider network")

    monkeypatch.setattr(registration, "_json_request", bomb)

    assert registration.main() == 0
    payload = json.loads(capsys.readouterr().out)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["ok"] is True
    assert payload["mode"] == "dry_run"
    assert payload["applied"] is False
    assert payload["network_called"] is False
    assert payload["webhook_url"] == "https://bot.example.test/webhooks/max"
    assert "bot-secret-token" not in serialized
    assert "webhook_secret-1" not in serialized


def test_apply_skips_duplicate_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config()
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(
        url: str,
        *,
        token: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        ca_bundle: str = "",
        timeout_sec: int = 30,
    ) -> registration.MaxApiResponse:
        calls.append((url, method, payload))
        assert token == cfg.token
        if url.endswith("/me"):
            return registration.MaxApiResponse(
                200,
                {"user_id": 1, "username": "clientplatform", "is_bot": True},
            )
        return registration.MaxApiResponse(
            200,
            {"subscriptions": [{"url": cfg.webhook_url}]},
        )

    monkeypatch.setattr(registration, "_json_request", fake_request)

    report = registration._apply_registration(cfg, timeout_sec=30)

    assert report.ok is True
    assert report.applied is True
    assert report.was_already_present is True
    assert report.created is False
    assert report.active_after is True
    assert all(method != "POST" for _url, method, _payload in calls)


def test_apply_posts_secret_only_in_request_and_never_in_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config()
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    subscription_reads = 0

    def fake_request(
        url: str,
        *,
        token: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        ca_bundle: str = "",
        timeout_sec: int = 30,
    ) -> registration.MaxApiResponse:
        nonlocal subscription_reads
        calls.append((url, method, payload))
        assert token == cfg.token
        if url.endswith("/me"):
            return registration.MaxApiResponse(
                200,
                {
                    "user_id": 1,
                    "username": f"clientplatform-{cfg.token}",
                    "name": f"bot-{cfg.secret}",
                    "is_bot": True,
                },
            )
        if method == "POST":
            return registration.MaxApiResponse(
                200,
                {"success": True, "secret": cfg.secret},
            )
        subscription_reads += 1
        active = subscription_reads > 1
        return registration.MaxApiResponse(
            200,
            {"subscriptions": [{"url": cfg.webhook_url}] if active else []},
        )

    monkeypatch.setattr(registration, "_json_request", fake_request)

    report = registration._apply_registration(cfg, timeout_sec=30)
    serialized = json.dumps(asdict(report), ensure_ascii=False)

    post_calls = [payload for _url, method, payload in calls if method == "POST"]
    assert post_calls == [
        {
            "url": cfg.webhook_url,
            "update_types": list(registration.MAX_WEBHOOK_UPDATE_TYPES),
            "secret": cfg.secret,
        }
    ]
    assert report.created is True
    assert report.active_after is True
    assert cfg.token not in serialized
    assert cfg.secret not in serialized
    assert "response" not in serialized


@pytest.mark.parametrize(
    "public_base,error_code",
    [
        ("http://bot.example.test", "public_base_must_be_https"),
        ("https://user:pass@bot.example.test", "public_base_must_not_contain_credentials"),
        ("https://bot.example.test/path", "public_base_must_not_contain_path"),
        ("https://bot.example.test?secret=x", "public_base_must_not_contain_query_or_fragment"),
        ("https://bot.example.test:invalid", "public_base_port_invalid"),
    ],
)
def test_public_base_rejects_non_origin_values(public_base: str, error_code: str) -> None:
    with pytest.raises(registration.MaxWebhookRegistrationError) as raised:
        registration._validated_public_base(public_base)
    assert raised.value.code == error_code
    assert raised.value.network_called is False


def test_provider_free_text_never_enters_error_report() -> None:
    cfg = _config()
    response = registration.MaxApiResponse(
        status=401,
        payload={"message": f"invalid {cfg.token} {cfg.secret}"},
    )

    with pytest.raises(registration.MaxWebhookRegistrationError) as raised:
        registration._require_success(response, stage="me")

    report = registration._error_report(
        cfg,
        mode="apply",
        error_code=raised.value.code,
        network_called=raised.value.network_called,
    )
    serialized = json.dumps(asdict(report), ensure_ascii=False)
    assert report.ok is False
    assert report.applied is False
    assert report.network_called is True
    assert report.error_code == "me_http_401:provider_response_invalid"
    assert cfg.token not in serialized
    assert cfg.secret not in serialized


def test_success_false_is_not_accepted_as_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config()
    subscription_reads = 0

    def fake_request(
        url: str,
        *,
        token: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        ca_bundle: str = "",
        timeout_sec: int = 30,
    ) -> registration.MaxApiResponse:
        nonlocal subscription_reads
        if url.endswith("/me"):
            return registration.MaxApiResponse(200, {"user_id": 1, "is_bot": True})
        if method == "POST":
            return registration.MaxApiResponse(200, {"success": False})
        subscription_reads += 1
        return registration.MaxApiResponse(200, {"subscriptions": []})

    monkeypatch.setattr(registration, "_json_request", fake_request)

    with pytest.raises(
        registration.MaxWebhookRegistrationError,
        match="subscription_create_rejected",
    ):
        registration._apply_registration(cfg, timeout_sec=30)


def test_http_200_invalid_json_is_not_accepted() -> None:
    response = registration.MaxApiResponse(200, {"error": "invalid_json"})

    with pytest.raises(registration.MaxWebhookRegistrationError) as raised:
        registration._require_success(response, stage="subscriptions_after")

    assert raised.value.code == "subscriptions_after_http_200:provider_response_invalid"
    assert raised.value.network_called is True
