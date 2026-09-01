from __future__ import annotations

from pathlib import Path

from core.environment import is_production_env, normalize_app_env


ROOT = Path(__file__).resolve().parents[1]


def test_production_aliases_share_one_canonical_value(monkeypatch) -> None:
    assert normalize_app_env("prod") == "prod"
    assert normalize_app_env(" production ") == "prod"
    assert is_production_env("prod") is True
    assert is_production_env("production") is True
    assert is_production_env("stage") is False

    monkeypatch.setenv("APP_ENV", "production")
    assert normalize_app_env() == "prod"
    assert is_production_env() is True


def test_runtime_uses_canonical_production_and_single_chart_router() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "production = is_production_env(app_env)" in source
    assert "or production" in source
    assert "if production:" in source
    assert "post_chart.router" not in source

    assert not (ROOT / "handlers" / "mood.py").exists()
    assert "dp.include_router(clientplatform_entry.router)" in source
