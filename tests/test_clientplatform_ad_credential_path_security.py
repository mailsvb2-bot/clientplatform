from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clientplatform.infrastructure.ad_credential_vault import (
    AdCredentialVaultError,
    AgeAdCredentialVault,
)


class AdCredentialPathSecurityTests(unittest.TestCase):
    def test_symlinked_identity_is_rejected_before_age_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.chmod(root, 0o700)
            target = root / "real-identity.txt"
            target.write_text("AGE-SECRET-KEY-TEST", encoding="utf-8")
            os.chmod(target, 0o600)
            identity = root / "identity.txt"
            try:
                identity.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            with (
                mock.patch.dict(os.environ, {"APP_ENV": "production"}, clear=False),
                mock.patch("subprocess.run") as run,
                self.assertRaisesRegex(
                    AdCredentialVaultError,
                    "regular file",
                ),
            ):
                AgeAdCredentialVault(identity).seal("oauth-token")
            run.assert_not_called()

    def test_world_accessible_identity_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            identity = root / "identity.txt"
            identity.write_text("AGE-SECRET-KEY-TEST", encoding="utf-8")
            os.chmod(identity, 0o600)
            os.chmod(root, 0o755)

            with (
                mock.patch.dict(os.environ, {"APP_ENV": "production"}, clear=False),
                mock.patch("subprocess.run") as run,
                self.assertRaisesRegex(
                    AdCredentialVaultError,
                    "directory permissions must be 0700",
                ),
            ):
                AgeAdCredentialVault(identity).seal("oauth-token")
            run.assert_not_called()

    def test_identity_directory_in_place_of_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.chmod(root, 0o700)
            identity = root / "identity.txt"
            identity.mkdir(mode=0o700)

            with (
                mock.patch.dict(os.environ, {"APP_ENV": "production"}, clear=False),
                mock.patch("subprocess.run") as run,
                self.assertRaisesRegex(
                    AdCredentialVaultError,
                    "regular file",
                ),
            ):
                AgeAdCredentialVault(identity).seal("oauth-token")
            run.assert_not_called()

    def test_empty_identity_is_rejected_before_age_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.chmod(root, 0o700)
            identity = root / "identity.txt"
            identity.touch(mode=0o600)

            with (
                mock.patch.dict(os.environ, {"APP_ENV": "production"}, clear=False),
                mock.patch("subprocess.run") as run,
                self.assertRaisesRegex(
                    AdCredentialVaultError,
                    "must not be empty",
                ),
            ):
                AgeAdCredentialVault(identity).seal("oauth-token")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
