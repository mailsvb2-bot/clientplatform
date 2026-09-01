from __future__ import annotations

import pytest

from services.validators.base import ValidationError
from services.validators.prod import validate_prod_guardrails


def _prod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setenv("VALIDATOR_RELEASE_MODE", "1")
    monkeypatch.setenv("VALIDATOR_GUARDRAILS_STRICT", "1")
    monkeypatch.setenv("TELEGRAM_TRANSPORT", "polling")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_ENABLED", "0")
    monkeypatch.setenv("CLIENTPLATFORM_DB_ENGINE", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://clientplatform@127.0.0.1:5432/clientplatform")


def test_prod_guardrails_reject_unguarded_prod_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    _prod_env(monkeypatch)
    monkeypatch.setenv("ALLOW_UNGUARDED_PROD", "1")

    with pytest.raises(ValidationError, match="ALLOW_UNGUARDED_PROD"):
        validate_prod_guardrails()
