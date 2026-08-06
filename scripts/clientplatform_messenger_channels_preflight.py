from __future__ import annotations

"""Validate optional MAX/VK channel activation without exposing credentials."""

import json
from dataclasses import asdict, dataclass

from runtime.ingress_flags import max_webhook_enabled, vk_webhook_enabled
from runtime.telegram_transport import telegram_transport
from services.messenger.setup import build_setup_status


@dataclass(frozen=True, slots=True)
class MessengerChannelPreflight:
    telegram_transport: str
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


def inspect_messenger_channels() -> MessengerChannelPreflight:
    status = build_setup_status()
    return MessengerChannelPreflight(
        telegram_transport=telegram_transport(),
        max_enabled=max_webhook_enabled(),
        max_ready=status.max_ok,
        vk_enabled=vk_webhook_enabled(),
        vk_ready=status.vk_ok,
        webhook_runtime_ready=status.webhook_runtime_ok,
        missing=status.missing,
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
