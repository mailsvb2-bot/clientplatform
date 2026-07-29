from __future__ import annotations

import importlib


def test_telegram_transport_ignores_backcompat_webhook_flag(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TRANSPORT", "telegram")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_ENABLED", "1")
    mod = importlib.import_module("runtime.telegram_transport")
    assert mod.telegram_webhook_requested() is True
    assert mod.telegram_transport() == "polling"


def test_telegram_transport_ignores_explicit_webhook(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TRANSPORT", "webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_ENABLED", "1")
    mod = importlib.import_module("runtime.telegram_transport")
    assert mod.telegram_webhook_requested() is True
    assert mod.telegram_transport() == "polling"


def test_payment_and_audio_modules_import():
    # Regression coverage for previous runtime NameError/import regressions.
    importlib.import_module("services.payments.subscription")
    importlib.import_module("services.payments.hooks")
    importlib.import_module("handlers.audio")
    importlib.import_module("handlers.mood_flow.ratings")


def test_pricing_title_normalizer_and_matcher_import():
    mod = importlib.import_module("services.pricing_update")
    assert mod._norm_title("  Ёжик — ПРО  30 дней ") == "ежик про 30 дней"
