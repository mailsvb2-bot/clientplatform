"""Validate the repository-owned ClientPlatform live E2E contract.

This is deliberately hermetic: it validates coverage declarations and safety
invariants without touching a provider, desktop profile, credential, or live
environment.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "live_e2e_manifest.json"
NATIVE_UI = ROOT / "clientplatform" / "application" / "native_member_interactions.py"

_REQUIRED_CHANNELS = {"telegram", "vk", "max", "cockpit_edge", "cockpit_chrome"}
_REQUIRED_ROLES = {"owner", "member", "customer"}
_REQUIRED_EVIDENCE = {
    "cross_tenant_denial",
    "duplicate_event_replay",
    "restart_recovery",
    "temporary_provider_failure",
}
_PARITY_ASSIGNMENTS = (
    "TELEGRAM_NATIVE_ACTION_EQUIVALENTS",
    "SIMPLE_OWNER_NATIVE_INTENT_EQUIVALENTS",
)


class LiveE2EContractError(RuntimeError):
    pass


def _literal_assignment(path: Path, name: str) -> dict[str, tuple[str, ...]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        target_name = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            target_name = target.id if isinstance(target, ast.Name) else None
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target_name = node.target.id if isinstance(node.target, ast.Name) else None
            value = node.value
        if target_name == name and value is not None:
            parsed = ast.literal_eval(value)
            if not isinstance(parsed, dict):
                break
            return {
                str(key): tuple(str(item) for item in values)
                for key, values in parsed.items()
            }
    raise LiveE2EContractError(f"missing_literal_assignment:{name}")


def required_native_actions() -> set[str]:
    actions: set[str] = set()
    for name in _PARITY_ASSIGNMENTS:
        mapping = _literal_assignment(NATIVE_UI, name)
        for values in mapping.values():
            actions.update(values)
    return actions


def load_manifest() -> dict[str, Any]:
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise LiveE2EContractError("manifest_not_object")
    return raw


def validate_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    if raw.get("schema_version") != 1:
        problems.append("schema_version_must_be_1")

    runner = raw.get("runner")
    if not isinstance(runner, dict):
        problems.append("runner_missing")
        runner = {}
    labels = set(runner.get("labels") or ())
    for label in ("self-hosted", "Windows", "X64", "clientplatform-live-e2e"):
        if label not in labels:
            problems.append(f"runner_label_missing:{label}")
    if runner.get("interactive_session_required") is not True:
        problems.append("interactive_session_required")
    if runner.get("staging_only") is not True:
        problems.append("runner_must_be_staging_only")

    safety = raw.get("safety")
    if not isinstance(safety, dict):
        problems.append("safety_missing")
        safety = {}
    for key in (
        "test_accounts_only",
        "production_credentials_forbidden",
        "real_money_forbidden",
        "cross_product_credentials_forbidden",
    ):
        if safety.get(key) is not True:
            problems.append(f"safety_invariant_missing:{key}")

    channels = raw.get("channels")
    if not isinstance(channels, dict):
        channels = {}
        problems.append("channels_missing")
    missing_channels = sorted(_REQUIRED_CHANNELS - set(channels))
    problems.extend(f"required_channel_missing:{item}" for item in missing_channels)

    live_probes = raw.get("live_transport_probes")
    if not isinstance(live_probes, dict):
        live_probes = {}
        problems.append("live_transport_probes_missing")
    for channel in sorted(_REQUIRED_CHANNELS):
        probes = live_probes.get(channel)
        if not isinstance(probes, list) or not probes:
            problems.append(f"live_transport_probe_missing:{channel}")
            continue
        seen_probe_ids: set[str] = set()
        for probe in probes:
            if not isinstance(probe, dict):
                problems.append(f"live_transport_probe_invalid:{channel}")
                continue
            probe_id = str(probe.get("id") or "").strip()
            user_input = str(probe.get("input") or "").strip()
            expected = str(probe.get("expect") or "").strip()
            if not probe_id:
                problems.append(f"live_transport_probe_id_missing:{channel}")
            elif probe_id in seen_probe_ids:
                problems.append(f"live_transport_probe_duplicate:{channel}:{probe_id}")
            seen_probe_ids.add(probe_id)
            if not user_input:
                problems.append(f"live_transport_probe_input_missing:{channel}:{probe_id}")
            if not expected:
                problems.append(f"live_transport_probe_expect_missing:{channel}:{probe_id}")

    journeys = raw.get("journeys")
    if not isinstance(journeys, list) or not journeys:
        journeys = []
        problems.append("journeys_missing")

    ids: set[str] = set()
    covered_actions: set[str] = set()
    covered_roles: set[str] = set()
    evidence: set[str] = set()
    journey_channels: set[str] = set()
    for item in journeys:
        if not isinstance(item, dict):
            problems.append("journey_not_object")
            continue
        journey_id = str(item.get("id") or "").strip()
        if not journey_id:
            problems.append("journey_id_missing")
        elif journey_id in ids:
            problems.append(f"duplicate_journey:{journey_id}")
        ids.add(journey_id)
        covered_actions.update(str(x) for x in item.get("covers_native_actions") or ())
        covered_roles.update(str(x) for x in item.get("roles") or ())
        evidence.update(str(x) for x in item.get("evidence") or ())
        journey_channels.update(str(x) for x in item.get("channels") or ())

    expected_actions = required_native_actions()
    for action in sorted(expected_actions - covered_actions):
        problems.append(f"native_action_uncovered:{action}")
    for action in sorted(covered_actions - expected_actions):
        problems.append(f"unknown_native_action:{action}")
    for role in sorted(_REQUIRED_ROLES - covered_roles):
        problems.append(f"required_role_uncovered:{role}")
    for channel in sorted(_REQUIRED_CHANNELS - journey_channels):
        problems.append(f"required_channel_uncovered:{channel}")
    for item in sorted(_REQUIRED_EVIDENCE - evidence):
        problems.append(f"required_evidence_uncovered:{item}")

    if problems:
        raise LiveE2EContractError(";".join(problems))
    return {
        "journeys": len(journeys),
        "native_actions": len(expected_actions),
        "channels": sorted(_REQUIRED_CHANNELS),
        "roles": sorted(covered_roles),
        "live_probes": sum(len(live_probes.get(channel) or ()) for channel in _REQUIRED_CHANNELS),
    }


def main() -> int:
    summary = validate_manifest(load_manifest())
    print("CLIENTPLATFORM_LIVE_E2E_CONTRACT_OK " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
