from __future__ import annotations

from pathlib import Path

from scripts.check_live_e2e_manifest import (
    load_manifest,
    required_native_actions,
    validate_manifest,
)

ROOT = Path(__file__).resolve().parents[1]


def test_live_e2e_manifest_covers_current_native_parity_registry() -> None:
    summary = validate_manifest(load_manifest())
    assert summary["journeys"] >= 14
    assert summary["native_actions"] == len(required_native_actions())
    assert summary["native_actions"] >= 90
    assert summary["live_probes"] >= 17
    assert summary["windows_variants"] == ["windows-10", "windows-11"]
    assert {"telegram", "vk", "max"} <= set(summary["channels"])


def test_live_e2e_workflow_never_runs_on_pull_request_code() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "clientplatform-live-e2e-windows.yml"
    ).read_text(encoding="utf-8")
    assert "pull_request:" not in workflow
    assert "clientplatform-live-e2e" in workflow
    assert "clientplatform-windows-10" in workflow
    assert "clientplatform-windows-11" in workflow
    assert "matrix.os_label" in workflow
    assert "matrix.os_id" in workflow
    assert "environment: clientplatform_live_e2e" in workflow
    assert "ref: main" in workflow
    assert "CLIENTPLATFORM_LIVE_E2E_REAL_MONEY: '0'" in workflow
    assert "CLIENTPLATFORM_LIVE_E2E_PRODUCTION_CREDENTIALS: '0'" in workflow
    assert "validate_runner.ps1" in workflow


def test_scheduled_live_e2e_is_disabled_until_runner_is_explicitly_enabled() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "clientplatform-live-e2e-windows.yml"
    ).read_text(encoding="utf-8")
    assert "vars.CLIENTPLATFORM_LIVE_E2E_ENABLED == '1'" in workflow
    assert "schedule:" in workflow


def test_live_e2e_contract_is_checked_on_hosted_windows_without_credentials() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "clientplatform-live-e2e-contract.yml"
    ).read_text(encoding="utf-8")
    assert "pull_request:" in workflow
    assert "runs-on: windows-2025" in workflow
    assert "scripts/check_live_e2e_manifest.py" in workflow
    assert "scripts/clientplatform_live_e2e.py --plan" in workflow
    assert "secrets." not in workflow


def test_desktop_driver_uses_windows_uia_and_never_exports_profiles() -> None:
    driver = (
        ROOT / "scripts" / "live_e2e" / "windows" / "desktop_driver.ps1"
    ).read_text(encoding="utf-8")
    assert "UIAutomationClient" in driver
    assert "AutomationElement" in driver
    assert "Save-Screenshot" in driver
    assert "ConvertTo-Json" in driver
    assert "browser profile" not in driver.casefold()
    assert "cookies" not in driver.casefold()
    assert "Invoke-Expression" not in driver
    assert "iex " not in driver.casefold()


def test_live_checklist_no_longer_uses_imported_consumer_smoke_as_evidence() -> None:
    checklist = (ROOT / "docs" / "MESSENGER_LIVE_SMOKE_CHECKLIST.md").read_text(
        encoding="utf-8"
    )
    for stale in (
        "Практика на утро",
        "Практика на вечер",
        "Мой прогресс",
        "Погода",
        "шкала -10",
    ):
        assert stale not in checklist
    assert "Owner entry" in checklist
    assert "Cross-channel parity" in checklist
    assert "Cross-tenant isolation" in checklist


def test_runner_validator_requires_real_windows_10_or_11_x64_interactive_host() -> None:
    validator = (
        ROOT / "scripts" / "live_e2e" / "windows" / "validate_runner.ps1"
    ).read_text(encoding="utf-8")
    assert "CLIENTPLATFORM_E2E_EXPECTED_WINDOWS" in validator
    assert "live_e2e_manifest.json" in validator
    assert "caption_pattern" in validator
    assert "minimum_build" in validator
    assert "BuildNumber" in validator
    assert "OSArchitecture" in validator
    assert "SessionId" in validator
    assert "Runner.Listener" in validator
    assert "github_actions_runner_must_not_run_as_service" in validator
    for variable in (
        "CLIENTPLATFORM_E2E_TELEGRAM_EXE",
        "CLIENTPLATFORM_E2E_MAX_EXE",
        "CLIENTPLATFORM_E2E_EDGE_EXE",
        "CLIENTPLATFORM_E2E_CHROME_EXE",
    ):
        assert variable in validator


def test_live_driver_requires_new_expected_text_not_merely_any_ui_change() -> None:
    driver = (
        ROOT / "scripts" / "live_e2e" / "windows" / "desktop_driver.ps1"
    ).read_text(encoding="utf-8")
    assert "Get-TextOccurrenceCount" in driver
    assert "$afterExpected -gt $beforeExpected" in driver
    assert "$last.Hash -ne $Before.Hash" in driver
    assert "provider_response_assertion_timeout" in driver
    assert "$plan.actions" not in driver
    assert "$plan.probes" in driver


def test_live_orchestrator_reports_semantic_and_live_evidence_separately() -> None:
    source = (ROOT / "scripts" / "clientplatform_live_e2e.py").read_text(
        encoding="utf-8"
    )
    assert '"semantic_contract"' in source
    assert '"live_transport"' in source
    assert '"hermetic_registry_coverage"' in source
    assert '"runner_identity"' in source
    assert "CLIENTPLATFORM_E2E_RUNNER_IDENTITY" in source
    assert 'raw["live_transport_probes"]' in source


def test_imported_consumer_messenger_fixtures_are_not_live_e2e_evidence() -> None:
    retired = (
        "max_button_callback_score_1.json",
        "max_message_created_weather.json",
        "vk_message_new_button_demo.json",
        "vk_message_new_score_plus_one.json",
    )
    fixture_root = ROOT / "tests" / "fixtures" / "messenger"
    for name in retired:
        assert not (fixture_root / name).exists()
