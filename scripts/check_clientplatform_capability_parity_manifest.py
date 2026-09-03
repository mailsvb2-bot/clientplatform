from __future__ import annotations

"""Fail-closed validator for the versioned issue-263 capability parity contract.

The pinned donor is provenance only. This guard never imports, executes, fetches,
or otherwise couples production runtime to donor code.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "config" / "capability_parity_manifest.json"
DONOR_SNAPSHOT_PATH = ROOT / "config" / "capability_parity_donor_snapshot.json"

PINNED_DONOR_SHA = "f63b44dd8963c1e6fd71ae8b05b9028d61f172ad"
PINNED_DONOR_ROOT_TREE_SHA = "94c7b3fd47b69e7d819a03e53c588390c102761b"
PINNED_DONOR_EVIDENCE_DIGEST_SHA256 = "c04fa0642d928aec1535e90d78be3ea969582537c7eb082423bf50149cf0412e"

EXPECTED_FAMILIES: dict[str, str] = {
    "01_platform_identity_access": "platform search / users / roles / permissions",
    "02_mailing_publications_cohorts": "mailing / broadcasts / publications / recipient cohorts",
    "03_operator_support_handoffs": "operator mode / operator queue / handoffs / support",
    "04_reports_analytics_observability": "reports / analytics / business and platform observability",
    "05_settings_system_runtime": "settings / system / runtime diagnostics",
    "06_security_audit_access_review": "security / audit / access review",
    "07_release_deploy_rollback_forensics": "release / deploy readiness / rollback / runtime forensics",
    "08_subscriptions_tariffs_payments_refunds": "subscriptions / tariffs / payments / payment problems / refunds",
    "09_shop_offerings_programs_delivery": "shop / offerings / programs / delivery",
    "10_growth_ai_autopilot_nba": "Growth AI / Growth Autopilot / Next Best Action / autonomous growth",
    "11_paid_ads_attribution_budget": "paid ads / attribution / budget / orchestration",
    "12_visual_assets_vendor_review_jobs": "visual generation / assets / vendor / review / media library / jobs",
    "13_speech_voice_metrics_experiments": "speech / voice providers / metrics / experiments / ops",
    "14_author_content_workspace": "author/content production / assets / export / preview / tasks / workspace",
    "15_incidents_backup_disaster_recovery": "incidents / backup / disaster recovery / recovery evidence",
    "16_referrals_gifts_retention_lifecycle": "referrals / gifts / retention / lifecycle / cohorts",
    "17_admin_navigation_messenger_parity": "admin navigation, Back/Home, accessibility and mobile limits across Telegram/VK/MAX",
}

ALLOWED_STATUSES = frozenset({"equivalent", "genericized", "missing", "domain-specific"})
PROVEN_STATUSES = frozenset({"equivalent", "genericized"})
ALLOWED_LEVELS = frozenset({"business_owner", "platform_operator", "shared"})
ALLOWED_GAP_DECISIONS = frozenset({"required_slice", "owner_exception"})
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Hard ratchet: these capabilities are proven by shipped slices or the reviewed
# M6-006 parity evidence. Every equivalent/genericized row must remain in this set.
PROVEN_CAPABILITY_RATCHET = frozenset(
    {
        'platform.directory_access_review',
        'platform.roles_permissions',
        'messaging.broadcast_publication_cohorts',
        'support.audited_access_session',
        'support.case_queue',
        'analytics.business_platform_observability',
        'runtime.settings_diagnostics',
        'security.audit_access_review',
        'release.deploy_rollback_forensics',
        'billing.subscription_payment_outcomes',
        'commerce.offerings_program_delivery',
        'growth.goal_first_autopilot_nba',
        'ads.connection_spend_attribution',
        'visual.gateway_assets_review_jobs',
        'voice.media_delivery_metrics',
        'content.production_workspace',
        'recovery.encrypted_backup_dr_evidence',
        'lifecycle.referral_retention_cohorts',
        'messenger.canonical_navigation_parity',
    }
)


class CapabilityParityContractError(ValueError):
    """Raised when the parity manifest weakens or violates its contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CapabilityParityContractError(message)


def _require_text(value: Any, label: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()), f"{label} must be non-empty text")
    return value.strip()


def _require_string_list(value: Any, label: str) -> list[str]:
    _require(isinstance(value, list) and bool(value), f"{label} must be a non-empty list")
    result = [_require_text(item, f"{label}[]") for item in value]
    _require(len(result) == len(set(result)), f"{label} contains duplicates")
    return result


