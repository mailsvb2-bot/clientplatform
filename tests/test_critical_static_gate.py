from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts import critical_static_gate


ROOT = Path(__file__).resolve().parents[1]


def test_critical_static_manifest_paths_exist() -> None:
    assert critical_static_gate.missing_critical_paths() == []


def test_critical_static_gate_direct_entrypoint_runs_manifest() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/critical_static_gate.py", "manifest"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "CRITICAL_STATIC_MANIFEST_OK" in proc.stdout


def test_canonical_money_privacy_messenger_sales_and_yandex_boundaries_are_covered() -> None:
    required_type_files = {
        "clientplatform/application/admin_ops.py",
        "clientplatform/application/outcomes.py",
        "clientplatform/application/owner_booking_journey.py",
        "clientplatform/application/sales_agent.py",
        "clientplatform/application/sales_orchestration.py",
        "clientplatform/application/dispatch_worker.py",
        "clientplatform/application/native_messenger_onboarding.py",
        "clientplatform/application/yandex_growth_analytics.py",
        "clientplatform/domain/outcomes.py",
        "clientplatform/infrastructure/outcome_repository.py",
        "clientplatform/infrastructure/revenue_attribution_repository.py",
        "clientplatform/infrastructure/tenancy_repository.py",
        "clientplatform/privacy_manifest.py",
        "clientplatform/runtime/messenger_channel_ingress.py",
        "clientplatform/runtime/native_messenger_http_admission.py",
        "clientplatform/runtime/native_messenger_reconciliation.py",
        "clientplatform/transport/native_messenger.py",
        "handlers/clientplatform_sales.py",
        "handlers/clientplatform_yandex_analytics.py",
        "services/messenger/delivery_outbox.py",
        "services/messenger/webhook_dedupe.py",
        "services/migrations/clientplatform_business_payment_outcomes_v1.py",
    }
    required_security_paths = set(required_type_files)

    assert required_type_files <= set(critical_static_gate.TYPE_CONTRACT_FILES)
    assert required_security_paths <= set(critical_static_gate.SECURITY_SCAN_PATHS)




def test_critical_static_manifest_has_no_duplicates() -> None:
    assert len(critical_static_gate.TYPE_CONTRACT_FILES) == len(
        set(critical_static_gate.TYPE_CONTRACT_FILES)
    )
    assert len(critical_static_gate.SECURITY_SCAN_PATHS) == len(
        set(critical_static_gate.SECURITY_SCAN_PATHS)
    )
