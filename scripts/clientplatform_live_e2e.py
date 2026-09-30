"""Execute the protected ClientPlatform Windows live-E2E contour.

The live contour is intentionally separate from PR CI. It may run only on a
trusted interactive Windows self-hosted runner with dedicated staging accounts.
No production credential or real-money branch is admitted.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess  # nosec B404 - fixed repository-owned PowerShell script
import sys
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_live_e2e_manifest import load_manifest, validate_manifest  # noqa: E402
DRIVER = ROOT / "scripts" / "live_e2e" / "windows" / "desktop_driver.ps1"


class LiveE2EPreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChannelProbe:
    channel: str
    journey_id: str
    status: str
    actions_checked: int
    detail: str = ""


def _truthy(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _require(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise LiveE2EPreflightError(f"missing_environment:{name}")
    return value


def _windows_preflight() -> dict[str, str]:
    if platform.system().casefold() != "windows":
        raise LiveE2EPreflightError("live_e2e_requires_windows")
    if not _truthy(os.getenv("CLIENTPLATFORM_LIVE_E2E")):
        raise LiveE2EPreflightError("CLIENTPLATFORM_LIVE_E2E_must_be_1")
    if str(os.getenv("APP_ENV") or "").strip().casefold() != "staging":
        raise LiveE2EPreflightError("APP_ENV_must_be_staging")
    if not _truthy(os.getenv("CLIENTPLATFORM_LIVE_E2E_TEST_ACCOUNTS")):
        raise LiveE2EPreflightError("dedicated_test_accounts_not_confirmed")
    if _truthy(os.getenv("CLIENTPLATFORM_LIVE_E2E_PRODUCTION_CREDENTIALS")):
        raise LiveE2EPreflightError("production_credentials_forbidden")
    if _truthy(os.getenv("CLIENTPLATFORM_LIVE_E2E_REAL_MONEY")):
        raise LiveE2EPreflightError("real_money_forbidden")

    session = str(os.getenv("SESSIONNAME") or "").strip()
    if not session or session.casefold() in {"services", "service"}:
        raise LiveE2EPreflightError("interactive_windows_session_required")
    if not DRIVER.is_file():
        raise LiveE2EPreflightError("desktop_driver_missing")

    return {
        "session": session,
        "telegram_chat": _require("CLIENTPLATFORM_E2E_TELEGRAM_CHAT"),
        "max_chat": _require("CLIENTPLATFORM_E2E_MAX_CHAT"),
        "vk_chat_url": _require("CLIENTPLATFORM_E2E_VK_CHAT_URL"),
        "cockpit_url": _require("CLIENTPLATFORM_E2E_COCKPIT_URL"),
    }


def _run_probe(
    *,
    channel: str,
    probes: list[dict[str, Any]],
    evidence_dir: Path,
) -> ChannelProbe:
    payload = {
        "journey_id": "transport-acceptance",
        "channel": channel,
        "probes": probes,
        "evidence_dir": str(evidence_dir),
    }
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False)
        plan_path = Path(handle.name)

    command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(DRIVER),
        "-PlanPath",
        str(plan_path),
    ]
    try:
        completed = subprocess.run(  # nosec B603 - argv is fixed; no shell
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
    finally:
        plan_path.unlink(missing_ok=True)

    detail = (completed.stdout + "\n" + completed.stderr).strip()[-4000:]
    if completed.returncode:
        return ChannelProbe(
            channel=channel,
            journey_id="transport-acceptance",
            status="failed",
            actions_checked=0,
            detail=detail,
        )
    return ChannelProbe(
        channel=channel,
        journey_id="transport-acceptance",
        status="ok",
        actions_checked=len(probes),
        detail=detail,
    )


def execute(evidence_dir: Path) -> int:
    raw = load_manifest()
    contract = validate_manifest(raw)
    preflight = _windows_preflight()
    evidence_dir.mkdir(parents=True, exist_ok=True)

    probes: list[ChannelProbe] = []
    for channel, channel_probes in raw["live_transport_probes"].items():
        probes.append(
            _run_probe(
                channel=str(channel),
                probes=list(channel_probes),
                evidence_dir=evidence_dir,
            )
        )

    failed = [item for item in probes if item.status != "ok"]
    report = {
        "suite": raw["suite"],
        "preflight": {
            "session": preflight["session"],
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
        "summary": {
            "channels": len(probes),
            "failed_channels": len(failed),
            "asserted_live_probes": sum(item.actions_checked for item in probes),
        },
    }
    (evidence_dir / "live-e2e-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        "CLIENTPLATFORM_LIVE_E2E_"
        + ("FAILED" if failed else "OK")
        + " "
        + json.dumps(report["summary"], sort_keys=True)
    )
    return 2 if failed else 0


def print_plan() -> int:
    raw = load_manifest()
    summary = validate_manifest(raw)
    print("CLIENTPLATFORM_LIVE_E2E_PLAN " + json.dumps(summary, sort_keys=True))
    print("Semantic journeys:")
    for journey in raw["journeys"]:
        print(
            f"- {journey['id']}: channels={','.join(journey['channels'])} "
            f"native_actions={len(journey.get('covers_native_actions') or ())}"
        )
    print("Asserted live transport probes:")
    for channel, probes in raw["live_transport_probes"].items():
        print(f"- {channel}: {len(probes)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--evidence-dir",
        default=str(ROOT / "artifacts" / "live-e2e"),
    )
    args = parser.parse_args()

    modes = int(args.plan) + int(args.preflight) + int(args.execute)
    if modes != 1:
        parser.error("choose exactly one of --plan, --preflight, --execute")
    if args.plan:
        return print_plan()
    validate_manifest(load_manifest())
    if args.preflight:
        data = _windows_preflight()
        print(
            "CLIENTPLATFORM_LIVE_E2E_PREFLIGHT_OK "
            + json.dumps({"session": data["session"]}, sort_keys=True)
        )
        return 0
    return execute(Path(args.evidence_dir).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