def _safe_relative_path(value: str, label: str) -> Path:
    raw = _require_text(value, label)
    candidate = Path(raw)
    _require(not candidate.is_absolute(), f"{label} must be repository-relative: {raw}")
    _require(".." not in candidate.parts, f"{label} must not traverse outside its repository: {raw}")
    _require(raw not in {".", ""}, f"{label} must identify a concrete path")
    return candidate


def _require_local_path(value: str, label: str, *, regression_test: bool = False) -> None:
    relative = _safe_relative_path(value, label)
    resolved = (ROOT / relative).resolve()
    _require(resolved == ROOT or ROOT in resolved.parents, f"{label} escapes repository root: {value}")
    _require(resolved.exists(), f"{label} does not exist: {value}")
    if regression_test:
        _require(relative.parts and relative.parts[0] == "tests", f"{label} must live under tests/: {value}")
        _require(resolved.is_file(), f"{label} must be a test file: {value}")
        _require(relative.name.startswith("test_") and relative.suffix == ".py", f"{label} must be a Python regression test: {value}")


def _validate_donor_snapshot(snapshot: dict[str, Any], *, donor_sha: str) -> set[str]:
    _require(isinstance(snapshot, dict), "donor snapshot root must be an object")
    _require(snapshot.get("schema_version") == 1, "donor snapshot schema_version must be 1")
    _require(snapshot.get("donor_sha") == donor_sha, "donor snapshot SHA must match manifest donor baseline")
    _require(snapshot.get("donor_sha") == PINNED_DONOR_SHA, "donor snapshot SHA must remain pinned")
    _require(
        snapshot.get("root_tree_sha") == PINNED_DONOR_ROOT_TREE_SHA,
        "donor snapshot root tree SHA drifted from the reviewed baseline",
    )

    declared_digest = _require_text(
        snapshot.get("evidence_digest_sha256"),
        "donor_snapshot.evidence_digest_sha256",
    )
    _require(bool(SHA256_RE.fullmatch(declared_digest)), "donor snapshot digest must be lowercase SHA-256")

    entries = snapshot.get("entries")
    _require(isinstance(entries, list) and bool(entries), "donor snapshot entries must be non-empty")
    seen_paths: set[str] = set()
    canonical_lines: list[str] = []
    for index, entry in enumerate(entries):
        _require(isinstance(entry, dict), f"donor snapshot entry[{index}] must be an object")
        raw_path = _require_text(entry.get("path"), f"donor_snapshot.entries[{index}].path")
        _safe_relative_path(raw_path, f"donor_snapshot.entries[{index}].path")
        _require("\\" not in raw_path, f"donor snapshot path must use POSIX separators: {raw_path}")
        _require(raw_path not in seen_paths, f"duplicate donor snapshot path: {raw_path}")
        seen_paths.add(raw_path)

        mode = _require_text(entry.get("mode"), f"donor_snapshot.entries[{index}].mode")
        _require(mode in {"100644", "100755"}, f"unsupported donor evidence mode for {raw_path}: {mode}")
        _require(entry.get("type") == "blob", f"donor evidence must be a frozen blob: {raw_path}")
        object_sha = _require_text(entry.get("object_sha"), f"donor_snapshot.entries[{index}].object_sha")
        _require(bool(SHA_RE.fullmatch(object_sha)), f"invalid donor object SHA for {raw_path}")
        canonical_lines.append(f"{raw_path}\t{mode}\tblob\t{object_sha}\n")

    _require(
        list(seen_paths) != [] and [entry["path"] for entry in entries] == sorted(seen_paths),
        "donor snapshot entries must be sorted by path",
    )
    computed_digest = hashlib.sha256("".join(canonical_lines).encode("utf-8")).hexdigest()
    _require(computed_digest == declared_digest, "donor snapshot evidence digest does not match its entries")
    _require(
        computed_digest == PINNED_DONOR_EVIDENCE_DIGEST_SHA256,
        "donor snapshot evidence set drifted from the reviewed baseline",
    )
    return seen_paths


