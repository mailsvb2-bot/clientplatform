from __future__ import annotations

import importlib
from pathlib import Path

preflight = importlib.import_module("scripts.clientplatform_program_media_preflight")

ROOT = Path(__file__).resolve().parents[1]


def production_env() -> dict[str, str]:
    return {
        "APP_ENV": "prod",
        "CLIENTPLATFORM_ENVIRONMENT": "production",
        "CLIENTPLATFORM_CONTROL_BOT_ENABLED": "1",
        "CLIENTPLATFORM_PROGRAM_MEDIA_INGEST_ENABLED": "1",
        "CLIENTPLATFORM_MEDIA_GATEWAY_ENABLED": "1",
        "CLIENTPLATFORM_MEDIA_GATEWAY_STORAGE_MODE": "s3",
        "CLIENTPLATFORM_MEDIA_GATEWAY_BASE_URL": (
            "https://clientplatform.example.test/clientplatform"
        ),
        "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT": "https://s3.example.test",
        "CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION": "test-1",
        "CLIENTPLATFORM_STORAGE_BUCKET": "clientplatform-production",
        "CLIENTPLATFORM_MEDIA_GATEWAY_ALLOWED_BUCKETS": "clientplatform-production",
        "CLIENTPLATFORM_SECRET_S3_ACCESS_KEY": "dedicated-access-key",
        "CLIENTPLATFORM_SECRET_S3_SECRET_KEY": "dedicated-secret-key",
        "CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY": "x" * 48,
        "CLIENTPLATFORM_PROGRAM_MEDIA_MAX_BYTES": "20000000",
        "CLIENTPLATFORM_PROGRAM_MEDIA_TIMEOUT_SEC": "30",
    }


def test_production_media_contract_accepts_complete_configuration() -> None:
    assert preflight.validate_environment(production_env()) == []


def test_production_media_contract_fails_closed() -> None:
    disabled = {
        **production_env(),
        "CLIENTPLATFORM_PROGRAM_MEDIA_INGEST_ENABLED": "0",
    }
    assert any(
        "PROGRAM_MEDIA_INGEST_ENABLED" in error
        for error in preflight.validate_environment(disabled)
    )

    oversized = {
        **production_env(),
        "CLIENTPLATFORM_PROGRAM_MEDIA_MAX_BYTES": "20000001",
    }
    assert any(
        "PROGRAM_MEDIA_MAX_BYTES" in error
        for error in preflight.validate_environment(oversized)
    )

    shared_bucket = {
        **production_env(),
        "CLIENTPLATFORM_STORAGE_BUCKET": "shared-production-media",
        "CLIENTPLATFORM_MEDIA_GATEWAY_ALLOWED_BUCKETS": "shared-production-media",
    }
    assert any(
        "dedicated to production" in error
        for error in preflight.validate_environment(shared_bucket)
    )


def test_nonproduction_and_disabled_control_bot_do_not_require_ingest() -> None:
    assert preflight.validate_environment({"APP_ENV": "dev"}) == []
    assert (
        preflight.validate_environment(
            {
                "APP_ENV": "prod",
                "CLIENTPLATFORM_ENVIRONMENT": "production",
                "CLIENTPLATFORM_CONTROL_BOT_ENABLED": "0",
            }
        )
        == []
    )


def test_deployment_contract_runs_media_preflight_before_application() -> None:
    compose = (ROOT / "deploy/clientplatform/compose.production.yml").read_text(
        encoding="utf-8"
    )
    image_entrypoint = (
        ROOT / "deploy/clientplatform/container-entrypoint.sh"
    ).read_text(encoding="utf-8")
    service = (ROOT / "deploy/clientplatform/clientplatform.service").read_text(
        encoding="utf-8"
    )
    env_example = (
        ROOT / "deploy/clientplatform/clientplatform.production.env.example"
    ).read_text(encoding="utf-8")

    media_preflight = "python -m scripts.clientplatform_program_media_preflight"
    application = "exec python main.py"
    assert media_preflight in image_entrypoint
    assert image_entrypoint.index(media_preflight) < image_entrypoint.index(application)
    assert "clientplatform_program_media_preflight.py" not in compose
    assert (
        "scripts/clientplatform_program_media_preflight.py --env-file "
        "/etc/clientplatform/clientplatform.env"
    ) in service
    assert "CLIENTPLATFORM_PROGRAM_MEDIA_INGEST_ENABLED=1" in env_example
    assert "CLIENTPLATFORM_PROGRAM_MEDIA_MAX_BYTES=20000000" in env_example
