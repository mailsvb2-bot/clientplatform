from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from config import settings as cfg
from runtime.messenger_max_sender import MaxBotSender
from services.messenger import reply_dispatcher, text_ui_router


_PAYMENT_ENV_NAMES = (
    "PAYMENT_HTTP_ENABLED",
    "YOOKASSA_SHOP_ID",
    "YOOKASSA_SECRET_KEY",
    "PAYMENT_CHECKOUT_SIGNING_KEY",
    "CHECKOUT_SIGNING_KEY",
    "MESSENGER_PUBLIC_BASE_URL",
    "PAYMENT_PUBLIC_BASE_URL",
    "PUBLIC_BASE_URL",
    "PRIVACY_EXPORT_HTTP_ENABLED",
    "PRIVACY_EXPORT_PUBLIC_BASE_URL",
    "MAX_WEBHOOK_ENABLED",
    "VK_WEBHOOK_ENABLED",
    "ALLOW_UNSIGNED_PAYMENT_CHECKOUT_IN_PROD",
    "ALLOW_UNVERIFIED_YOOKASSA_WEBHOOK_IN_PROD",
    "ALLOW_STATIC_PAYMENT_IDEMPOTENCE_KEY_IN_PROD",
    "PAYMENT_DANGEROUS_OVERRIDES_ALLOWED",
)


def _install_telegram_only_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PAYMENT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cfg, "APP_ENV", "prod")
    monkeypatch.setenv("ADMIN_IDS", "10")
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "0")
    monkeypatch.setattr(cfg.settings, "BOT_TOKEN", "bot-token")
    monkeypatch.setattr(cfg.settings, "HEALTHCHECK_ENABLED", True)
    monkeypatch.setattr(cfg.settings, "TELEGRAM_TRANSPORT", "polling")
    monkeypatch.setattr(cfg.settings, "TELEGRAM_WEBHOOK_ENABLED", False)
    monkeypatch.setattr(cfg.settings, "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(cfg.settings, "MESSENGER_WEBHOOK_ENABLED", False)
    monkeypatch.setattr(cfg.settings, "MESSENGER_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(cfg.settings, "MAX_BOT_TOKEN", "")
    monkeypatch.setattr(cfg.settings, "VK_GROUP_TOKEN", "")


def test_prod_config_allows_explicit_telegram_only_without_yookassa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_telegram_only_prod(monkeypatch)

    cfg._fail_fast_prod_config()


def test_prod_config_requires_external_checkout_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_telegram_only_prod(monkeypatch)
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "1")

    with pytest.raises(SystemExit) as exc_info:
        cfg._fail_fast_prod_config()

    message = str(exc_info.value)
    assert "YOOKASSA_SHOP_ID" in message
    assert "YOOKASSA_SECRET_KEY" in message
    assert "PAYMENT_CHECKOUT_SIGNING_KEY" in message
    assert "PAYMENT_PUBLIC_BASE_URL" in message


def test_prod_config_owner_vk_does_not_force_external_checkout_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_telegram_only_prod(monkeypatch)
    monkeypatch.setenv("VK_WEBHOOK_ENABLED", "1")
    monkeypatch.setattr(cfg.settings, "MESSENGER_PUBLIC_BASE_URL", "https://app.example")
    monkeypatch.setattr(cfg.settings, "VK_GROUP_ID", "241176159")
    monkeypatch.setattr(cfg.settings, "VK_GROUP_TOKEN", "vk-token-for-test")
    monkeypatch.setattr(cfg.settings, "VK_CONFIRMATION_TOKEN", "confirm-for-test")
    monkeypatch.setattr(cfg.settings, "VK_SECRET", "secret-for-test")

    cfg._fail_fast_prod_config()


def test_cross_messenger_privacy_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(text_ui_router, "_register", lambda *_args, **_kwargs: 77)

    user_id, replies = text_ui_router.handle_incoming_text(
        77,
        platform="vk",
        external_user_id="vk-77",
        text="privacy",
    )
    assert user_id == 77
    assert text_ui_router.PRIVACY_POLICY_URL in replies[0].text

    _, warning = text_ui_router.handle_incoming_text(
        77,
        platform="max",
        external_user_id="max-77",
        text="mydata",
    )
    assert "mydata CONFIRM" in warning[0].text
    assert "одноразовую HTTPS-ссылку" in warning[0].text

    _, replies = text_ui_router.handle_incoming_text(
        77,
        platform="max",
        external_user_id="max-77",
        text="mydata CONFIRM",
    )
    assert replies == [text_ui_router.MessengerReply(kind="privacy_export")]

    erased: list[tuple[int, str]] = []

    def erase(user_id: int, *, reason: str) -> Any:
        erased.append((user_id, reason))
        return SimpleNamespace(deleted_tables={"mood_sessions": 2, "state_ratings": 3})

    monkeypatch.setattr(text_ui_router, "erase_user_behavioral_data", erase)
    _, warning = text_ui_router.handle_incoming_text(
        77,
        platform="vk",
        external_user_id="vk-77",
        text="deletemydata",
    )
    assert "CONFIRM" in warning[0].text
    assert erased == []

    _, confirmed = text_ui_router.handle_incoming_text(
        77,
        platform="vk",
        external_user_id="vk-77",
        text="deletemydata CONFIRM",
    )
    assert "Удалено записей: 5" in confirmed[0].text
    assert erased == [(77, "vk_user_request")]


@pytest.mark.asyncio
async def test_privacy_export_is_sent_as_one_time_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    class Sender:
        async def send_document_file(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("privacy export must not be uploaded into messenger history")

        async def send_text(self, external_user_id: str, text: str, **kwargs: Any) -> None:
            observed["external_user_id"] = external_user_id
            observed["text"] = text
            observed["kwargs"] = kwargs

    monkeypatch.setattr(
        reply_dispatcher,
        "issue_privacy_export_url",
        lambda user_id, *, platform: f"https://example.test/privacy/export/{platform}-{user_id}",
    )
    monkeypatch.setattr(reply_dispatcher, "privacy_export_ttl_minutes", lambda: 10)
    await reply_dispatcher._send_privacy_export(
        platform="vk",
        sender=Sender(),
        external_user_id="vk-77",
        canonical_user_id=77,
    )

    assert observed["external_user_id"] == "vk-77"
    assert "https://example.test/privacy/export/vk-77" in observed["text"]
    assert "одноразовая" in observed["text"].casefold()
    assert "предпросмотр" in observed["text"].casefold()


@pytest.mark.asyncio
async def test_max_sender_uploads_privacy_export_as_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sender = MaxBotSender()
    calls: list[dict[str, Any]] = []

    async def send_media(*args: Any, **kwargs: Any) -> str:
        calls.append({"args": args, "kwargs": kwargs})
        return "sent"

    monkeypatch.setattr(sender, "_send_media_file", send_media)
    export_path = tmp_path / "export.json.gz"
    export_path.write_bytes(b"data")

    result = await sender.send_document_file(
        "max-77",
        export_path,
        caption="private",
        notify=False,
    )

    assert result == "sent"
    assert calls[0]["args"] == ("max-77", export_path)
    assert calls[0]["kwargs"] == {
        "media_type": "file",
        "caption": "private",
        "notify": False,
    }
