from __future__ import annotations

import importlib


def _run(monkeypatch, **env):
    for key in {
        "APP_ENV",
        "TELEGRAM_TRANSPORT",
        "RUN_MODE",
        "TELEGRAM_WEBHOOK_ENABLED",
        "TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED",
        "ALLOW_INSECURE_TELEGRAM_WEBHOOK",
        "MESSENGER_WEBHOOK_ENABLED",
        "MESSENGER_WEBHOOK_HOST",
        "MESSENGER_WEBHOOK_PORT",
        "MESSENGER_PUBLIC_BASE_URL",
        "PRIVACY_EXPORT_HTTP_ENABLED",
        "PRIVACY_EXPORT_PUBLIC_BASE_URL",
        "PRIVACY_EXPORT_TOKEN_TTL_MINUTES",
        "PUBLIC_BASE_URL",
        "HEALTHCHECK_ENABLED",
        "HEALTHCHECK_HOST",
        "HEALTHCHECK_PORT",
        "CLIENTPLATFORM_DB_ENGINE",
        "DATABASE_URL",
        "CLIENTPLATFORM_DB_PATH",
        "LOG_PATH",
        "BOT_TOKEN",
        "ADMIN_IDS",
        "ADMIN_ID",
        "VK_WEBHOOK_ENABLED",
        "VK_GROUP_ID",
        "VK_GROUP_TOKEN",
        "VK_CONFIRMATION_TOKEN",
        "VK_SECRET",
        "MAX_WEBHOOK_ENABLED",
        "MAX_BOT_TOKEN",
        "MAX_BOT_LINK_BASE",
        "MAX_WEBHOOK_SECRET",
    }:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mod = importlib.import_module("scripts.runtime_contract")
    importlib.reload(mod)
    return mod.run()



def _owner_vk_prod_env() -> dict[str, str]:
    return {
        "APP_ENV": "prod",
        "BOT_TOKEN": "test-bot-token",
        "ADMIN_IDS": "1",
        "TELEGRAM_TRANSPORT": "polling",
        "TELEGRAM_WEBHOOK_ENABLED": "0",
        "PRIVACY_EXPORT_HTTP_ENABLED": "1",
        "PRIVACY_EXPORT_PUBLIC_BASE_URL": "https://app.example",
        "PRIVACY_EXPORT_TOKEN_TTL_MINUTES": "10",
        "VK_WEBHOOK_ENABLED": "1",
        "MESSENGER_PUBLIC_BASE_URL": "https://app.example",
        "VK_GROUP_ID": "241176159",
        "VK_GROUP_TOKEN": "vk-token-for-test",
        "VK_CONFIRMATION_TOKEN": "vk-confirm-for-test",
        "VK_SECRET": "vk-secret-for-test",
        "CLIENTPLATFORM_DB_ENGINE": "postgres",
        "DATABASE_URL": "postgresql:///clientplatform_test",
        "LOG_PATH": "/tmp/clientplatform-test.log",
        "HEALTHCHECK_ENABLED": "1",
    }


def test_runtime_contract_accepts_owner_vk_without_payment_checkout(monkeypatch):
    errors, warnings = _run(monkeypatch, **_owner_vk_prod_env())

    assert errors == []



def test_runtime_contract_accepts_dev_defaults(monkeypatch):
    errors, warnings = _run(monkeypatch, APP_ENV="dev")

    assert errors == []


def test_runtime_contract_rejects_telegram_webhook(monkeypatch):
    errors, warnings = _run(
        monkeypatch,
        APP_ENV="prod",
        TELEGRAM_TRANSPORT="polling",
        TELEGRAM_WEBHOOK_ENABLED="1",
        CLIENTPLATFORM_DB_ENGINE="postgres",
        DATABASE_URL="postgresql:///clientplatform_test",
        LOG_PATH="/tmp/clientplatform.log",
        HEALTHCHECK_ENABLED="1",
    )

    assert any("Telegram" in error or "TELEGRAM" in error for error in errors)


