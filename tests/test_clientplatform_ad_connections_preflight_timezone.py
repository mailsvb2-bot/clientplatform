from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from scripts import clientplatform_ad_connections_preflight as preflight


_BASE_ENV = {
    "CLIENTPLATFORM_AD_CONNECTIONS_ENABLED": "1",
    "CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID": "client-id",
    "CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET": "client-secret",
    "CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI": "https://oauth.yandex.ru/verification_code",
    "CLIENTPLATFORM_AD_CREDENTIAL_IDENTITY_FILE": "/run/secrets/clientplatform-ad/identity.txt",
}


class AdConnectionsReportTimezonePreflightTests(unittest.TestCase):
    def _assert_timezone_rejected(self, value: str) -> None:
        env = {
            **_BASE_ENV,
            "CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE": value,
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(preflight, "_assert_private_identity") as identity_probe,
            self.assertRaisesRegex(
                preflight.AdConnectionsPreflightError,
                "^report_timezone_invalid$",
            ),
        ):
            preflight.run()
        identity_probe.assert_not_called()

    def test_unknown_report_timezone_fails_before_credential_probe(self) -> None:
        self._assert_timezone_rejected("Mars/Olympus")

    def test_pathlike_report_timezone_fails_before_credential_probe(self) -> None:
        self._assert_timezone_rejected("../Etc/UTC")

    def test_valid_report_timezone_reaches_credential_probe(self) -> None:
        env = {
            **_BASE_ENV,
            "CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE": "Europe/Amsterdam",
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(
                preflight,
                "_assert_private_identity",
                side_effect=preflight.AdConnectionsPreflightError(
                    "credential_probe_reached"
                ),
            ) as identity_probe,
            self.assertRaisesRegex(
                preflight.AdConnectionsPreflightError,
                "^credential_probe_reached$",
            ),
        ):
            preflight.run()
        identity_probe.assert_called_once_with(
            Path("/run/secrets/clientplatform-ad/identity.txt")
        )

    def test_default_report_timezone_reaches_credential_probe(self) -> None:
        with (
            mock.patch.dict(os.environ, _BASE_ENV, clear=True),
            mock.patch.object(
                preflight,
                "_assert_private_identity",
                side_effect=preflight.AdConnectionsPreflightError(
                    "credential_probe_reached"
                ),
            ) as identity_probe,
            self.assertRaisesRegex(
                preflight.AdConnectionsPreflightError,
                "^credential_probe_reached$",
            ),
        ):
            preflight.run()
        identity_probe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
