from __future__ import annotations

from runtime import health_server, ingress_flags
from services.messenger.preflight import MessengerPreflightStatus


def _clear_flags(monkeypatch) -> None:
    for name in (
        "MAX_WEBHOOK_ENABLED",
        "VK_WEBHOOK_ENABLED",
        "PRIVACY_EXPORT_HTTP_ENABLED",
        "PRIVACY_EXPORT_PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_privacy_export_can_enable_http_ingress_without_messenger_channels(monkeypatch):
    _clear_flags(monkeypatch)
    monkeypatch.setenv("PRIVACY_EXPORT_HTTP_ENABLED", "1")
    monkeypatch.setenv("PRIVACY_EXPORT_PUBLIC_BASE_URL", "https://clientplatform.example")
    monkeypatch.setattr(ingress_flags.settings, "MESSENGER_WEBHOOK_ENABLED", False)
    monkeypatch.setattr(ingress_flags.settings, "MAX_BOT_TOKEN", "")
    monkeypatch.setattr(ingress_flags.settings, "VK_GROUP_TOKEN", "")

    assert ingress_flags.max_webhook_enabled() is False
    assert ingress_flags.vk_webhook_enabled() is False
    assert ingress_flags.http_ingress_enabled() is True


def test_legacy_messenger_flag_does_not_enable_unconfigured_channels(monkeypatch):
    _clear_flags(monkeypatch)
    monkeypatch.setattr(ingress_flags.settings, "MESSENGER_WEBHOOK_ENABLED", True)
    monkeypatch.setattr(ingress_flags.settings, "MAX_BOT_TOKEN", "")
    monkeypatch.setattr(ingress_flags.settings, "VK_GROUP_TOKEN", "")

    assert ingress_flags.max_webhook_enabled() is False
    assert ingress_flags.vk_webhook_enabled() is False
    assert ingress_flags.http_ingress_enabled() is False


def test_readiness_ignores_disabled_channel_preflight_failures(monkeypatch):
    statuses = (
        MessengerPreflightStatus(
            channel="max", ok=False, missing=("MAX_BOT_TOKEN",), details={"enabled": False}
        ),
        MessengerPreflightStatus(
            channel="vk", ok=False, missing=("VK_GROUP_TOKEN",), details={"enabled": False}
        ),
    )
    monkeypatch.setattr(health_server, "check_all_preflights", lambda: statuses)
    ok, errors, details = health_server._messenger_preflight_readiness()
    assert ok is True
    assert errors == []
    assert details["max_preflight_enabled"] is False
    assert details["vk_preflight_enabled"] is False


def test_readiness_fails_enabled_channel_preflight(monkeypatch):
    statuses = (
        MessengerPreflightStatus(
            channel="vk", ok=False, missing=("VK_GROUP_TOKEN",), details={"enabled": True}
        ),
    )
    monkeypatch.setattr(health_server, "check_all_preflights", lambda: statuses)
    ok, errors, _ = health_server._messenger_preflight_readiness()
    assert ok is False
    assert errors == ["ingress:vk:missing:VK_GROUP_TOKEN"]
