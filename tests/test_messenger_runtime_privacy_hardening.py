from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from runtime import messenger_ingress
from services.messenger import observability
from services.messenger.text_ui_router import MessengerReply


def test_payload_observability_never_persists_or_logs_user_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        observability,
        "log_runtime_event",
        lambda _user_id, **kwargs: captured.append(dict(kwargs)),
    )
    secret_text = "Это личная информация, которую нельзя сохранять в логах"

    with caplog.at_level(logging.INFO):
        observability.log_payload_normalized(
            platform="max",
            user_id=17,
            raw_text=secret_text,
            normalized_text=secret_text.strip(),
            event_key="provider-event-123",
        )

    assert secret_text not in caplog.text
    assert captured
    payload = captured[0]["payload"]
    assert "raw_text" not in payload
    assert "normalized_text" not in payload
    assert payload["action"] == "text"
    assert payload["raw_len"] == len(secret_text)
    assert payload["normalized_len"] == len(secret_text.strip())


def test_action_classifier_collapses_tokens_and_free_text() -> None:
    assert observability.classify_messenger_action("ref_SUPER_SECRET_TOKEN") == "start_payload"
    assert observability.classify_messenger_action("/start referral-secret") == "start_payload"
    assert observability.classify_messenger_action("/help anything") == "/help"
    assert observability.classify_messenger_action("cpm:customer:123") == "cpm"
    assert observability.classify_messenger_action("Очень личный свободный текст") == "text"


def test_deployed_max_webhook_rejects_query_and_body_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("ALLOW_LEGACY_MAX_WEBHOOK_SECRET_SOURCES", "1")
    monkeypatch.setattr(messenger_ingress.settings, "MAX_WEBHOOK_SECRET", "expected-secret")
    request = SimpleNamespace(headers={}, query={"secret": "expected-secret"})

    assert messenger_ingress._provided_max_secret(request) == ""
    assert messenger_ingress._max_secret_ok(request, {"secret": "expected-secret"}) is False


def test_non_deployed_legacy_secret_source_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr(messenger_ingress.settings, "MAX_WEBHOOK_SECRET", "expected-secret")
    request = SimpleNamespace(headers={}, query={"secret": "expected-secret"})

    monkeypatch.delenv("ALLOW_LEGACY_MAX_WEBHOOK_SECRET_SOURCES", raising=False)
    assert messenger_ingress._max_secret_ok(request, {}) is False

    monkeypatch.setenv("ALLOW_LEGACY_MAX_WEBHOOK_SECRET_SOURCES", "1")
    assert messenger_ingress._max_secret_ok(request, {}) is False


def test_process_and_persist_keeps_raw_text_out_of_events_and_outbox_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_meta: list[dict[str, Any]] = []
    outbox: list[dict[str, Any]] = []
    raw_text = "Мои личные сведения и закрытый комментарий"

    monkeypatch.setattr(messenger_ingress, "claim_inbound_event", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(messenger_ingress, "log_payload_normalized", lambda **_kwargs: None)
    monkeypatch.setattr(
        messenger_ingress,
        "parse_clientplatform_entry_command",
        lambda *_args, **_kwargs: SimpleNamespace(action="owner_start"),
    )
    monkeypatch.setattr(
        messenger_ingress,
        "handle_clientplatform_entry",
        lambda *_args, **_kwargs: (55, [MessengerReply(text="Ответ")]),
    )
    monkeypatch.setattr(
        messenger_ingress,
        "log_event",
        lambda _user_id, _name, meta: event_meta.append(dict(meta)),
    )
    monkeypatch.setattr(
        messenger_ingress,
        "persist_reply_bundle",
        lambda **kwargs: outbox.append(dict(kwargs)) or True,
    )

    processed, canonical_user_id, replies = messenger_ingress._process_and_persist(
        platform="vk",
        event_key="event-1",
        payload={"type": "message_new"},
        extracted={
            "user_id": 55,
            "external_user_id": "vk-55",
            "text": raw_text,
            "username": "",
            "display_name": "",
            "first_name": "",
        },
        event_type="message_new",
    )

    assert processed is True
    assert canonical_user_id == 55
    assert replies == 1
    assert event_meta == [{"action": "owner_start", "text_len": len(raw_text)}]
    assert outbox[0]["action"] == "clientplatform_owner_start"
    assert raw_text not in str(event_meta)
    assert raw_text not in str(outbox[0]["action"])
