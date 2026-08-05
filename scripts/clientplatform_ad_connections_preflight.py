from __future__ import annotations

"""Fail closed before enabling personal advertising account credentials."""

import os
import stat
from pathlib import Path

from clientplatform.application.ad_connections import ad_connections_enabled
from clientplatform.infrastructure.ad_credential_vault import (
    AdCredentialVaultError,
    AgeAdCredentialVault,
)


class AdConnectionsPreflightError(RuntimeError):
    """Sanitized configuration failure safe for startup logs."""


def _required(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value or value.lower().startswith("change"):
        raise AdConnectionsPreflightError(f"missing_{name.lower()}")
    return value


def _assert_private_identity(identity_path: Path) -> None:
    try:
        directory_stat = identity_path.parent.lstat()
        identity_stat = identity_path.lstat()
    except OSError as exc:
        raise AdConnectionsPreflightError("credential_identity_missing") from exc

    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(
        directory_stat.st_mode
    ):
        raise AdConnectionsPreflightError("credential_identity_directory_invalid")
    if directory_stat.st_mode & 0o777 != 0o700:
        raise AdConnectionsPreflightError(
            "credential_identity_directory_permissions_invalid"
        )
    if hasattr(os, "geteuid") and directory_stat.st_uid != os.geteuid():
        raise AdConnectionsPreflightError("credential_identity_directory_owner_invalid")

    if stat.S_ISLNK(identity_stat.st_mode) or not stat.S_ISREG(identity_stat.st_mode):
        raise AdConnectionsPreflightError("credential_identity_type_invalid")
    if identity_stat.st_size <= 0:
        raise AdConnectionsPreflightError("credential_identity_missing")
    if identity_stat.st_mode & 0o777 != 0o600:
        raise AdConnectionsPreflightError("credential_identity_permissions_invalid")
    if hasattr(os, "geteuid") and identity_stat.st_uid != os.geteuid():
        raise AdConnectionsPreflightError("credential_identity_owner_invalid")


def run() -> None:
    if not ad_connections_enabled():
        print("CLIENTPLATFORM_AD_CONNECTIONS_PREFLIGHT_DISABLED_OK")
        return

    domain = _required("CLIENTPLATFORM_DOMAIN")
    _required("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID")
    _required("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET")
    redirect_uri = _required("CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI")
    expected_redirect = f"https://{domain}/oauth/yandex-direct/callback"
    if redirect_uri != expected_redirect:
        raise AdConnectionsPreflightError("oauth_redirect_uri_mismatch")

    identity_path = Path(_required("CLIENTPLATFORM_AD_CREDENTIAL_IDENTITY_FILE"))
    if identity_path != Path("/run/secrets/clientplatform-ad/identity.txt"):
        raise AdConnectionsPreflightError("credential_identity_path_mismatch")
    _assert_private_identity(identity_path)

    vault = AgeAdCredentialVault(identity_path)
    probe = "clientplatform-ad-credential-preflight"
    try:
        ciphertext = vault.seal(probe)
        opened = vault.open(ciphertext)
    except AdCredentialVaultError as exc:
        raise AdConnectionsPreflightError("credential_round_trip_failed") from exc
    if opened != probe:
        raise AdConnectionsPreflightError("credential_round_trip_failed")

    print("CLIENTPLATFORM_AD_CONNECTIONS_PREFLIGHT_OK")


def main() -> int:
    try:
        run()
    except AdConnectionsPreflightError as exc:
        print(f"CLIENTPLATFORM_AD_CONNECTIONS_PREFLIGHT_FAILED:{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
