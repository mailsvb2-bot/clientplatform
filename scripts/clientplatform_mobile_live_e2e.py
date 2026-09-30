"""Execute protected ClientPlatform live E2E on real phones and tablets."""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 - fixed repository-owned drivers
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_live_e2e_manifest import load_manifest, validate_manifest  # noqa: E402

APPIUM_DRIVER = ROOT / "scripts" / "live_e2e" / "mobile" / "appium_driver.py"
HYPIUM_ADAPTER = ROOT / "scripts" / "live_e2e" / "mobile" / "hypium_adapter.py"
FORM_FACTOR_ADAPTER = ROOT / "scripts" / "live_e2e" / "mobile" / "form_factor_adapter.py"
_PROFILE_ENV = {
    "telegram": "CLIENTPLATFORM_E2E_TELEGRAM_MOBILE_PROFILE",
    "vk": "CLIENTPLATFORM_E2E_VK_MOBILE_PROFILE",
    "max": "CLIENTPLATFORM_E2E_MAX_MOBILE_PROFILE",
    "cockpit": "CLIENTPLATFORM_E2E_COCKPIT_MOBILE_PROFILE",
}
_FORBIDDEN_PROFILE_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "authorization",
    "credential",
}


class MobileLiveE2EError(RuntimeError):
    pass


@dataclass(frozen=True)
class SurfaceProbe:
    channel: str
    status: str
    actions_checked: int
    detail: str = ""


