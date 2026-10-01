"""Validate runner-local physical form-factor evidence for real tablet targets."""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 - fixed runner-local executable; shell disabled
from pathlib import Path
from typing import Any


class FormFactorEvidenceError(RuntimeError):
    pass


_KIND_ASSERTION = {
    "multiwindow-state": "multiwindow_verified",
    "window-resize": "window_resized",
    "touchview-transition": "mode_transition_verified",
    "physical-keyboard": "physical_keyboard_verified",
    "suspend-resume": "resume_verified",
}


def _validate_result(plan: dict[str, Any], result: dict[str, Any]) -> None:
    requested = {
        str(item["id"]): str(item["kind"])
        for item in plan["probes"]
    }
    rows = result.get("probes")
    if not isinstance(rows, list):
        raise FormFactorEvidenceError("form_factor_result_probes_missing")

    evidence_root = Path(str(plan["evidence_dir"])).resolve()
    actual: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise FormFactorEvidenceError("form_factor_probe_invalid")
        probe_id = str(row.get("id") or "")
        actual.add(probe_id)
        kind = requested.get(probe_id)
        if not kind:
            raise FormFactorEvidenceError("form_factor_unrequested_probe")
        if row.get("status") != "ok":
            raise FormFactorEvidenceError(f"form_factor_probe_failed:{probe_id}")
        if row.get("state_preserved") is not True:
            raise FormFactorEvidenceError(f"form_factor_state_not_preserved:{probe_id}")
        assertion = _KIND_ASSERTION.get(kind)
        if not assertion or row.get(assertion) is not True:
            raise FormFactorEvidenceError(
                f"form_factor_assertion_missing:{probe_id}:{assertion or kind}"
            )
        screenshot = str(row.get("screenshot") or "")
        if not screenshot:
            raise FormFactorEvidenceError(f"form_factor_screenshot_missing:{probe_id}")
        screenshot_path = (evidence_root / screenshot).resolve()
        if not screenshot_path.is_relative_to(evidence_root) or not screenshot_path.is_file():
            raise FormFactorEvidenceError(f"form_factor_screenshot_invalid:{probe_id}")

    if actual != set(requested):
        raise FormFactorEvidenceError("form_factor_probe_set_mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    args = parser.parse_args()
    plan_path = Path(args.plan).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    result_path = Path(str(plan["result_path"])).resolve()
    evidence_root = Path(str(plan["evidence_dir"])).resolve()
    if not result_path.is_relative_to(evidence_root):
        raise FormFactorEvidenceError("form_factor_result_path_outside_evidence")

    configured = str(os.environ.get("CLIENTPLATFORM_E2E_FORM_FACTOR_RUNNER") or "").strip()
    if not configured:
        raise FormFactorEvidenceError("CLIENTPLATFORM_E2E_FORM_FACTOR_RUNNER_missing")
    runner = Path(configured).resolve()
    if not runner.is_file():
        raise FormFactorEvidenceError("form_factor_runner_missing")
    repo_root = Path(__file__).resolve().parents[3]
    if runner.is_relative_to(repo_root):
        raise FormFactorEvidenceError("form_factor_runner_must_be_runner_local")

    completed = subprocess.run(  # nosec B603 - argv only; shell disabled
        [str(runner), "--plan", str(plan_path), "--result", str(result_path)],
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    if completed.returncode:
        raise FormFactorEvidenceError(
            f"form_factor_runner_failed:{completed.returncode}"
        )
    if not result_path.is_file():
        raise FormFactorEvidenceError("form_factor_result_missing")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise FormFactorEvidenceError("form_factor_result_not_object")
    _validate_result(plan, result)
    print(
        "CLIENTPLATFORM_FORM_FACTOR_EVIDENCE_OK "
        + json.dumps(
            {
                "target_id": plan["target"]["id"],
                "probes": len(plan["probes"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
