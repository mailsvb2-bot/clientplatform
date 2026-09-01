from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from runtime.messenger_max_sender import MAX_API2_BASE_URL, MaxBotSender, _attachment_retry_delays
from runtime.messenger_transport_errors import MessengerTransportError
from scripts import max_provider_audit, register_max_webhook
from services.messenger import preflight


ROOT = Path(__file__).resolve().parents[1]


def test_max_sender_defaults_and_legacy_values_resolve_to_api2(monkeypatch) -> None:
    monkeypatch.delenv("MAX_API_BASE_URL", raising=False)
    monkeypatch.delenv("ALLOW_CUSTOM_MAX_API_BASE_URL", raising=False)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setattr("runtime.messenger_max_sender.settings.MAX_API_BASE_URL", "", raising=False)

    assert MaxBotSender()._api_base() == MAX_API2_BASE_URL
    assert MaxBotSender(api_base_url="https://platform-api.max.ru")._api_base() == MAX_API2_BASE_URL
    assert MaxBotSender(api_base_url="https://botapi.max.ru")._api_base() == MAX_API2_BASE_URL


def test_max_sender_rejects_custom_origin_in_deployed_env(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("ALLOW_CUSTOM_MAX_API_BASE_URL", "1")

    with pytest.raises(MessengerTransportError, match="platform-api2.max.ru"):
        MaxBotSender(api_base_url="https://attacker.example")._api_base()


def test_max_sender_allows_custom_origin_only_for_explicit_dev_tests(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("ALLOW_CUSTOM_MAX_API_BASE_URL", raising=False)
    with pytest.raises(MessengerTransportError, match="platform-api2.max.ru"):
        MaxBotSender(api_base_url="https://max.test")._api_base()

    monkeypatch.setenv("ALLOW_CUSTOM_MAX_API_BASE_URL", "1")
    assert MaxBotSender(api_base_url="https://max.test")._api_base() == "https://max.test"


def test_max_attachment_retry_schedule_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("MAX_ATTACHMENT_RETRY_DELAYS_SEC", "0,1,2.5,broken,-1,90")

    assert _attachment_retry_delays() == (0.0, 1.0, 2.5, 60.0)


def test_max_multipart_upload_does_not_receive_bot_token(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "audio.mp3"
    source.write_bytes(b"audio")
    captured: dict[str, object] = {}

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("MAX_CA_BUNDLE", raising=False)
    monkeypatch.setattr("runtime.messenger_max_sender.get_cached_media_token", lambda *args, **kwargs: None)
    monkeypatch.setattr("runtime.messenger_max_sender.store_media_token", lambda *args, **kwargs: None)

    def fake_json_request(url, *, method, headers, payload, retries=None, ssl_context=None):
        captured["api_headers"] = headers
        captured["api_retries"] = retries
        captured["api_ssl_context"] = ssl_context
        return {"url": "https://upload.max.example/file", "token": "media-token"}

    def fake_multipart_upload(url, *, field_name, path, timeout=120, retries=None, ssl_context=None):
        captured["upload_url"] = url
        captured["field_name"] = field_name
        captured["path"] = path
        captured["timeout"] = timeout
        captured["upload_retries"] = retries
        captured["upload_ssl_context"] = ssl_context
        return {"token": "media-token"}

    monkeypatch.setattr("runtime.messenger_max_sender.json_request", fake_json_request)
    monkeypatch.setattr("runtime.messenger_max_sender.multipart_upload", fake_multipart_upload)

    result = asyncio.run(MaxBotSender(token="bot-secret")._ensure_media_token(source, media_type="audio"))

    assert result == "media-token"
    assert captured["api_headers"] == {"Authorization": "bot-secret"}
    assert captured["api_retries"] == 1
    assert captured["upload_url"] == "https://upload.max.example/file"
    assert captured["field_name"] == "data"
    assert captured["path"] == source


def test_max_ca_bundle_is_used_by_api_and_upload(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "audio.mp3"
    source.write_bytes(b"audio")
    bundle = tmp_path / "ca.pem"
    bundle.write_text("test-ca", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeSslContext:
        def load_verify_locations(self, *, cafile=None, capath=None, cadata=None) -> None:
            captured["loaded_cafile"] = cafile

    sentinel = FakeSslContext()

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("MAX_CA_BUNDLE", str(bundle))
    monkeypatch.setattr("runtime.messenger_max_sender.ssl.create_default_context", lambda: sentinel)
    monkeypatch.setattr("runtime.messenger_max_sender.get_cached_media_token", lambda *args, **kwargs: None)
    monkeypatch.setattr("runtime.messenger_max_sender.store_media_token", lambda *args, **kwargs: None)

    def fake_json_request(url, *, method, headers, payload, retries=None, ssl_context=None):
        captured["api_ssl_context"] = ssl_context
        return {"url": "https://upload.max.example/file", "token": "media-token"}

    def fake_multipart_upload(url, *, field_name, path, timeout=120, retries=None, ssl_context=None):
        captured["upload_ssl_context"] = ssl_context
        return {"token": "media-token"}

    monkeypatch.setattr("runtime.messenger_max_sender.json_request", fake_json_request)
    monkeypatch.setattr("runtime.messenger_max_sender.multipart_upload", fake_multipart_upload)

    asyncio.run(MaxBotSender(token="bot-secret")._ensure_media_token(source, media_type="audio"))

    assert captured["loaded_cafile"] == str(bundle)
    assert captured["api_ssl_context"] is sentinel
    assert captured["upload_ssl_context"] is sentinel


def test_max_registration_uses_api2_and_validates_secret(monkeypatch) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "token")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "valid_secret-1")
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example.test")
    monkeypatch.delenv("MAX_API_BASE_URL", raising=False)

    config = register_max_webhook._load_config()

    assert config.api_base_url == "https://platform-api2.max.ru"
    assert config.webhook_url == "https://bot.example.test/webhooks/max"

    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "bad secret")
    with pytest.raises(SystemExit, match="MAX_WEBHOOK_SECRET"):
        register_max_webhook._load_config()


def test_max_registration_rejects_legacy_api_domain(monkeypatch) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "token")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "valid_secret-1")
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example.test")
    monkeypatch.setenv("MAX_API_BASE_URL", "https://platform-api.max.ru")

    with pytest.raises(SystemExit, match="platform-api2.max.ru"):
        register_max_webhook._load_config()