def _validate_baselines(manifest: dict[str, Any]) -> None:
    _require(manifest.get("schema_version") == 1, "schema_version must be 1")
    _require(manifest.get("contract") == "issue-263-capability-parity", "unexpected capability parity contract id")

    client_sha = _require_text(manifest.get("clientplatform_baseline_sha"), "clientplatform_baseline_sha")
    _require(bool(SHA_RE.fullmatch(client_sha)), "clientplatform_baseline_sha must be an exact lowercase 40-hex SHA")

    donor = manifest.get("donor_baseline")
    _require(isinstance(donor, dict), "donor_baseline must be an object")
    _require(donor.get("id") == "issue-263-pinned-donor", "donor_baseline.id must use neutral pinned provenance")
    donor_sha = _require_text(donor.get("sha"), "donor_baseline.sha")
    _require(bool(SHA_RE.fullmatch(donor_sha)), "donor_baseline.sha must be an exact lowercase 40-hex SHA")
    _require(donor_sha == PINNED_DONOR_SHA, "donor_baseline.sha must remain pinned to the reviewed issue-263 baseline")
    _require(donor_sha != client_sha, "donor and ClientPlatform baselines must be distinct")
    _require(donor.get("runtime_dependency") is False, "donor runtime dependency is forbidden")

    declared_statuses = manifest.get("allowed_statuses")
    _require(isinstance(declared_statuses, list), "allowed_statuses must be a list")
    _require(set(declared_statuses) == ALLOWED_STATUSES and len(declared_statuses) == len(ALLOWED_STATUSES), "allowed_statuses must contain exactly the four canonical statuses")


def _validate_capability(
    capability: dict[str, Any],
    *,
    family_id: str,
    seen_capability_ids: set[str],
    donor_snapshot_paths: set[str],
    seen_donor_evidence_paths: set[str],
) -> tuple[str, str]:
    capability_id = _require_text(capability.get("id"), f"{family_id}.capability.id")
    _require(capability_id not in seen_capability_ids, f"duplicate capability id: {capability_id}")
    seen_capability_ids.add(capability_id)

    _require_text(capability.get("name"), f"{capability_id}.name")
    status = _require_text(capability.get("status"), f"{capability_id}.status")
    _require(status in ALLOWED_STATUSES, f"{capability_id} has unknown status: {status}")
    level = _require_text(capability.get("level"), f"{capability_id}.level")
    _require(level in ALLOWED_LEVELS, f"{capability_id} has unknown level: {level}")

    donor_evidence = _require_string_list(capability.get("donor_evidence"), f"{capability_id}.donor_evidence")
    for index, path in enumerate(donor_evidence):
        _safe_relative_path(path, f"{capability_id}.donor_evidence[{index}]")
        _require(path in donor_snapshot_paths, f"{capability_id}.donor_evidence[{index}] is not in the frozen donor snapshot: {path}")
        seen_donor_evidence_paths.add(path)

    if status in PROVEN_STATUSES:
        owners = _require_string_list(capability.get("clientplatform_owner"), f"{capability_id}.clientplatform_owner")
        tests = _require_string_list(capability.get("regression_evidence"), f"{capability_id}.regression_evidence")
        for index, path in enumerate(owners):
            _require_local_path(path, f"{capability_id}.clientplatform_owner[{index}]")
        for index, path in enumerate(tests):
            _require_local_path(path, f"{capability_id}.regression_evidence[{index}]", regression_test=True)
        _require_text(capability.get("rationale"), f"{capability_id}.rationale")
    elif status == "missing":
        gap = capability.get("gap")
        _require(isinstance(gap, dict), f"{capability_id}.gap must be an object")
        decision = _require_text(gap.get("decision"), f"{capability_id}.gap.decision")
        _require(decision in ALLOWED_GAP_DECISIONS, f"{capability_id} has unknown gap decision: {decision}")
        _require_text(gap.get("reason"), f"{capability_id}.gap.reason")
        if decision == "required_slice":
            _require_text(gap.get("priority"), f"{capability_id}.gap.priority")
        else:
            _require_text(gap.get("approved_by"), f"{capability_id}.gap.approved_by")
            _require_text(gap.get("approval_reference"), f"{capability_id}.gap.approval_reference")
    else:
        _require_text(capability.get("generic_mechanism"), f"{capability_id}.generic_mechanism")
        _require_text(capability.get("disposition"), f"{capability_id}.disposition")

    return capability_id, status


