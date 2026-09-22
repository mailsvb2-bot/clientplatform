from __future__ import annotations

from pathlib import Path

import pytest

from scripts import clientplatform_recover_max_production_env as recovery


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if "=" not in raw or raw.lstrip().startswith("#"):
            continue
        key, value = raw.split("=", 1)
        result[key] = value
    return result


def test_recover_uses_verified_historical_token_and_derives_missing_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "clientplatform.env"
    _write_env(
        env_file,
        {
            "APP_ENV": "prod",
            "MESSENGER_PUBLIC_BASE_URL": "https://app.clientplatform.ru",
            "MAX_API_BASE_URL": recovery.OFFICIAL_MAX_API_BASE,
            "MAX_WEBHOOK_ENABLED": "0",
        },
    )
    _write_env(
        tmp_path / "clientplatform.env.before-previous",
        {
            "MAX_BOT_TOKEN": "historical-token",
            "MAX_WEBHOOK_SECRET": "old-secret-not-required",
        },
    )

    def fake_me(token: str, api_base: str, *, timeout_sec: int):
        assert token == "historical-token"
        assert api_base == recovery.OFFICIAL_MAX_API_BASE
        assert timeout_sec == 7
        return {"user_id": 123, "username": "clientplatform_max_bot"}

    monkeypatch.setattr(recovery, "_max_me", fake_me)
    report = recovery.recover(env_file, timeout_sec=7)

    assert report.ok is True
    assert report.token_source == "historical_backup"
    assert report.webhook_secret_generated is True
    assert report.bot_link_derived is True
    assert report.bot_name_derived is True

    values = _values(env_file)
    assert values["MAX_BOT_TOKEN"] == "historical-token"
    assert values["MAX_WEBHOOK_ENABLED"] == "1"
    assert values["MAX_BOT_NAME"] == "clientplatform_max_bot"
    assert values["MAX_BOT_LINK_BASE"] == recovery.CANONICAL_MAX_LINK_BASE
    assert values["MAX_WEBHOOK_SECRET"]
    assert values["MAX_WEBHOOK_SECRET"] != "old-secret-not-required"


def test_recover_preserves_current_verified_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "clientplatform.env"
    _write_env(
        env_file,
        {
            "MESSENGER_PUBLIC_BASE_URL": "https://app.clientplatform.ru",
            "MAX_API_BASE_URL": recovery.OFFICIAL_MAX_API_BASE,
            "MAX_BOT_TOKEN": "current-token",
            "MAX_WEBHOOK_SECRET": "current_secret",
            "MAX_BOT_LINK_BASE": "https://max.ru/current-bot",
            "MAX_BOT_NAME": "current-bot",
            "MAX_WEBHOOK_ENABLED": "0",
        },
    )
    monkeypatch.setattr(
        recovery,
        "_max_me",
        lambda token, api_base, timeout_sec: {"user_id": 5, "username": "current-bot"},
    )

    report = recovery.recover(env_file)
    values = _values(env_file)

    assert report.token_source == "current"
    assert report.webhook_secret_generated is False
    assert report.bot_link_derived is False
    assert report.bot_name_derived is False
    assert values["MAX_WEBHOOK_SECRET"] == "current_secret"
    assert values["MAX_BOT_LINK_BASE"] == "https://max.ru/current-bot"
    assert values["MAX_WEBHOOK_ENABLED"] == "1"


def test_recover_fails_without_current_or_historical_token(tmp_path: Path) -> None:
    env_file = tmp_path / "clientplatform.env"
    _write_env(
        env_file,
        {"MESSENGER_PUBLIC_BASE_URL": "https://app.clientplatform.ru"},
    )

    with pytest.raises(
        recovery.MaxProductionRecoveryError,
        match="max_bot_token_not_recoverable",
    ):
        recovery.recover(env_file)


def test_recover_rejects_multiple_valid_bot_identities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "clientplatform.env"
    _write_env(
        env_file,
        {"MESSENGER_PUBLIC_BASE_URL": "https://app.clientplatform.ru"},
    )
    _write_env(
        tmp_path / "clientplatform.env.before-one",
        {"MAX_BOT_TOKEN": "token-one"},
    )
    _write_env(
        tmp_path / "clientplatform.env.before-two",
        {"MAX_BOT_TOKEN": "token-two"},
    )

    def fake_me(token: str, api_base: str, *, timeout_sec: int):
        return {
            "user_id": 1 if token == "token-one" else 2,
            "username": "one" if token == "token-one" else "two",
        }

    monkeypatch.setattr(recovery, "_max_me", fake_me)

    with pytest.raises(
        recovery.MaxProductionRecoveryError,
        match="multiple_valid_max_bot_identities",
    ):
        recovery.recover(env_file)


def test_recover_rejects_nonofficial_provider_origin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "clientplatform.env"
    _write_env(
        env_file,
        {
            "MESSENGER_PUBLIC_BASE_URL": "https://app.clientplatform.ru",
            "MAX_API_BASE_URL": "https://example.test",
            "MAX_BOT_TOKEN": "token",
        },
    )
    called = False

    def fake_me(token: str, api_base: str, *, timeout_sec: int):
        nonlocal called
        called = True
        return {"user_id": 1, "username": "bot"}

    monkeypatch.setattr(recovery, "_max_me", fake_me)

    with pytest.raises(
        recovery.MaxProductionRecoveryError,
        match="max_api_base_not_official",
    ):
        recovery.recover(env_file)
    assert called is False
