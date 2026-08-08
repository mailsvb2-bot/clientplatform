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


def test_recent_payment_privacy_messenger_and_sales_boundaries_are_covered() -> None:
    required_type_files = {
        "clientplatform/application/owner_booking_journey.py",
        "clientplatform/application/sales_agent.py",
        "clientplatform/application/sales_orchestration.py",
        "clientplatform/infrastructure/sales_action_repository.py",
        "handlers/clientplatform_sales.py",
        "handlers/info.py",
        "runtime/messenger_ingress_reliability.py",
        "runtime/messenger_media_http.py",
        "runtime/payment_http.py",
        "runtime/payment_webhook_admission.py",
        "services/messenger/audio_access.py",
        "services/messenger/webhook_dedupe.py",
        "services/payments/receipt_contract.py",
        "services/payments/retry_queue.py",
        "services/payments/verified_reconciliation.py",
        "services/privacy_controls.py",
    }
    required_security_paths = (
        required_type_files
        - {
            "services/payments/receipt_contract.py",
            "services/payments/retry_queue.py",
            "services/payments/verified_reconciliation.py",
        }
    ) | {"services/payments"}

    assert required_type_files <= set(critical_static_gate.TYPE_CONTRACT_FILES)
    assert required_security_paths <= set(critical_static_gate.SECURITY_SCAN_PATHS)


def test_critical_static_manifest_has_no_duplicates() -> None:
    assert len(critical_static_gate.TYPE_CONTRACT_FILES) == len(
        set(critical_static_gate.TYPE_CONTRACT_FILES)
    )
    assert len(critical_static_gate.SECURITY_SCAN_PATHS) == len(
        set(critical_static_gate.SECURITY_SCAN_PATHS)
    )
