"""Strict bridge between ClientPlatform plans and a runner-local DevEco Testing Hypium executor."""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 - fixed runner-local executable, shell disabled
from pathlib import Path
from typing import Any


class HypiumAdapterError(RuntimeError):
    pass


def _validate_result(plan: dict[str, Any], result: dict[str, Any]) -> None:
    expected = {str(item["id"]) for item in plan["probes"]}
    probe_by_id = {str(item["id"]): item for item in plan["probes"]}
    mode = str(plan.get("mode") or "transport")
    rows = result.get("probes")
    if not isinstance(rows, list):
        raise HypiumAdapterError("hypium_result_probes_missing")
    actual: set[str] = set()
    evidence_root = Path(str(plan["evidence_dir"])).resolve()
    for row in rows:
        if not isinstance(row, dict):
            raise HypiumAdapterError("hypium_probe_invalid")
        probe_id = str(row.get("id") or "")
        actual.add(probe_id)
        if row.get("status") != "ok":
            raise HypiumAdapterError(f"hypium_probe_failed:{probe_id}")
        if row.get("expected_text_asserted") is not True:
            raise HypiumAdapterError(f"hypium_expected_text_not_asserted:{probe_id}")
        if mode == "compatibility":
            if row.get("state_preserved") is not True:
                raise HypiumAdapterError(f"hypium_state_not_preserved:{probe_id}")
            probe = probe_by_id.get(probe_id) or {}
            if probe.get("kind") == "orientation-roundtrip":
                if row.get("orientation_verified") is not True:
                    raise HypiumAdapterError(
                        f"hypium_orientation_not_verified:{probe_id}"
                    )
                if probe.get("require_change") is True and row.get("orientation_changed") is not True:
                    raise HypiumAdapterError(
                        f"hypium_orientation_change_not_observed:{probe_id}"
                    )
        screenshot = str(row.get("screenshot") or "")
        if not screenshot:
            raise HypiumAdapterError(f"hypium_screenshot_missing:{probe_id}")
        screenshot_path = (evidence_root / screenshot).resolve()
        if not screenshot_path.is_relative_to(evidence_root) or not screenshot_path.is_file():
            raise HypiumAdapterError(f"hypium_screenshot_invalid:{probe_id}")
    if actual != expected:
        raise HypiumAdapterError("hypium_probe_set_mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    args = parser.parse_args()
    plan_path = Path(args.plan).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    result_path = Path(str(plan["result_path"])).resolve()
    evidence_root = Path(str(plan["evidence_dir"])).resolve()
    if not result_path.is_relative_to(evidence_root):
        raise HypiumAdapterError("hypium_result_path_outside_evidence")

    runner = Path(str(os.environ.get("CLIENTPLATFORM_E2E_HYPIUM_RUNNER") or "")).resolve()
    if not runner.is_file():
        raise HypiumAdapterError("CLIENTPLATFORM_E2E_HYPIUM_RUNNER_missing")
    repo_root = Path(__file__).resolve().parents[3]
    if runner.is_relative_to(repo_root):
        raise HypiumAdapterError("hypium_runner_must_be_runner_local")

    completed = subprocess.run(  # nosec B603 - argv only; shell disabled
        [str(runner), "--plan", str(plan_path), "--result", str(result_path)],
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if completed.returncode:
        raise HypiumAdapterError(f"hypium_runner_failed:{completed.returncode}")
    if not result_path.is_file():
        raise HypiumAdapterError("hypium_result_missing")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise HypiumAdapterError("hypium_result_not_object")
    _validate_result(plan, result)
    print(
        "CLIENTPLATFORM_HYPIUM_PROBES_OK "
        + json.dumps(
            {
                "target_id": plan["target"]["id"],
                "channel": plan["channel"],
                "probes": len(plan["probes"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
