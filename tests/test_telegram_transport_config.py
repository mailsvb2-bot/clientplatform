from config.settings import settings
from runtime import health_server
from runtime.telegram_transport import telegram_transport, telegram_webhook_requested


def _clear_transport_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_TRANSPORT", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_ENABLED", raising=False)


def test_telegram_transport_defaults_to_polling(monkeypatch):
    _clear_transport_env(monkeypatch)
    monkeypatch.setattr(settings, "TELEGRAM_TRANSPORT", "polling")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_ENABLED", False)
    assert telegram_webhook_requested() is False
    assert telegram_transport() == "polling"


def test_telegram_transport_backcompat_flag_is_diagnostic_only(monkeypatch):
    _clear_transport_env(monkeypatch)
    monkeypatch.setattr(settings, "TELEGRAM_TRANSPORT", "telegram")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_ENABLED", True)
    assert telegram_webhook_requested() is True
    assert telegram_transport() == "polling"


def test_telegram_transport_explicit_webhook_is_ignored(monkeypatch):
    _clear_transport_env(monkeypatch)
    monkeypatch.setattr(settings, "TELEGRAM_TRANSPORT", "webhook")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_ENABLED", True)
    assert telegram_webhook_requested() is True
    assert telegram_transport() == "polling"


def test_health_webhook_runtime_ignores_telegram_webhook_settings(monkeypatch):
    _clear_transport_env(monkeypatch)
    monkeypatch.setattr(health_server.settings, "MESSENGER_WEBHOOK_ENABLED", False)
    monkeypatch.setattr(health_server.settings, "TELEGRAM_TRANSPORT", "webhook")
    monkeypatch.setattr(health_server.settings, "TELEGRAM_WEBHOOK_ENABLED", True)
    monkeypatch.setattr(health_server, "http_ingress_enabled", lambda: False)
    assert health_server._telegram_transport() == "polling"
    assert health_server._telegram_webhook_configured() is False
    assert health_server._webhook_configured() is False


def test_health_webhook_runtime_reports_webhook_native_providers(monkeypatch):
    monkeypatch.setattr(health_server, "http_ingress_enabled", lambda: True)
    assert health_server._webhook_configured() is True
