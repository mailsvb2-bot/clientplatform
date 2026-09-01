from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.validators import privacy as privacy_validator


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class ClientPlatformPrivacyValidatorBoundaryTests(unittest.TestCase):
    def test_fresh_clientplatform_validates_global_and_tenant_manifests(self) -> None:
        calls: list[str] = []
        with (
            patch.object(privacy_validator, "get_connection", return_value=_Conn()),
            patch.object(privacy_validator, "_clientplatform_schema_present", return_value=True),
            patch.object(
                privacy_validator,
                "validate_privacy_manifest",
                side_effect=lambda _conn, *, strict: calls.append("global")
                or SimpleNamespace(discovered_user_tables=("users",)),
            ),
            patch.object(
                privacy_validator,
                "validate_clientplatform_privacy_manifest",
                side_effect=lambda _conn, *, strict, require_complete: calls.append("tenant")
                or SimpleNamespace(discovered_business_tables=("businesses",)),
            ),
        ):
            privacy_validator.validate_privacy_schema(strict=True)
        self.assertEqual(calls, ["global", "tenant"])

    def test_global_manifest_remains_fail_closed_without_tenant_schema(self) -> None:
        def fail(_conn, *, strict: bool):
            self.assertTrue(strict)
            raise RuntimeError("global_privacy_invalid")

        with (
            patch.object(privacy_validator, "get_connection", return_value=_Conn()),
            patch.object(privacy_validator, "_clientplatform_schema_present", return_value=False),
            patch.object(privacy_validator, "validate_privacy_manifest", side_effect=fail),
        ):
            with self.assertRaisesRegex(privacy_validator.ValidationError, "global_privacy_invalid"):
                privacy_validator.validate_privacy_schema(strict=True)


if __name__ == "__main__":
    unittest.main()