def validate_manifest(
    manifest: dict[str, Any],
    *,
    donor_snapshot: dict[str, Any] | None = None,
) -> dict[str, int]:
    _require(isinstance(manifest, dict), "manifest root must be an object")
    _validate_baselines(manifest)
    donor_sha = str(manifest["donor_baseline"]["sha"])
    snapshot = load_donor_snapshot() if donor_snapshot is None else donor_snapshot
    donor_snapshot_paths = _validate_donor_snapshot(snapshot, donor_sha=donor_sha)

    families = manifest.get("families")
    _require(isinstance(families, list), "families must be a list")
    _require(len(families) == len(EXPECTED_FAMILIES), f"families must contain exactly {len(EXPECTED_FAMILIES)} entries")

    seen_family_ids: set[str] = set()
    seen_capability_ids: set[str] = set()
    seen_donor_evidence_paths: set[str] = set()
    status_counts = {status: 0 for status in ALLOWED_STATUSES}

    for family in families:
        _require(isinstance(family, dict), "every family must be an object")
        family_id = _require_text(family.get("id"), "family.id")
        _require(family_id not in seen_family_ids, f"duplicate family id: {family_id}")
        seen_family_ids.add(family_id)
        _require(family_id in EXPECTED_FAMILIES, f"unknown family id: {family_id}")
        _require(family.get("title") == EXPECTED_FAMILIES[family_id], f"family title drift for {family_id}")

        capabilities = family.get("capabilities")
        _require(isinstance(capabilities, list) and bool(capabilities), f"{family_id}.capabilities must be non-empty")
        for capability in capabilities:
            _require(isinstance(capability, dict), f"{family_id} capability must be an object")
            _, status = _validate_capability(
                capability,
                family_id=family_id,
                seen_capability_ids=seen_capability_ids,
                donor_snapshot_paths=donor_snapshot_paths,
                seen_donor_evidence_paths=seen_donor_evidence_paths,
            )
            status_counts[status] += 1

        if family_id == "17_admin_navigation_messenger_parity":
            _require(family.get("canonical_messenger_behavior") is True, "family 17 must assert one canonical messenger behavior")
            adapters = family.get("adapters")
            _require(isinstance(adapters, list), "family 17 adapters must be a list")
            _require(set(adapters) == {"telegram", "vk", "max"} and len(adapters) == 3, "family 17 must cover exactly telegram/vk/max adapters")

    _require(seen_family_ids == set(EXPECTED_FAMILIES), "one or more required capability families are missing")

    declared_ratchet = set(_require_string_list(manifest.get("required_proven_capabilities"), "required_proven_capabilities"))
    _require(
        declared_ratchet == PROVEN_CAPABILITY_RATCHET,
        "required_proven_capabilities must match the hard proven-capability ratchet",
    )
    _require(declared_ratchet <= seen_capability_ids, "required_proven_capabilities references a missing capability")

    status_by_id: dict[str, str] = {}
    for family in families:
        for capability in family["capabilities"]:
            status_by_id[capability["id"]] = capability["status"]
    for capability_id in PROVEN_CAPABILITY_RATCHET:
        _require(status_by_id.get(capability_id) in PROVEN_STATUSES, f"proven capability cannot be downgraded: {capability_id}")

    proven_in_manifest = {
        capability_id
        for capability_id, status in status_by_id.items()
        if status in PROVEN_STATUSES
    }
    _require(
        proven_in_manifest == PROVEN_CAPABILITY_RATCHET,
        "every equivalent/genericized capability must be hard-ratcheted",
    )

    _require(
        seen_donor_evidence_paths == donor_snapshot_paths,
        "frozen donor snapshot must contain exactly the evidence paths referenced by the manifest",
    )

    return {
        "families": len(families),
        "capabilities": len(seen_capability_ids),
        "equivalent": status_counts["equivalent"],
        "genericized": status_counts["genericized"],
        "missing": status_counts["missing"],
        "domain_specific": status_counts["domain-specific"],
    }


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityParityContractError(f"cannot load {label}: {exc}") from exc
    _require(isinstance(payload, dict), f"{label} root must be an object")
    return payload


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    return _load_json_object(path, "capability parity manifest")


def load_donor_snapshot(path: Path = DONOR_SNAPSHOT_PATH) -> dict[str, Any]:
    return _load_json_object(path, "capability parity donor snapshot")


def main() -> int:
    try:
        stats = validate_manifest(load_manifest())
    except CapabilityParityContractError as exc:
        print(f"CLIENTPLATFORM_CAPABILITY_PARITY_GUARD_FAIL:{exc}")
        return 1
    print(
        "CLIENTPLATFORM_CAPABILITY_PARITY_GUARD_OK:"
        f"families={stats['families']} "
        f"capabilities={stats['capabilities']} "
        f"equivalent={stats['equivalent']} "
        f"genericized={stats['genericized']} "
        f"missing={stats['missing']} "
        f"domain_specific={stats['domain_specific']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