def _truthy(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _require(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise MobileLiveE2EError(f"missing_environment:{name}")
    return value


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            folded = str(key).casefold()
            if any(fragment in folded for fragment in _FORBIDDEN_PROFILE_KEYS):
                return True
            if _contains_forbidden_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _target(raw: dict[str, Any]) -> dict[str, Any]:
    target_id = _require("CLIENTPLATFORM_E2E_TARGET_ID")
    matches = [
        item
        for item in raw["mobile_runner"]["targets"]
        if str(item.get("id") or "") == target_id
    ]
    if len(matches) != 1:
        raise MobileLiveE2EError(f"unknown_mobile_target:{target_id}")
    return dict(matches[0])


def _load_profiles(target: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for surface in target["required_surfaces"]:
        env_name = _PROFILE_ENV[str(surface)]
        raw_value = _require(env_name)
        try:
            profile = json.loads(raw_value)
        except ValueError as exc:
            raise MobileLiveE2EError(f"invalid_mobile_profile_json:{surface}") from exc
        if not isinstance(profile, dict):
            raise MobileLiveE2EError(f"mobile_profile_not_object:{surface}")
        if _contains_forbidden_key(profile):
            raise MobileLiveE2EError(f"mobile_profile_contains_secret_like_key:{surface}")
        profiles[str(surface)] = profile
    return profiles


def _preflight(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    if not _truthy(os.getenv("CLIENTPLATFORM_LIVE_E2E")):
        raise MobileLiveE2EError("CLIENTPLATFORM_LIVE_E2E_must_be_1")
    if str(os.getenv("APP_ENV") or "").strip().casefold() != "staging":
        raise MobileLiveE2EError("APP_ENV_must_be_staging")
    if not _truthy(os.getenv("CLIENTPLATFORM_LIVE_E2E_TEST_ACCOUNTS")):
        raise MobileLiveE2EError("dedicated_test_accounts_not_confirmed")
    if _truthy(os.getenv("CLIENTPLATFORM_LIVE_E2E_PRODUCTION_CREDENTIALS")):
        raise MobileLiveE2EError("production_credentials_forbidden")
    if _truthy(os.getenv("CLIENTPLATFORM_LIVE_E2E_REAL_MONEY")):
        raise MobileLiveE2EError("real_money_forbidden")

    target = _target(raw)
    identity_path = Path(_require("CLIENTPLATFORM_E2E_DEVICE_IDENTITY")).resolve()
    if not identity_path.is_file():
        raise MobileLiveE2EError("validated_device_identity_missing")
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise MobileLiveE2EError("validated_device_identity_invalid") from exc
    if not isinstance(identity, dict):
        raise MobileLiveE2EError("validated_device_identity_not_object")
    if identity.get("target_id") != target["id"]:
        raise MobileLiveE2EError("device_identity_target_mismatch")
    if identity.get("device_os") != target["device_os"]:
        raise MobileLiveE2EError("device_identity_os_mismatch")
    if identity.get("form_factor") != target["form_factor"]:
        raise MobileLiveE2EError("device_identity_form_factor_mismatch")

    profiles = _load_profiles(target)
    return target, identity, profiles


def _surface_probes(raw: dict[str, Any], surface: str) -> list[dict[str, Any]]:
    if surface == "cockpit":
        return list(raw["live_transport_probes"]["cockpit_chrome"])
    return list(raw["live_transport_probes"][surface])


def _driver_for(target: dict[str, Any]) -> Path:
    driver = str(target["driver"])
    if driver in {"appium-uiautomator2", "appium-xcuitest"}:
        return APPIUM_DRIVER
    if driver == "deveco-hypium":
        return HYPIUM_ADAPTER
    raise MobileLiveE2EError(f"unsupported_mobile_driver:{driver}")


def _run_surface(
    *,
    target: dict[str, Any],
    channel: str,
    probes: list[dict[str, Any]],
    profile: dict[str, Any],
    evidence_dir: Path,
) -> SurfaceProbe:
    channel_dir = evidence_dir / channel
    channel_dir.mkdir(parents=True, exist_ok=True)
    result_path = channel_dir / "result.json"
    plan = {
        "target": target,
        "channel": channel,
        "probes": probes,
        "profile": profile,
        "evidence_dir": str(channel_dir),
        "result_path": str(result_path),
    }
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", delete=False
    ) as handle:
        json.dump(plan, handle, ensure_ascii=False)
        plan_path = Path(handle.name)

    driver = _driver_for(target)
    try:
        completed = subprocess.run(  # nosec B603 - fixed Python + repository driver
            [sys.executable, str(driver), "--plan", str(plan_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=360,
            check=False,
        )
    finally:
        plan_path.unlink(missing_ok=True)

    if completed.returncode or not result_path.is_file():
        return SurfaceProbe(
            channel=channel,
            status="failed",
            actions_checked=0,
            detail=f"driver_failed:{completed.returncode}",
        )

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SurfaceProbe(
            channel=channel,
            status="failed",
            actions_checked=0,
            detail="driver_result_invalid",
        )
    rows = result.get("probes") if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) != len(probes):
        return SurfaceProbe(
            channel=channel,
            status="failed",
            actions_checked=0,
            detail="driver_probe_count_mismatch",
        )
    passed = all(
        isinstance(row, dict)
        and row.get("status") == "ok"
        and row.get("expected_text_asserted") is True
        for row in rows
    )
    return SurfaceProbe(
        channel=channel,
        status="ok" if passed else "failed",
        actions_checked=len(probes) if passed else 0,
        detail="expected_text_evidence_ok" if passed else "driver_probe_evidence_failed",
    )


def _run_behavior_plan(
    *,
    target: dict[str, Any],
    group: str,
    mode: str,
    probes: list[dict[str, Any]],
    profile: dict[str, Any],
    state_anchor: str,
    evidence_dir: Path,
    driver: Path,
) -> SurfaceProbe:
    group_dir = evidence_dir / group
    group_dir.mkdir(parents=True, exist_ok=True)
    result_path = group_dir / "result.json"
    plan = {
        "target": target,
        "channel": group,
        "mode": mode,
        "probes": probes,
        "profile": profile,
        "state_anchor": state_anchor,
        "evidence_dir": str(group_dir),
        "result_path": str(result_path),
    }
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", delete=False
    ) as handle:
        json.dump(plan, handle, ensure_ascii=False)
        plan_path = Path(handle.name)

    try:
        completed = subprocess.run(  # nosec B603 - fixed repository driver
            [sys.executable, str(driver), "--plan", str(plan_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=660,
            check=False,
        )
    finally:
        plan_path.unlink(missing_ok=True)

    if completed.returncode or not result_path.is_file():
        return SurfaceProbe(
            channel=group,
            status="failed",
            actions_checked=0,
            detail=f"driver_failed:{completed.returncode}",
        )
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SurfaceProbe(
            channel=group,
            status="failed",
            actions_checked=0,
            detail="driver_result_invalid",
        )
    rows = result.get("probes") if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) != len(probes):
        return SurfaceProbe(
            channel=group,
            status="failed",
            actions_checked=0,
            detail="driver_probe_count_mismatch",
        )
    passed = all(
        isinstance(row, dict)
        and row.get("status") == "ok"
        and row.get("state_preserved") is True
        for row in rows
    )
    if mode == "compatibility":
        passed = passed and all(
            row.get("expected_text_asserted") is True
            for row in rows
            if isinstance(row, dict)
        )
    return SurfaceProbe(
        channel=group,
        status="ok" if passed else "failed",
        actions_checked=len(probes) if passed else 0,
        detail="behavior_evidence_ok" if passed else "behavior_evidence_failed",
    )


def execute(evidence_dir: Path) -> int:
    raw = load_manifest()
    contract = validate_manifest(raw)
    target, identity, profiles = _preflight(raw)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    probes: list[SurfaceProbe] = []
    for surface in target["required_surfaces"]:
        surface_name = str(surface)
        probes.append(
            _run_surface(
                target=target,
                channel=surface_name,
                probes=_surface_probes(raw, surface_name),
                profile=profiles[surface_name],
                evidence_dir=evidence_dir,
            )
        )

    state_anchor = str(raw["live_transport_probes"]["telegram"][0]["expect"])
    compatibility = _run_behavior_plan(
        target=target,
        group="device-compatibility",
        mode="compatibility",
        probes=list(target["compatibility_probes"]),
        profile=profiles["telegram"],
        state_anchor=state_anchor,
        evidence_dir=evidence_dir,
        driver=_driver_for(target),
    )
    form_factor_probes = list(target.get("form_factor_probes") or ())
    form_factor = None
    if form_factor_probes:
        form_factor = _run_behavior_plan(
            target=target,
            group="form-factor",
            mode="form-factor",
            probes=form_factor_probes,
            profile=profiles["telegram"],
            state_anchor=state_anchor,
            evidence_dir=evidence_dir,
            driver=FORM_FACTOR_ADAPTER,
        )

    failed = [item for item in probes if item.status != "ok"]
    if compatibility.status != "ok":
        failed.append(compatibility)
    if form_factor is not None and form_factor.status != "ok":
        failed.append(form_factor)
    report = {
        "suite": raw["suite"],
        "target_id": target["id"],
        "device_identity": identity,
        "safety": {
            "test_accounts": True,
            "staging_only": True,
            "real_money": False,
        },
        "semantic_contract": {
            "journeys": contract["journeys"],
            "native_actions": contract["native_actions"],
            "claim": "hermetic_registry_coverage",
        },
        "live_transport": [asdict(item) for item in probes],
        "device_compatibility": asdict(compatibility),
        "form_factor": (
            asdict(form_factor)
            if form_factor is not None
            else {
                "channel": "form-factor",
                "status": "not-required",
                "actions_checked": 0,
                "detail": "no_form_factor_probes_for_target",
            }
        ),
        "summary": {
            "surfaces": len(probes),
            "failed_evidence_groups": len(failed),
            "asserted_live_probes": sum(item.actions_checked for item in probes),
            "compatibility_probes": compatibility.actions_checked,
            "form_factor_probes": (
                form_factor.actions_checked if form_factor is not None else 0
            ),
        },
    }
    (evidence_dir / "mobile-live-e2e-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        "CLIENTPLATFORM_MOBILE_LIVE_E2E_"
        + ("FAILED" if failed else "OK")
        + " "
        + json.dumps(
            {"target_id": target["id"], **report["summary"]},
            sort_keys=True,
        )
    )
    return 2 if failed else 0


def print_plan() -> int:
    raw = load_manifest()
    summary = validate_manifest(raw)
    print("CLIENTPLATFORM_MOBILE_LIVE_E2E_PLAN " + json.dumps(summary, sort_keys=True))
    for target in raw["mobile_runner"]["targets"]:
        print(
            f"- {target['id']}: host={target['host_label']} "
            f"os={target['device_os']} form_factor={target['form_factor']} "
            f"driver={target['driver']} surfaces={','.join(target['required_surfaces'])} "
            f"compatibility={len(target.get('compatibility_probes') or ())} "
            f"form_factor={len(target.get('form_factor_probes') or ())}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--evidence-dir",
        default=str(ROOT / "artifacts" / "mobile-live-e2e"),
    )
    args = parser.parse_args()
    if int(args.plan) + int(args.preflight) + int(args.execute) != 1:
        parser.error("choose exactly one of --plan, --preflight, --execute")

    raw = load_manifest()
    validate_manifest(raw)
    if args.plan:
        return print_plan()
    target, identity, _ = _preflight(raw)
    if args.preflight:
        print(
            "CLIENTPLATFORM_MOBILE_LIVE_E2E_PREFLIGHT_OK "
            + json.dumps(
                {
                    "target_id": target["id"],
                    "device_ref_hash": identity.get("device_ref_hash"),
                },
                sort_keys=True,
            )
        )
        return 0
    return execute(Path(args.evidence_dir).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
