from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / "deploy/clientplatform/clientplatform.production.env.example"
ENV_PREPARER = ROOT / "scripts/clientplatform_prepare_production_env.py"


def test_canonical_production_environment_disables_yookassa_by_default() -> None:
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()

    assert lines.count("TELEGRAM_YOOKASSA_ENABLED=0") == 1
    assert not any(line.startswith("TELEGRAM_YOOKASSA_ENABLED=1") for line in lines)


def test_canonical_env_preparer_adds_safe_yookassa_default_without_legacy_state() -> None:
    source = ENV_PREPARER.read_text(encoding="utf-8")

    assert '"TELEGRAM_YOOKASSA_ENABLED": "0"' in source
    assert "/var/lib/metrotherapy/deploy-migrations" not in source
    assert "telegram-stars-only-checkout-v1.applied" not in source
    assert "run_deploy_worker.sh" not in source