def test_runtime_contract_rejects_sqlite_prod(monkeypatch, tmp_path):
    errors, warnings = _run(
        monkeypatch,
        APP_ENV="prod",
        TELEGRAM_TRANSPORT="polling",
        TELEGRAM_WEBHOOK_ENABLED="0",
        CLIENTPLATFORM_DB_ENGINE="sqlite",
        CLIENTPLATFORM_DB_PATH=str(tmp_path / "state" / "data.db"),
        LOG_PATH="/tmp/clientplatform.log",
        HEALTHCHECK_ENABLED="1",
    )

    assert any("CLIENTPLATFORM_DB_ENGINE" in error for error in errors)
    assert any("DATABASE_URL" in error for error in errors)


def test_runtime_contract_rejects_bad_database_url_scheme(monkeypatch):
    errors, warnings = _run(
        monkeypatch,
        APP_ENV="prod",
        TELEGRAM_TRANSPORT="polling",
        TELEGRAM_WEBHOOK_ENABLED="0",
        CLIENTPLATFORM_DB_ENGINE="postgres",
        DATABASE_URL="sqlite:///tmp.db",
        LOG_PATH="/tmp/clientplatform.log",
        HEALTHCHECK_ENABLED="1",
    )

    assert any("DATABASE_URL" in error for error in errors)


def test_runtime_contract_rejects_repo_relative_log_path(monkeypatch):
    errors, warnings = _run(
        monkeypatch,
        APP_ENV="prod",
        TELEGRAM_TRANSPORT="polling",
        TELEGRAM_WEBHOOK_ENABLED="0",
        CLIENTPLATFORM_DB_ENGINE="postgres",
        DATABASE_URL="postgresql:///clientplatform_test",
        LOG_PATH="logs/app.log",
        HEALTHCHECK_ENABLED="1",
    )

    assert any("LOG_PATH" in error for error in errors)


def test_runtime_contract_detects_messenger_health_port_collision(monkeypatch):
    errors, warnings = _run(
        monkeypatch,
        APP_ENV="dev",
        PRIVACY_EXPORT_HTTP_ENABLED="1",
        PRIVACY_EXPORT_PUBLIC_BASE_URL="https://example.invalid",
        MESSENGER_WEBHOOK_HOST="127.0.0.1",
        MESSENGER_WEBHOOK_PORT="8082",
        MESSENGER_PUBLIC_BASE_URL="https://example.invalid",
        HEALTHCHECK_HOST="127.0.0.1",
        HEALTHCHECK_PORT="8082",
    )

    assert any("collide" in error for error in errors)


def test_runtime_contract_requires_privacy_export_in_prod(monkeypatch):
    errors, warnings = _run(
        monkeypatch,
        APP_ENV="prod",
        TELEGRAM_TRANSPORT="polling",
        TELEGRAM_WEBHOOK_ENABLED="0",
        PRIVACY_EXPORT_HTTP_ENABLED="0",
        CLIENTPLATFORM_DB_ENGINE="postgres",
        DATABASE_URL="postgresql:///clientplatform_test",
        LOG_PATH="/tmp/clientplatform.log",
        HEALTHCHECK_ENABLED="1",
    )

    assert any("PRIVACY_EXPORT_HTTP_ENABLED" in error for error in errors)


def test_runtime_contract_accepts_privacy_export_as_http_ingress(monkeypatch):
    errors, warnings = _run(
        monkeypatch,
        APP_ENV="dev",
        PRIVACY_EXPORT_HTTP_ENABLED="1",
        PRIVACY_EXPORT_PUBLIC_BASE_URL="https://example.invalid",
        PRIVACY_EXPORT_TOKEN_TTL_MINUTES="10",
        MAX_WEBHOOK_ENABLED="0",
        VK_WEBHOOK_ENABLED="0",
    )

    assert not any("PRIVACY_EXPORT" in error or "privacy export" in error for error in errors)
    assert not any("HTTP ingress is disabled" in warning for warning in warnings)


def test_runtime_contract_rejects_insecure_privacy_url_and_bad_ttl(monkeypatch):
    errors, warnings = _run(
        monkeypatch,
        APP_ENV="stage",
        PRIVACY_EXPORT_HTTP_ENABLED="1",
        PRIVACY_EXPORT_PUBLIC_BASE_URL="http://example.invalid",
        PRIVACY_EXPORT_TOKEN_TTL_MINUTES="31",
    )

    assert any("https://" in error for error in errors)
    assert any("PRIVACY_EXPORT_TOKEN_TTL_MINUTES" in error for error in errors)
