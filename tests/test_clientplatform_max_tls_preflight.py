from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import clientplatform_messenger_channels_preflight as preflight


class MaxTlsPreflightTests(unittest.TestCase):
    def test_missing_explicit_bundle_is_warning_not_false_vk_blocker(self) -> None:
        with patch.dict(os.environ, {"MAX_CA_BUNDLE": ""}):
            missing, warnings = preflight._max_tls_configuration()

        self.assertEqual(missing, ())
        self.assertEqual(len(warnings), 1)
        self.assertIn("platform-api2.max.ru", warnings[0])

    def test_configured_bundle_must_be_absolute_and_readable(self) -> None:
        with patch.dict(os.environ, {"MAX_CA_BUNDLE": "relative-ca.pem"}):
            missing, warnings = preflight._max_tls_configuration()

        self.assertEqual(warnings, ())
        self.assertIn("absolute readable CA file", missing[0])

    def test_configured_bundle_extends_default_context_and_passes(self) -> None:
        with tempfile.NamedTemporaryFile() as bundle:
            context = MagicMock()
            with (
                patch.dict(os.environ, {"MAX_CA_BUNDLE": bundle.name}),
                patch.object(
                    preflight.ssl,
                    "create_default_context",
                    return_value=context,
                ) as create_context,
            ):
                missing, warnings = preflight._max_tls_configuration()

        self.assertEqual(missing, ())
        self.assertEqual(warnings, ())
        create_context.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(cafile=bundle.name)


if __name__ == "__main__":
    unittest.main()
