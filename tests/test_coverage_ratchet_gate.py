from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from scripts.coverage_gate import CoverageBaseline, _load_baseline_payload, ratchet_errors

ROOT = Path(__file__).resolve().parents[1]


def _baseline(
    percent: float,
    *,
    branch_percent: float | None = None,
    tolerance: float = 0.01,
    update_on_improvement: bool = True,
) -> CoverageBaseline:
    return CoverageBaseline(
        total_percent=percent,
        branch_percent=branch_percent,
        comparison_tolerance=tolerance,
        require_update_on_improvement=update_on_improvement,
    )


def test_ratchet_rejects_coverage_regression() -> None:
    errors = ratchet_errors(79.98, _baseline(80.00))
    assert any("regressed below" in error for error in errors)


def test_ratchet_rejects_lowering_tracked_baseline() -> None:
    errors = ratchet_errors(82.00, _baseline(79.00), _baseline(80.00))
    assert any("tracked coverage baseline was lowered" in error for error in errors)


def test_ratchet_requires_locking_meaningful_improvement() -> None:
    errors = ratchet_errors(80.25, _baseline(80.00))
    assert any("total_percent" in error for error in errors)


def test_ratchet_accepts_measurement_within_tolerance() -> None:
    assert ratchet_errors(79.995, _baseline(80.00)) == []


def test_bootstrap_baseline_allows_initial_measurement() -> None:
    assert ratchet_errors(64.25, _baseline(0.0, update_on_improvement=False)) == []


def test_branch_ratchet_rejects_regression_independently() -> None:
    errors = ratchet_errors(
        61.62,
        _baseline(70.16, branch_percent=61.65),
        metric="branch",
    )
    assert any("branch coverage regressed below" in error for error in errors)


def test_branch_ratchet_rejects_lowering_tracked_baseline() -> None:
    errors = ratchet_errors(
        65.00,
        _baseline(72.00, branch_percent=60.00),
        _baseline(71.00, branch_percent=61.65),
        metric="branch",
    )
    assert any("tracked branch coverage baseline was lowered" in error for error in errors)


def test_branch_ratchet_requires_locking_improvement() -> None:
    errors = ratchet_errors(
        61.80,
        _baseline(70.16, branch_percent=61.65),
        metric="branch",
    )
    assert any("branch_percent" in error for error in errors)


def test_branch_ratchet_accepts_old_base_without_branch_metric() -> None:
    errors = ratchet_errors(
        61.65,
        _baseline(70.16, branch_percent=61.65),
        _baseline(70.07, branch_percent=None),
        metric="branch",
    )
    assert errors == []


def test_baseline_payload_rejects_noncanonical_source() -> None:
    with pytest.raises(ValueError, match="canonical production surface"):
        _load_baseline_payload(
            {
                "schema_version": 2,
                "total_percent": 80,
                "branch_percent": 70,
                "comparison_tolerance": 0.01,
                "require_update_on_improvement": True,
                "source": ["core"],
            }
        )


def test_current_baseline_requires_branch_metric() -> None:
    payload = {
        "schema_version": 1,
        "total_percent": 80,
        "comparison_tolerance": 0.01,
        "require_update_on_improvement": True,
        "source": ["services", "handlers", "core", "runtime", "config"],
    }
    legacy = _load_baseline_payload(payload)
    assert legacy.branch_percent is None
    with pytest.raises(ValueError, match="branch_percent"):
        _load_baseline_payload(payload, require_branch=True)


def test_coverage_configuration_and_ci_contract() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    coverage_run = config["tool"]["coverage"]["run"]
    assert coverage_run["branch"] is True
    assert coverage_run["source"] == ["services", "handlers", "core", "runtime", "config"]

    baseline = json.loads((ROOT / "coverage-baseline.json").read_text(encoding="utf-8"))
    assert baseline["schema_version"] == 2
    assert baseline["measurement"] == "combined coverage with an independent branch coverage ratchet"
    assert baseline["total_percent"] == 70.32
    assert baseline["branch_percent"] == 61.86

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "python scripts/coverage_gate.py" in workflow
    assert "coverage-reports" in workflow
    assert "ci/coverage-ratchet" in workflow
    assert "ci/branch-coverage-ratchet" in workflow
    assert "branch_baseline_percent" in workflow
    assert "requirements-coverage.txt" in workflow
