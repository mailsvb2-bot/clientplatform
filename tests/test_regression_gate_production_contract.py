from scripts import regression_gate


def _step(name: str):
    return next(step for step in regression_gate.STEPS if step.name == name)


def test_hermetic_production_contract_is_mandatory() -> None:
    step = _step("hermetic production contract validation")

    assert step.env_file is None
    assert step.skip_if_missing_env_file is False
    assert step.cmd[1] == "-c"
    assert "validate_prod_guardrails" in step.cmd[-1]
    assert "validate_project.py" not in step.cmd[-1]
    assert step.env is regression_gate.HERMETIC_PROD_VALIDATOR_ENV


def test_hermetic_production_contract_uses_fail_closed_prod_settings() -> None:
    env = regression_gate.HERMETIC_PROD_VALIDATOR_ENV

    assert env["APP_ENV"] == "prod"
    assert env["CLIENTPLATFORM_DB_ENGINE"] == "postgres"
    assert env["DATABASE_URL"].startswith("postgresql://")
    assert env["VALIDATOR_RELEASE_MODE"] == "1"
    assert env["VALIDATOR_GUARDRAILS_STRICT"] == "1"
    assert env["TELEGRAM_TRANSPORT"] == "polling"
    assert env["TELEGRAM_WEBHOOK_ENABLED"] == "0"
    assert env["TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED"] == "0"
    assert env["PRIVACY_EXPORT_HTTP_ENABLED"] == "1"


def test_real_production_env_validation_remains_additional() -> None:
    step = _step("optional prod-config validation")

    assert step.env_file == regression_gate.PROD_ENV_FILE
    assert step.skip_if_missing_env_file is True


def test_mandatory_prod_contract_precedes_optional_real_env_check() -> None:
    names = [step.name for step in regression_gate.STEPS]

    assert names.index("hermetic production contract validation") < names.index(
        "optional prod-config validation"
    )
