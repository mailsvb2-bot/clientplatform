from __future__ import annotations

import importlib

import pytest

cfg = importlib.import_module("config.settings")


ENV_NAMES = (
    "ADMIN_IDS",
    "ADMIN_ID",
    "YOOKASSA_SHOP_ID",
    "YOOKASSA_SECRET_KEY",
    "PAYMENT_CHECKOUT_SIGNING_KEY",
    "CHECKOUT_SIGNING_KEY",
    "MESSENGER_PUBLIC_BASE_URL",
    "PAYMENT_HTTP_ENABLED",
    "PAYMENT_PUBLIC_BASE_URL",
    "PUBLIC_BASE_URL",
    "ALLOW_UNSIGNED_PAYMENT_CHECKOUT_IN_PROD",
    "ALLOW_UNVERIFIED_YOOKASSA_WEBHOOK_IN_PROD",
    "ALLOW_STATIC_PAYMENT_IDEMPOTENCE_KEY_IN_PROD",
    "PAYMENT_DANGEROUS_OVERRIDES_ALLOWED",
    "MAX_WEBHOOK_ENABLED",
    "VK_WEBHOOK_ENABLED",
)


def clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def install_valid_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_env(monkeypatch)
    monkeypatch.setattr(cfg, "APP_ENV", "prod")
    monkeypatch.setenv("ADMIN_IDS", "10")
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "1")
    monkeypatch.setenv("YOOKASSA_SHOP_ID", "shop")
    monkeypatch.setenv("YOOKASSA_SECRET_KEY", "secret")
    monkeypatch.setenv("PAYMENT_CHECKOUT_SIGNING_KEY", "signing")
    monkeypatch.setenv("PAYMENT_PUBLIC_BASE_URL", "https://pay.example")

    monkeypatch.setattr(cfg.settings, "BOT_TOKEN", "bot-token")
    monkeypatch.setattr(cfg.settings, "HEALTHCHECK_ENABLED", True)
    monkeypatch.setattr(cfg.settings, "TELEGRAM_TRANSPORT", "polling")
    monkeypatch.setattr(cfg.settings, "TELEGRAM_WEBHOOK_ENABLED", False)
    monkeypatch.setattr(cfg.settings, "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(cfg.settings, "TELEGRAM_WEBHOOK_SECRET_TOKEN", "")
    monkeypatch.setattr(cfg.settings, "TELEGRAM_WEBHOOK_PREFIX", "/telegram-webhook")
    monkeypatch.setattr(cfg.settings, "MESSENGER_WEBHOOK_ENABLED", False)
    monkeypatch.setattr(cfg.settings, "MESSENGER_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(cfg.settings, "MAX_BOT_TOKEN", "")
    monkeypatch.setattr(cfg.settings, "MAX_BOT_LINK_BASE", "")
    monkeypatch.setattr(cfg.settings, "MAX_WEBHOOK_SECRET", "")
    monkeypatch.setattr(cfg.settings, "VK_GROUP_TOKEN", "")
    monkeypatch.setattr(cfg.settings, "VK_CONFIRMATION_TOKEN", "")
    monkeypatch.setattr(cfg.settings, "VK_GROUP_ID", "")
    monkeypatch.setattr(cfg.settings, "VK_SECRET", "")


def test_admin_id_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_env(monkeypatch)
    assert cfg._parse_admin_ids_env() == []

    monkeypatch.setenv("ADMIN_IDS", "1, bad, ,2")
    assert cfg._parse_admin_ids_env() == [1, 2]

    monkeypatch.delenv("ADMIN_IDS", raising=False)
    monkeypatch.setenv("ADMIN_ID", "3")
    assert cfg._parse_admin_ids_env() == [3]
    monkeypatch.setenv("ADMIN_ID", "bad")
    assert cfg._parse_admin_ids_env() == []


def test_typed_env_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHASE9_VALUE", raising=False)
    assert cfg._env("PHASE9_VALUE", "fallback") == "fallback"
    monkeypatch.setenv("PHASE9_VALUE", "")
    assert cfg._env("PHASE9_VALUE", "fallback") == "fallback"
    monkeypatch.setenv("PHASE9_VALUE", "value")
    assert cfg._env("PHASE9_VALUE", "fallback") == "value"

    for raw in ("1", "true", "YES", "on", "webhook"):
        monkeypatch.setenv("PHASE9_BOOL", raw)
        assert cfg._env_bool("PHASE9_BOOL") is True
        assert cfg._truthy_env("PHASE9_BOOL") is True
    monkeypatch.setenv("PHASE9_BOOL", "0")
    assert cfg._env_bool("PHASE9_BOOL") is False

    monkeypatch.setenv("PHASE9_INT", "5")
    assert cfg._env_int("PHASE9_INT", 1, minimum=1, maximum=10) == 5
    monkeypatch.setenv("PHASE9_INT", "bad")
    with pytest.raises(cfg.ConfigurationError, match="must be an integer"):
        cfg._env_int("PHASE9_INT", 1)
    monkeypatch.setenv("PHASE9_INT", "0")
    with pytest.raises(cfg.ConfigurationError, match=">= 1"):
        cfg._env_int("PHASE9_INT", 1, minimum=1)
    monkeypatch.setenv("PHASE9_INT", "11")
    with pytest.raises(cfg.ConfigurationError, match="<= 10"):
        cfg._env_int("PHASE9_INT", 1, maximum=10)


def test_int_fallback_first_env_and_optional_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHASE9_PRIMARY", raising=False)
    monkeypatch.setenv("PHASE9_FALLBACK", "7")
    assert cfg._env_int_fallback("PHASE9_PRIMARY", "PHASE9_FALLBACK", 1) == 7
    monkeypatch.setenv("PHASE9_PRIMARY", "8")
    assert cfg._env_int_fallback("PHASE9_PRIMARY", "PHASE9_FALLBACK", 1) == 8

    monkeypatch.delenv("PHASE9_A", raising=False)
    monkeypatch.setenv("PHASE9_B", " second ")
    assert cfg._first_env("PHASE9_A", "PHASE9_B") == "second"
    monkeypatch.delenv("PHASE9_B", raising=False)
    assert cfg._first_env("PHASE9_A", "PHASE9_B") == ""

    monkeypatch.delenv("PHASE9_FLAG", raising=False)
    assert cfg._optional_feature_flag("PHASE9_FLAG") is None
    monkeypatch.setenv("PHASE9_FLAG", "yes")
    assert cfg._optional_feature_flag("PHASE9_FLAG") is True
    monkeypatch.setenv("PHASE9_FLAG", "0")
    assert cfg._optional_feature_flag("PHASE9_FLAG") is False


def test_admin_id_list_env_and_field_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_env(monkeypatch)
    settings = cfg.Settings(ADMIN_IDS="1; bad,2")
    assert settings.admin_id_list == [1, 2]

    monkeypatch.setenv("ADMIN_ID", "9")
    assert settings.admin_id_list == [9]

    monkeypatch.delenv("ADMIN_ID", raising=False)
    settings.ADMIN_IDS = ""
    assert settings.admin_id_list == []


def test_payment_base_url_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_env(monkeypatch)
    monkeypatch.setattr(cfg.settings, "MESSENGER_PUBLIC_BASE_URL", "https://settings-messenger/")
    monkeypatch.setattr(cfg.settings, "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL", "https://telegram/")
    assert cfg._prod_payment_base_url() == "https://settings-messenger"

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://public/")
    assert cfg._prod_payment_base_url() == "https://public"
    monkeypatch.setenv("PAYMENT_PUBLIC_BASE_URL", "https://payment/")
    assert cfg._prod_payment_base_url() == "https://payment"
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://messenger/")
    assert cfg._prod_payment_base_url() == "https://messenger"


def test_fail_fast_skips_non_prod_and_accepts_valid_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    install_valid_prod(monkeypatch)
    monkeypatch.setattr(cfg, "APP_ENV", "stage")
    cfg._fail_fast_prod_config()

    monkeypatch.setattr(cfg, "APP_ENV", "production")
    cfg._fail_fast_prod_config()


def test_fail_fast_aggregates_missing_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    install_valid_prod(monkeypatch)
    monkeypatch.setattr(cfg.settings, "BOT_TOKEN", "")
    monkeypatch.delenv("ADMIN_IDS", raising=False)
    monkeypatch.delenv("YOOKASSA_SHOP_ID", raising=False)
    monkeypatch.delenv("YOOKASSA_SECRET_KEY", raising=False)
    monkeypatch.delenv("PAYMENT_CHECKOUT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("PAYMENT_PUBLIC_BASE_URL", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        cfg._fail_fast_prod_config()

    message = str(exc_info.value)
    for name in (
        "ADMIN_IDS",
        "BOT_TOKEN",
        "PAYMENT_CHECKOUT_SIGNING_KEY",
        "PAYMENT_PUBLIC_BASE_URL",
        "YOOKASSA_SECRET_KEY",
        "YOOKASSA_SHOP_ID",
    ):
        assert name in message


def test_fail_fast_rejects_insecure_public_url_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    install_valid_prod(monkeypatch)
    monkeypatch.setenv("PAYMENT_PUBLIC_BASE_URL", "http://pay.example")
    with pytest.raises(SystemExit, match="must start with https"):
        cfg._fail_fast_prod_config()

    monkeypatch.setenv("PAYMENT_PUBLIC_BASE_URL", "https://pay.example")
    monkeypatch.setenv("ALLOW_UNSIGNED_PAYMENT_CHECKOUT_IN_PROD", "1")
    with pytest.raises(SystemExit, match="Dangerous payment override"):
        cfg._fail_fast_prod_config()

    monkeypatch.setenv("PAYMENT_DANGEROUS_OVERRIDES_ALLOWED", "1")
    cfg._fail_fast_prod_config()


def test_fail_fast_rejects_malformed_payment_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    install_valid_prod(monkeypatch)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "tru")

    with pytest.raises(SystemExit, match="PAYMENT_HTTP_ENABLED"):
        cfg._fail_fast_prod_config()


def test_fail_fast_telegram_webhook_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    install_valid_prod(monkeypatch)
    monkeypatch.setattr(cfg.settings, "TELEGRAM_TRANSPORT", "webhook")
    monkeypatch.setattr(cfg.settings, "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL", "http://telegram.example")
    monkeypatch.setattr(cfg.settings, "TELEGRAM_WEBHOOK_SECRET_TOKEN", "")
    with pytest.raises(SystemExit, match="TELEGRAM_WEBHOOK_PUBLIC_BASE_URL must start"):
        cfg._fail_fast_prod_config()

    monkeypatch.setattr(cfg.settings, "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL", "https://telegram.example")
    monkeypatch.setattr(cfg.settings, "TELEGRAM_WEBHOOK_PREFIX", "telegram")
    with pytest.raises(SystemExit, match="PREFIX must start"):
        cfg._fail_fast_prod_config()

    monkeypatch.setattr(cfg.settings, "TELEGRAM_WEBHOOK_PREFIX", "/telegram")
    with pytest.raises(SystemExit) as exc_info:
        cfg._fail_fast_prod_config()
    assert "TELEGRAM_WEBHOOK_SECRET_TOKEN" in str(exc_info.value)

    monkeypatch.setattr(cfg.settings, "TELEGRAM_WEBHOOK_SECRET_TOKEN", "secret")
    cfg._fail_fast_prod_config()


def test_fail_fast_max_and_vk_feature_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    install_valid_prod(monkeypatch)
    monkeypatch.setenv("MAX_WEBHOOK_ENABLED", "1")
    with pytest.raises(SystemExit) as exc_info:
        cfg._fail_fast_prod_config()
    message = str(exc_info.value)
    assert "MESSENGER_PUBLIC_BASE_URL" in message
    assert "MAX_BOT_TOKEN" in message
    assert "MAX_BOT_LINK_BASE" in message
    assert "MAX_WEBHOOK_SECRET" in message

    install_valid_prod(monkeypatch)
    monkeypatch.setenv("VK_WEBHOOK_ENABLED", "1")
    with pytest.raises(SystemExit) as exc_info:
        cfg._fail_fast_prod_config()
    message = str(exc_info.value)
    assert "VK_GROUP_TOKEN" in message
    assert "VK_CONFIRMATION_TOKEN" in message
    assert "VK_GROUP_ID" in message
    assert "VK_SECRET" in message


def test_legacy_messenger_flags_and_healthcheck(monkeypatch: pytest.MonkeyPatch) -> None:
    install_valid_prod(monkeypatch)
    monkeypatch.setattr(cfg.settings, "MESSENGER_WEBHOOK_ENABLED", True)
    monkeypatch.setattr(cfg.settings, "MAX_BOT_TOKEN", "token")
    with pytest.raises(SystemExit) as exc_info:
        cfg._fail_fast_prod_config()
    assert "MAX_BOT_LINK_BASE" in str(exc_info.value)

    install_valid_prod(monkeypatch)
    monkeypatch.setenv("MAX_WEBHOOK_ENABLED", "0")
    monkeypatch.setenv("VK_WEBHOOK_ENABLED", "0")
    monkeypatch.setattr(cfg.settings, "MESSENGER_WEBHOOK_ENABLED", True)
    cfg._fail_fast_prod_config()

    monkeypatch.setattr(cfg.settings, "HEALTHCHECK_ENABLED", False)
    with pytest.raises(SystemExit, match="HEALTHCHECK_ENABLED must be 1"):
        cfg._fail_fast_prod_config()