def test_max_preflight_warns_on_legacy_api_domain(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("MAX_WEBHOOK_ENABLED", "1")
    monkeypatch.setenv("MAX_API_BASE_URL", "https://platform-api.max.ru")
    monkeypatch.setattr(preflight.settings, "MESSENGER_PUBLIC_BASE_URL", "https://bot.example.test", raising=False)
    monkeypatch.setattr(preflight.settings, "MAX_BOT_TOKEN", "token", raising=False)
    monkeypatch.setattr(preflight.settings, "MAX_BOT_LINK_BASE", "https://max.ru/bot/{payload}", raising=False)

    status = preflight.check_max_preflight()

    assert status.ok is True
    assert any("platform-api2.max.ru" in warning for warning in status.warnings)


def test_max_preflight_fails_on_custom_domain_in_prod(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("MAX_WEBHOOK_ENABLED", "1")
    monkeypatch.setenv("MAX_API_BASE_URL", "https://attacker.example")
    monkeypatch.setattr(preflight.settings, "MESSENGER_PUBLIC_BASE_URL", "https://bot.example.test", raising=False)
    monkeypatch.setattr(preflight.settings, "MAX_BOT_TOKEN", "token", raising=False)
    monkeypatch.setattr(preflight.settings, "MAX_BOT_LINK_BASE", "https://max.ru/bot/{payload}", raising=False)
    monkeypatch.setattr(preflight.settings, "MAX_WEBHOOK_SECRET", "secret", raising=False)

    status = preflight.check_max_preflight()

    assert status.ok is False
    assert any("official_api2_required" in item for item in status.missing)


def test_max_audit_reports_active_api2_subscription(monkeypatch) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("MAX_API_BASE_URL", "https://platform-api2.max.ru")
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example.test")

    def fake_call(_token: str, path: str):
        if path == "/me":
            return {"username": "clientplatform"}, 200
        return {"subscriptions": [{"url": "https://bot.example.test/webhooks/max"}]}, 200

    monkeypatch.setattr(max_provider_audit, "_api_call", fake_call)
    message, code = max_provider_audit.run()

    assert code == 0
    assert message == (
        "status=ok stage=subscriptions bot=clientplatform code=200 "
        "error=NONE api=platform-api2.max.ru webhook=present"
    )


def test_max_audit_fails_closed_when_webhook_is_missing(monkeypatch) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("MAX_API_BASE_URL", "https://platform-api2.max.ru")
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example.test")

    def fake_call(_token: str, path: str):
        if path == "/me":
            return {"username": "clientplatform"}, 200
        return {"subscriptions": []}, 200

    monkeypatch.setattr(max_provider_audit, "_api_call", fake_call)
    message, code = max_provider_audit.run()

    assert code == 7
    assert "WEBHOOK_NOT_REGISTERED" in message
    assert "secret-token" not in message


def test_max_production_files_use_api2_and_no_query_secret_helper() -> None:
    paths = [
        ROOT / "deploy" / "clientplatform" / "clientplatform.production.env.example",
        ROOT / "docs" / "MAX_WEBHOOK_SETUP.md",
        ROOT / "scripts" / "register_max_webhook.py",
    ]
    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert "https://platform-api2.max.ru" in content

    assert not (ROOT / "scripts" / "configure_max_webhook.py").exists()
    docs = (ROOT / "docs" / "MAX_WEBHOOK_SETUP.md").read_text(encoding="utf-8")
    assert "X-Max-Bot-Api-Secret" in docs
    assert "?secret=" not in docs


def test_max_sender_source_does_not_forward_token_to_upload_url() -> None:
    source = (ROOT / "runtime" / "messenger_max_sender.py").read_text(encoding="utf-8")

    assert "multipart_upload," in source
    assert "field_name=\"data\"" in source
    assert "multipart_upload, upload_url, token=token" not in source


def test_clientplatform_env_preparer_migrates_max_to_api2() -> None:
    source = (ROOT / "scripts" / "clientplatform_prepare_production_env.py").read_text(
        encoding="utf-8"
    )

    assert '_MAX_API2_BASE_URL = "https://platform-api2.max.ru"' in source
    assert '"MAX_API_BASE_URL": _MAX_API2_BASE_URL' in source
    assert "run_deploy_worker.sh" not in source
