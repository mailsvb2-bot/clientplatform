from pathlib import Path

from scripts import clientplatform_production_deploy as deploy


def test_failure_reason_exposes_only_sanitized_deployment_errors() -> None:
    safe = deploy.DeploymentError("production_readiness_timeout")
    secret = RuntimeError("password=must-not-leak")

    assert deploy._safe_failure_reason(safe) == "production_readiness_timeout"
    assert deploy._safe_failure_reason(secret) == "unexpected_deployment_error"
    assert "must-not-leak" not in deploy._safe_failure_reason(secret)


def test_recovery_evidence_and_marker_include_safe_failure_reason() -> None:
    source = Path(deploy.__file__).read_text(encoding="utf-8")
    recovery = source[source.index('"operation": "production_recovery_failed"') :]

    assert '"failure_reason": _safe_failure_reason(deployment_error)' in recovery
    assert 'f"{failure_reason}:{recovery_evidence}"' in recovery
