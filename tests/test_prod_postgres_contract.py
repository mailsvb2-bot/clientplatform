from __future__ import annotations

import pytest

from services.validators.base import ValidationError
from services.validators.prod import validate_prod_postgres_contract


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("APP_ENV", "CLIENTPLATFORM_DB_ENGINE", "DATABASE_URL", "ALLOW_SQLITE_IN_PROD"):
        monkeypatch.delenv(name, raising=False)


def test_prod_postgres_contract_rejects_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("CLIENTPLATFORM_DB_ENGINE", "sqlite")

    with pytest.raises(ValidationError, match="Postgres"):
        validate_prod_postgres_contract(strict=True)


def test_prod_postgres_contract_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("CLIENTPLATFORM_DB_ENGINE", "postgres")

    with pytest.raises(ValidationError, match="DATABASE_URL"):
        validate_prod_postgres_contract(strict=True)


def test_prod_postgres_contract_accepts_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("CLIENTPLATFORM_DB_ENGINE", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/clientplatform")

    validate_prod_postgres_contract(strict=True)
