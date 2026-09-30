"""Validate that a mobile live-E2E runner controls the declared real device."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess  # nosec B404 - fixed local device tools, shell disabled
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_live_e2e_manifest import load_manifest, validate_manifest  # noqa: E402


class DeviceValidationError(RuntimeError):
    pass


def _truthy(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _require(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise DeviceValidationError(f"missing_environment:{name}")
    return value


def _run(command: list[str], *, timeout: int = 20) -> str:
    completed = subprocess.run(  # nosec B603 - argv only; no shell
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode:
        raise DeviceValidationError(
            f"device_command_failed:{Path(command[0]).name}:{completed.returncode}"
        )
    return output


def _tool(env_name: str, default: str) -> str:
    configured = str(os.getenv(env_name) or "").strip()
    candidate = configured or default
    resolved = shutil.which(candidate)
    if resolved:
        return resolved
    path = Path(candidate)
    if path.is_file():
        return str(path.resolve())
    raise DeviceValidationError(f"device_tool_missing:{env_name}:{default}")


def _target() -> dict[str, Any]:
    raw = load_manifest()
    validate_manifest(raw)
    target_id = _require("CLIENTPLATFORM_E2E_TARGET_ID")
    targets = raw["mobile_runner"]["targets"]
    matches = [item for item in targets if item.get("id") == target_id]
    if len(matches) != 1:
        raise DeviceValidationError(f"unknown_mobile_target:{target_id}")
    return dict(matches[0])


def _host_label() -> str:
    system = platform.system()
    return {"Darwin": "macOS"}.get(system, system)


def _safety_preflight(target: dict[str, Any]) -> None:
    if not _truthy(os.getenv("CLIENTPLATFORM_LIVE_E2E")):
        raise DeviceValidationError("CLIENTPLATFORM_LIVE_E2E_must_be_1")
    if str(os.getenv("APP_ENV") or "").strip().casefold() != "staging":
        raise DeviceValidationError("APP_ENV_must_be_staging")
    if not _truthy(os.getenv("CLIENTPLATFORM_LIVE_E2E_TEST_ACCOUNTS")):
        raise DeviceValidationError("dedicated_test_accounts_not_confirmed")
    if _truthy(os.getenv("CLIENTPLATFORM_LIVE_E2E_PRODUCTION_CREDENTIALS")):
        raise DeviceValidationError("production_credentials_forbidden")
    if _truthy(os.getenv("CLIENTPLATFORM_LIVE_E2E_REAL_MONEY")):
        raise DeviceValidationError("real_money_forbidden")
    if _host_label() != target["host_label"]:
        raise DeviceValidationError(
            f"mobile_runner_host_mismatch:expected={target['host_label']}:actual={_host_label()}"
        )


def _appium_status() -> None:
    base = str(os.getenv("CLIENTPLATFORM_E2E_APPIUM_URL") or "http://127.0.0.1:4723").rstrip("/")
    try:
        with urllib.request.urlopen(base + "/status", timeout=8) as response:  # nosec B310 - loopback/admin configured
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise DeviceValidationError("appium_status_unavailable") from exc
    value = payload.get("value") if isinstance(payload, dict) else None
    if isinstance(value, dict) and value.get("ready") is False:
        raise DeviceValidationError("appium_not_ready")


def _adb_shell(adb: str, udid: str, *args: str) -> str:
    return _run([adb, "-s", udid, "shell", *args])


def _smallest_width_dp(size_text: str, density_text: str) -> float:
    size_matches = re.findall(r"(\d+)x(\d+)", size_text)
    density_matches = re.findall(r"(\d+)", density_text)
    if not size_matches or not density_matches:
        raise DeviceValidationError("android_display_metrics_unavailable")
    width, height = (int(value) for value in size_matches[0])
    density = int(density_matches[0])
    if density <= 0:
        raise DeviceValidationError("android_density_invalid")
    return min(width, height) * 160.0 / density


def classify_android_target(*, manufacturer: str, features: str, smallest_width_dp: float) -> str:
    folded = manufacturer.casefold()
    if "org.chromium.arc" in features:
        return "chromeos-tablet"
    if "amazon" in folded:
        return "fireos-tablet"
    return "android-tablet" if smallest_width_dp >= 600.0 else "android-phone"


def _validate_android_family(target: dict[str, Any], udid: str) -> dict[str, Any]:
    adb = _tool("CLIENTPLATFORM_E2E_ADB_EXE", "adb")
    devices = _run([adb, "devices"])
    connected = {
        parts[0]: parts[1]
        for line in devices.splitlines()[1:]
        if len(parts := line.split()) >= 2
    }
    if connected.get(udid) != "device":
        raise DeviceValidationError("adb_device_not_ready")

    if udid.startswith("emulator-"):
        raise DeviceValidationError("real_device_required:android_emulator_serial")
    qemu = {
        _adb_shell(adb, udid, "getprop", "ro.kernel.qemu").strip(),
        _adb_shell(adb, udid, "getprop", "ro.boot.qemu").strip(),
    }
    if "1" in qemu:
        raise DeviceValidationError("real_device_required:android_qemu")

    manufacturer = _adb_shell(adb, udid, "getprop", "ro.product.manufacturer").strip()
    model = _adb_shell(adb, udid, "getprop", "ro.product.model").strip()
    os_version = _adb_shell(adb, udid, "getprop", "ro.build.version.release").strip()
    features = _adb_shell(adb, udid, "pm", "list", "features")
    size = _adb_shell(adb, udid, "wm", "size")
    density = _adb_shell(adb, udid, "wm", "density")
    swdp = _smallest_width_dp(size, density)
    actual_target = classify_android_target(
        manufacturer=manufacturer,
        features=features,
        smallest_width_dp=swdp,
    )
    if actual_target != target["id"]:
        raise DeviceValidationError(
            f"mobile_target_identity_mismatch:expected={target['id']}:actual={actual_target}"
        )
    _appium_status()
    return {
        "manufacturer": manufacturer,
        "model": model,
        "os_version": os_version,
        "smallest_width_dp": round(swdp, 1),
    }


def _validate_apple(target: dict[str, Any], udid: str) -> dict[str, Any]:
    xcrun = _tool("CLIENTPLATFORM_E2E_XCRUN_EXE", "xcrun")
    listing = _run([xcrun, "xctrace", "list", "devices"])
    real_section = listing.split("== Simulators ==", 1)[0]
    line = next((item.strip() for item in real_section.splitlines() if udid in item), "")
    if not line:
        raise DeviceValidationError("real_apple_device_not_found")
    expected_model = "iPhone" if target["form_factor"] == "phone" else "iPad"
    if expected_model.casefold() not in line.casefold():
        raise DeviceValidationError(
            f"apple_form_factor_mismatch:expected={expected_model}:actual={line}"
        )
    _appium_status()
    return {"device_line": line.replace(udid, "<redacted>")[:240]}


def _validate_harmony(target: dict[str, Any], udid: str) -> dict[str, Any]:
    hdc = _tool("CLIENTPLATFORM_E2E_HDC_EXE", "hdc")
    targets = _run([hdc, "list", "targets"])
    if udid not in {line.strip().split()[0] for line in targets.splitlines() if line.strip()}:
        raise DeviceValidationError("harmony_device_not_connected")
    if not _truthy(os.getenv("CLIENTPLATFORM_E2E_HARMONY_REAL_DEVICE")):
        raise DeviceValidationError("harmony_real_device_attestation_required")
    declared_kind = _require("CLIENTPLATFORM_E2E_HARMONY_DEVICE_KIND").casefold()
    if declared_kind != str(target["form_factor"]).casefold():
        raise DeviceValidationError("harmony_form_factor_attestation_mismatch")
    echo = _run([hdc, "-t", udid, "shell", "echo", "clientplatform-e2e"])
    if "clientplatform-e2e" not in echo:
        raise DeviceValidationError("harmony_shell_probe_failed")
    runner = Path(_require("CLIENTPLATFORM_E2E_HYPIUM_RUNNER"))
    if not runner.is_file():
        raise DeviceValidationError("hypium_runner_missing")
    return {"hypium_runner": runner.name, "form_factor": declared_kind}


def main() -> int:
    target = _target()
    _safety_preflight(target)
    udid = _require("CLIENTPLATFORM_E2E_DEVICE_UDID")

    if target["device_os"] in {"android", "fireos", "chromeos"}:
        details = _validate_android_family(target, udid)
    elif target["device_os"] in {"ios", "ipados"}:
        details = _validate_apple(target, udid)
    elif target["device_os"] == "harmonyos":
        details = _validate_harmony(target, udid)
    else:
        raise DeviceValidationError(f"unsupported_mobile_os:{target['device_os']}")

    identity = {
        "target_id": target["id"],
        "device_os": target["device_os"],
        "form_factor": target["form_factor"],
        "driver": target["driver"],
        "device_ref_hash": hashlib.sha256(udid.encode("utf-8")).hexdigest()[:16],
        "details": details,
    }
    identity_path = Path(_require("CLIENTPLATFORM_E2E_DEVICE_IDENTITY")).resolve()
    identity_path.write_text(
        json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        "CLIENTPLATFORM_MOBILE_DEVICE_OK "
        + json.dumps(
            {
                "target_id": target["id"],
                "device_os": target["device_os"],
                "form_factor": target["form_factor"],
                "device_ref_hash": identity["device_ref_hash"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
