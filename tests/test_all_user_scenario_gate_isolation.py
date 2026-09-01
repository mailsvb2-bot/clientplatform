from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts import all_user_scenario_gate as gate


@pytest.mark.parametrize(
    "name,value",
    [
        ("DATABASE_URL", "postgresql://prod-user:prod-pass@db.internal/prod"),
        ("CLIENTPLATFORM_DB_PATH", "/srv/clientplatform/data/data.db"),
        ("CLIENTPLATFORM_DB_PATH", "/srv/clientplatform/data/data.db"),
        ("YOOKASSA_WEBHOOK_SECRET", "live-webhook-secret"),
        ("MAX_BOT_TOKEN", "live-max-token"),
        ("MAX_WEBHOOK_SECRET", "live-max-secret"),
        ("VK_TOKEN", "live-vk-token"),
        ("BOT_TOKEN", "live-telegram-token"),
    ],
)
def test_isolated_parent_env_does_not_copy_application_configuration(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    isolated = gate._isolated_parent_env()

    assert name not in isolated
    assert value not in isolated.values()


def test_step_env_uses_private_sqlite_and_disables_live_ingress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod.example/clientplatform")
    monkeypatch.setenv("CLIENTPLATFORM_DB_PATH", "/srv/clientplatform/data/data.db")
    monkeypatch.setenv("CLIENTPLATFORM_DB_PATH", "/srv/clientplatform/data/data.db")
    target = tmp_path / "scenario.db"

    env = gate._step_env(target)

    assert env["APP_ENV"] == "test"
    assert env["LOAD_DOTENV"] == "0"
    assert env["CLIENTPLATFORM_DB_ENGINE"] == "sqlite"
    assert env["CLIENTPLATFORM_DB_PATH"] == str(target)
    assert env["DATABASE_URL"] == ""
    assert env["MESSENGER_WEBHOOK_ENABLED"] == "0"
    assert env["MAX_WEBHOOK_ENABLED"] == "0"
    assert env["VK_WEBHOOK_ENABLED"] == "0"
    assert "live-secret" not in env.values()
    assert "/srv/clientplatform/data/data.db" not in env.values()
    assert "/srv/clientplatform/data/data.db" not in env.values()


def test_each_step_gets_distinct_database_path(
    tmp_path: Path,
) -> None:
    first = gate._step_env(tmp_path / "first.db")
    second = gate._step_env(tmp_path / "second.db")

    assert first["CLIENTPLATFORM_DB_PATH"] != second["CLIENTPLATFORM_DB_PATH"]


def test_safe_system_environment_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setenv("HOME", "/tmp/synthetic-home")

    isolated = gate._isolated_parent_env()

    assert "PATH" in isolated
    assert isolated["HOME"] == "/tmp/synthetic-home"
