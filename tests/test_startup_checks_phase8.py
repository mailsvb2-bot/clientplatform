from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import paths, startup_checks


ENV_KEYS = {
    "APP_ENV",
    "ADMIN_IDS",
    "ADMIN_ID",
    "HEALTHCHECK_ENABLED",
    "TELEGRAM_TRANSPORT",
    "RUN_MODE",
    "TELEGRAM_WEBHOOK_ENABLED",
    "MESSENGER_WEBHOOK_ENABLED",
    "TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED",
    "ALLOW_INSECURE_TELEGRAM_WEBHOOK",
    "CLIENTPLATFORM_DB_ENGINE",
    "DATABASE_URL",
    "CLIENTPLATFORM_RUNTIME_ROOT",
    "CLIENTPLATFORM_WRITABLE_ROOT",
    "CLIENTPLATFORM_DATA_DIR",
    "CLIENTPLATFORM_LOGS_DIR",
    "MESSENGER_WEBHOOK_HOST",
    "MESSENGER_WEBHOOK_PORT",
    "WEBHOOK_HOST",
    "WEBHOOK_PORT",
    "HEALTHCHECK_HOST",
    "HEALTHCHECK_PORT",
    "BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
}


def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def valid_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    clean_env(monkeypatch)
    values = {
        "APP_ENV": "prod",
        "ADMIN_IDS": "1",
        "HEALTHCHECK_ENABLED": "1",
        "TELEGRAM_TRANSPORT": "polling",
        "TELEGRAM_WEBHOOK_ENABLED": "0",
        "MESSENGER_WEBHOOK_ENABLED": "0",
        "TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED": "0",
        "ALLOW_INSECURE_TELEGRAM_WEBHOOK": "0",
        "CLIENTPLATFORM_DB_ENGINE": "postgres",
        "DATABASE_URL": "postgresql://user:pass@db/app",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_env_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    clean_env(monkeypatch)
    assert startup_checks._truthy_env("FLAG") is False
    for value in ("1", "true", "YES", " on ", "webhook"):
        monkeypatch.setenv("FLAG", value)
        assert startup_checks._truthy_env("FLAG") is True
    monkeypatch.setenv("FLAG", "off")
    assert startup_checks._truthy_env("FLAG") is False

    assert startup_checks._int_env("PORT", 10) == 10
    monkeypatch.setenv("PORT", " 42 ")
    assert startup_checks._int_env("PORT", 10) == 42
    monkeypatch.setenv("PORT", "invalid")
    with pytest.raises(startup_checks.StartupCheckError, match="Invalid integer env PORT"):
        startup_checks._int_env("PORT", 10)

    monkeypatch.setenv("EMPTY", "  ")
    monkeypatch.setenv("SECOND", " value ")
    assert startup_checks._env_any("EMPTY", "SECOND") == "value"
    assert startup_checks._env_any("MISSING") == ""


def test_resolved_db_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    clean_env(monkeypatch)
    for raw in ("postgres", "postgresql", "pg"):
        monkeypatch.setenv("CLIENTPLATFORM_DB_ENGINE", raw)
        assert startup_checks._resolved_db_engine() == "postgres"
    for raw in ("sqlite", "sqlite3"):
        monkeypatch.setenv("CLIENTPLATFORM_DB_ENGINE", raw)
        assert startup_checks._resolved_db_engine() == "sqlite"
    monkeypatch.setenv("CLIENTPLATFORM_DB_ENGINE", "")
    monkeypatch.setenv("DATABASE_URL", "postgresql://db")
    assert startup_checks._resolved_db_engine() == "postgres"
    monkeypatch.delenv("DATABASE_URL")
    assert startup_checks._resolved_db_engine() == "sqlite"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ADMIN_IDS": "", "ADMIN_ID": ""}, "ADMIN_IDS or ADMIN_ID"),
        ({"HEALTHCHECK_ENABLED": "0"}, "HEALTHCHECK_ENABLED must be 1"),
        ({"TELEGRAM_TRANSPORT": "webhook"}, "polling-only"),
        ({"TELEGRAM_WEBHOOK_ENABLED": "1"}, "polling-only"),
        ({"TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED": "1"}, "LEGACY_TOKEN_WEBHOOK"),
        ({"ALLOW_INSECURE_TELEGRAM_WEBHOOK": "1"}, "forbidden in prod"),
        ({"CLIENTPLATFORM_DB_ENGINE": "sqlite", "DATABASE_URL": ""}, "must be postgres"),
        ({"DATABASE_URL": ""}, "DATABASE_URL is required"),
        ({"DATABASE_URL": "sqlite:///data.db"}, "must use postgres"),
    ],
)
def test_prod_ingress_rejects_unsafe_configuration(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
    message: str,
) -> None:
    valid_prod(monkeypatch)
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(startup_checks.StartupCheckError, match=message):
        startup_checks._prod_ingress_checks()


