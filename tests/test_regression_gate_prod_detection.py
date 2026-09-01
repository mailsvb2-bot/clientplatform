from __future__ import annotations

from scripts import regression_gate


def test_prod_host_detection_is_not_tied_to_checkout_path(tmp_path, monkeypatch):
    env_file = tmp_path / "clientplatform.env"
    env_file.write_text(
        "APP_ENV=prod\nCLIENTPLATFORM_DB_ENGINE=postgres\nDATABASE_URL=postgresql://db/clientplatform\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(regression_gate, "PROD_ENV_FILE", env_file)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ALLOW_FULL_REGRESSION_ON_PROD", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("CLIENTPLATFORM_DB_ENGINE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert regression_gate._is_live_prod_host() is True


def test_ci_never_uses_prod_host_guard(tmp_path, monkeypatch):
    env_file = tmp_path / "clientplatform.env"
    env_file.write_text("APP_ENV=prod\nCLIENTPLATFORM_DB_ENGINE=postgres\n", encoding="utf-8")
    monkeypatch.setattr(regression_gate, "PROD_ENV_FILE", env_file)
    monkeypatch.setenv("CI", "1")

    assert regression_gate._is_live_prod_host() is False


def test_non_prod_env_file_does_not_trigger_host_guard(tmp_path, monkeypatch):
    env_file = tmp_path / "clientplatform.env"
    env_file.write_text("APP_ENV=stage\nCLIENTPLATFORM_DB_ENGINE=postgres\n", encoding="utf-8")
    monkeypatch.setattr(regression_gate, "PROD_ENV_FILE", env_file)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ALLOW_FULL_REGRESSION_ON_PROD", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("CLIENTPLATFORM_DB_ENGINE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert regression_gate._is_live_prod_host() is False
