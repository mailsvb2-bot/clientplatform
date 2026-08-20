from __future__ import annotations

"""Validate optional MAX/VK channel activation without exposing credentials."""

import json
import os
from dataclasses import asdict, dataclass

from runtime.ingress_flags import max_webhook_enabled, vk_webhook_enabled
from runtime.telegram_transport import telegram_transport
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

    @property
    def ok(self) -> bool:
        return not self.missing


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _deployed_env() -> bool:
    return str(os.getenv("APP_ENV") or "dev").strip().lower() in {
        "prod", "production", "stage", "staging"
    }


def inspect_messenger_channels() -> MessengerChannelPreflight:
    status = build_setup_status()
    omnichannel_enabled = _truthy_env("CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED")
    public_base = str(status.public_base_url or "").strip().rstrip("/")
    omnichannel_ready = bool(
        not omnichannel_enabled
        or (public_base and (not _deployed_env() or public_base.startswith("https://")))
    )
    missing = list(status.missing)
    if omnichannel_enabled and not public_base:
        missing.append("MESSENGER_PUBLIC_BASE_URL")
    elif omnichannel_enabled and _deployed_env() and not public_base.startswith("https://"):
        missing.append("MESSENGER_PUBLIC_BASE_URL must use https://")
    return MessengerChannelPreflight(
        telegram_transport=telegram_transport(),
        omnichannel_enabled=omnichannel_enabled,
        omnichannel_ready=omnichannel_ready,
        max_enabled=max_webhook_enabled(),
        max_ready=status.max_ok,
        vk_enabled=vk_webhook_enabled(),
        vk_ready=status.vk_ok,
        webhook_runtime_ready=bool(status.webhook_runtime_ok and omnichannel_ready),
        missing=tuple(dict.fromkeys(missing)),
        warnings=status.warnings,
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