def test_prod_ingress_accepts_valid_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    valid_prod(monkeypatch)
    startup_checks._prod_ingress_checks()
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CLIENTPLATFORM_DB_ENGINE", "pg")
    monkeypatch.setenv("DATABASE_URL", "postgres://db/app")
    startup_checks._prod_ingress_checks()


def test_webhook_health_port_collision_and_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("MESSENGER_WEBHOOK_ENABLED", "1")
    monkeypatch.setenv("HEALTHCHECK_ENABLED", "1")
    monkeypatch.setenv("MESSENGER_WEBHOOK_HOST", "127.0.0.1")
    monkeypatch.setenv("HEALTHCHECK_HOST", "0.0.0.0")
    monkeypatch.setenv("MESSENGER_WEBHOOK_PORT", "8081")
    monkeypatch.setenv("HEALTHCHECK_PORT", "8081")
    with pytest.raises(startup_checks.StartupCheckError, match="Port collision"):
        startup_checks._prod_ingress_checks()

    monkeypatch.setenv("HEALTHCHECK_PORT", "8082")
    startup_checks._prod_ingress_checks()

    monkeypatch.setenv("WEBHOOK_PORT", "bad")
    monkeypatch.delenv("MESSENGER_WEBHOOK_PORT")
    with pytest.raises(startup_checks.StartupCheckError, match="Invalid integer env WEBHOOK_PORT"):
        startup_checks._prod_ingress_checks()


def make_project(root: Path) -> None:
    for relative in (
        "services/job_keys.py",
        "core/task_manager.py",
        "services/db_writer.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# required\n", encoding="utf-8")


def test_run_startup_checks_clean_deploy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", "secret")
    make_project(tmp_path)

    startup_checks.run_startup_checks(tmp_path)

    for relative in ("data", "logs"):
        assert (tmp_path / relative).is_dir()
    assert not (tmp_path / "audio").exists()


def test_production_startup_uses_external_writable_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    valid_prod(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", "secret")
    release = tmp_path / "release"
    state = tmp_path / "state"
    make_project(release)
    monkeypatch.setenv("CLIENTPLATFORM_WRITABLE_ROOT", str(state))

    assert paths.resolve_data_dir(release) == state / "data"
    assert paths.resolve_logs_dir(release) == state / "logs"

    startup_checks.run_startup_checks(release)

    assert (state / "data").is_dir()
    assert (state / "logs").is_dir()
    assert not (release / "data").exists()
    assert not (release / "logs").exists()
    assert not (release / "audio").exists()


def test_production_startup_rejects_writable_paths_inside_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    valid_prod(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", "secret")
    release = tmp_path / "release"
    make_project(release)
    monkeypatch.setenv("CLIENTPLATFORM_DATA_DIR", str(release / "data"))
    monkeypatch.setenv("CLIENTPLATFORM_LOGS_DIR", str(tmp_path / "state" / "logs"))

    with pytest.raises(startup_checks.StartupCheckError, match="CLIENTPLATFORM_DATA_DIR must be outside"):
        startup_checks.run_startup_checks(release)


def test_run_startup_checks_accepts_telegram_token_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    make_project(tmp_path)
    startup_checks.run_startup_checks(tmp_path)


def test_run_startup_checks_missing_file_and_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", "secret")
    make_project(tmp_path)
    (tmp_path / "services/db_writer.py").unlink()
    with pytest.raises(startup_checks.StartupCheckError, match="Missing required file"):
        startup_checks.run_startup_checks(tmp_path)

    make_project(tmp_path)
    monkeypatch.delenv("BOT_TOKEN")
    with pytest.raises(startup_checks.StartupCheckError, match="BOT_TOKEN is empty"):
        startup_checks.run_startup_checks(tmp_path)
