from __future__ import annotations

from pathlib import Path

from scripts import clientplatform_prepare_production_env as env_prep


ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = (
    ROOT / "deploy" / "clientplatform" / "clientplatform.production.env.example"
)

EXPECTED_ENV = {
    "TELEGRAM_STARS_PRICING_MODE": "explicit",
    "TELEGRAM_STARS_PRICE_PRACTICE_START_7": "1500",
    "TELEGRAM_STARS_PRICE_PRACTICE_60": "2500",
    "TELEGRAM_STARS_PRICE_PRACTICE_ANTISTRESS_60": "5000",
    "TELEGRAM_STARS_PRICE_PRACTICE_PERSONAL_MONTH": "15000",
}


def _write_minimal_env(path: Path, *, extra: str = "") -> None:
    path.write_text(
        "\n".join(
            (
                "CLIENTPLATFORM_DOMAIN=clientplatform.example.test",
                "CLIENTPLATFORM_STORAGE_BUCKET=clientplatform-production",
                "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT=https://s3.example.test",
                "CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION=region-1",
                "CLIENTPLATFORM_SECRET_S3_ACCESS_KEY=access",
                "CLIENTPLATFORM_SECRET_S3_SECRET_KEY=secret",
                extra,
            )
        ).rstrip()
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_example_environment_uses_the_explicit_stars_ladder() -> None:
    env = ENV_EXAMPLE.read_text(encoding="utf-8")

    for key, value in EXPECTED_ENV.items():
        assert f"{key}={value}" in env
    assert "TELEGRAM_STARS_PRICING_MODE=buyer_parity" not in env


def test_clientplatform_env_preparer_adds_stars_ladder_atomically(tmp_path: Path) -> None:
    env_file = tmp_path / "clientplatform.env"
    _write_minimal_env(env_file)

    added = set(env_prep.prepare(env_file))
    prepared = env_file.read_text(encoding="utf-8")
    backup = env_file.with_name(env_file.name + ".before-current-main")

    assert set(EXPECTED_ENV).issubset(added)
    for key, value in EXPECTED_ENV.items():
        assert f"{key}={value}" in prepared
    assert backup.is_file()
    backup_text = backup.read_text(encoding="utf-8")
    assert "TELEGRAM_STARS_PRICING_MODE=" not in backup_text


def test_clientplatform_env_preparer_preserves_operator_stars_override(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "clientplatform.env"
    override = "TELEGRAM_STARS_PRICE_PRACTICE_60=2750"
    _write_minimal_env(env_file, extra=override)

    added = set(env_prep.prepare(env_file))
    prepared = env_file.read_text(encoding="utf-8")

    assert "TELEGRAM_STARS_PRICE_PRACTICE_60" not in added
    assert override in prepared
    for key, value in EXPECTED_ENV.items():
        if key == "TELEGRAM_STARS_PRICE_PRACTICE_60":
            continue
        assert f"{key}={value}" in prepared
