from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_clientplatform_capability_parity_manifest.py"
SPEC = importlib.util.spec_from_file_location("clientplatform_capability_parity_manifest_guard", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)


def _manifest():
    return copy.deepcopy(GUARD.load_manifest())


def _snapshot():
    return copy.deepcopy(GUARD.load_donor_snapshot())


def _capability(manifest, capability_id: str):
    for family in manifest["families"]:
        for capability in family["capabilities"]:
            if capability["id"] == capability_id:
                return capability
    raise AssertionError(f"capability not found: {capability_id}")


class CapabilityParityManifestTests(unittest.TestCase):
    def assert_contract_error(self, manifest, expected: str) -> None:
        with self.assertRaises(GUARD.CapabilityParityContractError) as raised:
            GUARD.validate_manifest(manifest)
        self.assertIn(expected, str(raised.exception))

    def test_repository_manifest_is_complete_and_keeps_missing_gaps_explicit(self) -> None:
        stats = GUARD.validate_manifest(_manifest())
        self.assertEqual(stats["families"], 17)
        self.assertEqual(stats["capabilities"], 20)
        self.assertEqual(stats["equivalent"], 2)
        self.assertEqual(stats["genericized"], 17)
        self.assertEqual(stats["missing"], 1)
        self.assertEqual(stats["domain_specific"], 0)


    def test_only_evidence_backed_account_consolidation_remains_missing(self) -> None:
        manifest = _manifest()
        missing = [
            capability["id"]
            for family in manifest["families"]
            for capability in family["capabilities"]
            if capability["status"] == "missing"
        ]
        self.assertEqual(missing, ["platform.account_consolidation"])

    def test_donor_evidence_must_exist_in_frozen_snapshot(self) -> None:
        manifest = _manifest()
        capability = _capability(manifest, "analytics.business_platform_observability")
        capability["donor_evidence"] = ["handlers/not-a-real-reviewed-donor-path.py"]
        self.assert_contract_error(manifest, "is not in the frozen donor snapshot")

    def test_donor_snapshot_digest_rejects_entry_tampering(self) -> None:
        snapshot = _snapshot()
        snapshot["entries"][0]["object_sha"] = "0" * 40
        with self.assertRaises(GUARD.CapabilityParityContractError) as raised:
            GUARD.validate_manifest(_manifest(), donor_snapshot=snapshot)
        self.assertIn("evidence digest does not match its entries", str(raised.exception))

    def test_donor_snapshot_hard_digest_rejects_simultaneous_rehash(self) -> None:
        snapshot = _snapshot()
        snapshot["entries"][0]["object_sha"] = "0" * 40
        canonical = "".join(
            f"{entry['path']}\t{entry['mode']}\t{entry['type']}\t{entry['object_sha']}\n"
            for entry in snapshot["entries"]
        ).encode("utf-8")
        snapshot["evidence_digest_sha256"] = GUARD.hashlib.sha256(canonical).hexdigest()
        with self.assertRaises(GUARD.CapabilityParityContractError) as raised:
            GUARD.validate_manifest(_manifest(), donor_snapshot=snapshot)
        self.assertIn("evidence set drifted from the reviewed baseline", str(raised.exception))

    def test_donor_baseline_sha_cannot_drift(self) -> None:
        manifest = _manifest()
        manifest["donor_baseline"]["sha"] = "0" * 40
        self.assert_contract_error(manifest, "must remain pinned")

    def test_missing_required_family_is_rejected(self) -> None:
        manifest = _manifest()
        manifest["families"] = manifest["families"][:-1]
        self.assert_contract_error(manifest, "families must contain exactly 17 entries")

    def test_unknown_status_is_rejected(self) -> None:
        manifest = _manifest()
        _capability(manifest, "analytics.business_platform_observability")["status"] = "documented"
        self.assert_contract_error(manifest, "unknown status")

    def test_hard_proven_capability_ratchet_cannot_be_removed_with_manifest_metadata(self) -> None:
        manifest = _manifest()
        target = "support.case_queue"
        manifest["required_proven_capabilities"].remove(target)
        for family in manifest["families"]:
            family["capabilities"] = [item for item in family["capabilities"] if item["id"] != target]
        self.assert_contract_error(manifest, "weakens the hard proven-capability ratchet")

    def test_newly_proven_capability_is_also_hard_ratcheted(self) -> None:
        manifest = _manifest()
        capability = _capability(manifest, "analytics.business_platform_observability")
        capability["status"] = "missing"
        capability.pop("clientplatform_owner", None)
        capability.pop("regression_evidence", None)
        capability.pop("rationale", None)
        capability["gap"] = {
            "decision": "required_slice",
            "priority": "high",
            "reason": "synthetic regression",
        }
        self.assert_contract_error(manifest, "proven capability cannot be downgraded")

    def test_proven_capability_cannot_be_downgraded_to_missing(self) -> None:
        manifest = _manifest()
        capability = _capability(manifest, "platform.directory_access_review")
        capability["status"] = "missing"
        capability.pop("clientplatform_owner", None)
        capability.pop("regression_evidence", None)
        capability.pop("rationale", None)
        capability["gap"] = {
            "decision": "required_slice",
            "priority": "high",
            "reason": "synthetic regression",
        }
        self.assert_contract_error(manifest, "proven capability cannot be downgraded")

    def test_equivalent_or_genericized_capability_requires_real_regression_test(self) -> None:
        manifest = _manifest()
        capability = _capability(manifest, "billing.subscription_payment_outcomes")
        capability["regression_evidence"] = ["docs/CLIENTPLATFORM_CANON_TZ.md"]
        self.assert_contract_error(manifest, "must live under tests/")

    def test_local_owner_path_traversal_is_rejected(self) -> None:
        manifest = _manifest()
        capability = _capability(manifest, "analytics.business_platform_observability")
        capability["clientplatform_owner"] = ["../outside.py"]
        self.assert_contract_error(manifest, "must not traverse outside")

    def test_donor_is_provenance_only_and_cannot_become_runtime_dependency(self) -> None:
        manifest = _manifest()
        manifest["donor_baseline"]["runtime_dependency"] = True
        self.assert_contract_error(manifest, "donor runtime dependency is forbidden")

    def test_messenger_family_requires_one_canonical_behavior_and_all_three_adapters(self) -> None:
        manifest = _manifest()
        family = next(item for item in manifest["families"] if item["id"] == "17_admin_navigation_messenger_parity")
        family["adapters"] = ["telegram", "vk"]
        self.assert_contract_error(manifest, "exactly telegram/vk/max adapters")

    def test_missing_gap_must_name_follow_on_decision_and_priority(self) -> None:
        manifest = _manifest()
        capability = _capability(manifest, "platform.account_consolidation")
        capability["gap"].pop("priority")
        self.assert_contract_error(manifest, "gap.priority must be non-empty text")


if __name__ == "__main__":
    unittest.main()
