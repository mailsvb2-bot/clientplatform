from __future__ import annotations

"""Validate optional MAX/VK channel activation without exposing credentials."""

import json
import os
import shutil
import ssl
from dataclasses import asdict, dataclass
from pathlib import Path

from clientplatform.infrastructure.managed_bot_credentials import (
    AgeManagedBotCredentialVault,
    ManagedBotCredentialError,
)
from clientplatform.runtime.secrets import (
    EnvironmentCredentialProvider,
    SecretReferenceError,
)
from runtime.ingress_flags import max_webhook_enabled, vk_webhook_enabled
from runtime.telegram_transport import telegram_runtime_enabled, telegram_transport
from services.messenger.setup import build_setup_status


@dataclass(frozen=True, slots=True)
class MessengerChannelPreflight:
    telegram_transport: str
    omnichannel_enabled: bool
    omnichannel_ready: bool
    max_enabled: bool
    max_ready: bool
    vk_enabled: bool
    vk_ready: bool
    webhook_runtime_ready: bool
    missing: tuple[str, ...]
    warnings: tuple[str, ...]
    telegram_runtime_enabled: bool = True

    @property
    def ok(self) -> bool:
        return not self.missing


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _deployed_env() -> bool:
    return str(os.getenv("APP_ENV") or "dev").strip().lower() in {
        "prod",
        "production",
        "stage",
        "staging",
    }


def _native_security_missing() -> tuple[str, ...]:
    """Return only sanitized prerequisite names, never secret material."""

    missing: list[str] = []
    signing_reference = str(
        os.getenv("CLIENTPLATFORM_MEDIA_SIGNING_SECRET_REFERENCE")
        or "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
    ).strip()
    try:
        signing_secret = EnvironmentCredentialProvider().resolve(signing_reference)
        if len(signing_secret.encode("utf-8")) < 32:
            raise SecretReferenceError("setup signing secret is too short")
    except (SecretReferenceError, ValueError):
        missing.append("CLIENTPLATFORM native setup signing secret")

    identity_raw = str(
        os.getenv("CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE") or ""
    ).strip()
    identity_path = Path(identity_raw) if identity_raw else None
    if identity_path is None or not identity_path.is_absolute():
        missing.append("CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE")
    else:
        try:
            AgeManagedBotCredentialVault(identity_path).validate_identity()
        except ManagedBotCredentialError:
            missing.append("CLIENTPLATFORM managed credential age identity")

    if shutil.which("age") is None or shutil.which("age-keygen") is None:
        missing.append("age and age-keygen executables")
    return tuple(missing)


def _max_tls_configuration() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate an explicit MAX CA extension without assuming MAX is active.

    MAX currently requires clients of platform-api2.max.ru to trust the Russian
    MinDigital CA. Some production images may already carry that CA in their
    system trust store, so absence of MAX_CA_BUNDLE is a warning rather than a
    hard failure. Once an operator configures an explicit bundle, however, the
    path and PEM contents must be usable before readiness may pass.
    """

    raw = str(os.getenv("MAX_CA_BUNDLE") or "").strip()
    if not raw:
        return (), (
            "MAX_CA_BUNDLE is not configured; verify the system trust store includes the MinDigital CA required by platform-api2.max.ru",
        )

    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        return ("MAX_CA_BUNDLE must be an absolute readable CA file",), ()
    try:
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=str(path))
    except (OSError, ssl.SSLError):
        return ("MAX_CA_BUNDLE must contain a valid CA certificate",), ()
    return (), ()


def inspect_messenger_channels() -> MessengerChannelPreflight:
    status = build_setup_status()
    telegram_enabled = telegram_runtime_enabled()
    omnichannel_enabled = _truthy_env("CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED")
    public_base = str(status.public_base_url or "").strip().rstrip("/")
    deployed_native = omnichannel_enabled and _deployed_env()
    native_security_missing = _native_security_missing() if deployed_native else ()
    max_tls_missing, max_tls_warnings = (
        _max_tls_configuration() if deployed_native else ((), ())
    )
    omnichannel_ready = bool(
        not omnichannel_enabled
        or (
            public_base
            and (not _deployed_env() or public_base.startswith("https://"))
            and not native_security_missing
            and not max_tls_missing
        )
    )
    # Canonical tenant-scoped VK/MAX uses encrypted per-business credentials.
    # Legacy MAX/VK flags remain fully validated by build_setup_status() when an
    # operator explicitly enables those old global ingress paths.
    missing = list(status.missing)
    if omnichannel_enabled and not public_base:
        missing.append("MESSENGER_PUBLIC_BASE_URL")
    elif omnichannel_enabled and _deployed_env() and not public_base.startswith("https://"):
        missing.append("MESSENGER_PUBLIC_BASE_URL must use https://")
    missing.extend(native_security_missing)
    missing.extend(max_tls_missing)
    warnings = tuple(dict.fromkeys((*status.warnings, *max_tls_warnings)))
    return MessengerChannelPreflight(
        telegram_runtime_enabled=telegram_enabled,
        telegram_transport=telegram_transport(),
        omnichannel_enabled=omnichannel_enabled,
        omnichannel_ready=omnichannel_ready,
        max_enabled=max_webhook_enabled(),
        max_ready=status.max_ok,
        vk_enabled=vk_webhook_enabled(),
        vk_ready=status.vk_ok,
        webhook_runtime_ready=bool(status.webhook_runtime_ok and omnichannel_ready),
        missing=tuple(dict.fromkeys(missing)),
        warnings=warnings,
    )


def main() -> int:
    result = inspect_messenger_channels()
    payload = asdict(result)
    payload["ok"] = result.ok
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if not result.ok:
        print(
            "CLIENTPLATFORM_MESSENGER_CHANNELS_PREFLIGHT_FAILED:"
            + ",".join(result.missing)
        )
        return 1
    print("CLIENTPLATFORM_MESSENGER_CHANNELS_PREFLIGHT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
