from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Any
from pathlib import Path

from services.messenger.platforms import normalize_platform, MessengerPlatform
from services.messenger.preferences import resolve_delivery_platform, get_channel_snapshot


class UnsupportedMessengerDelivery(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryPlan:
    platform: str
    external_user_id: str | None
    user_id: int = 0
    reason: str = ""


class TextSender(Protocol):
    async def send_text(self, external_user_id: str, text: str, **kwargs: Any) -> Any:
        ...

    async def send_audio_file(self, external_user_id: str, file_path: Path, *, caption: str | None = None, **kwargs: Any) -> Any:
        ...


@dataclass
class SenderRegistry:
    telegram: TextSender | None = None
    max: TextSender | None = None
    vk: TextSender | None = None

    def get(self, platform: str) -> TextSender | None:
        norm = normalize_platform(platform)
        if norm == MessengerPlatform.TELEGRAM.value:
            return self.telegram
        if norm == MessengerPlatform.MAX.value:
            return self.max
        if norm == MessengerPlatform.VK.value:
            return self.vk
        return None


def build_delivery_plan(user_id: int, *, fallback: str = MessengerPlatform.TELEGRAM.value, preferred_platform: str | None = None) -> DeliveryPlan:
    snapshot = get_channel_snapshot(int(user_id))
    canonical_user_id = int(snapshot['user_id'])
    platform = normalize_platform(preferred_platform) if preferred_platform else resolve_delivery_platform(canonical_user_id, fallback=fallback)
    for identity in snapshot['identities']:
        if normalize_platform(identity['platform']) == platform:
            external_user_id = (identity.get('external_user_id') or '').strip() or None
            if platform == MessengerPlatform.TELEGRAM.value and not external_user_id:
                external_user_id = str(canonical_user_id)
            return DeliveryPlan(user_id=canonical_user_id, platform=platform, external_user_id=external_user_id)
    if platform == MessengerPlatform.TELEGRAM.value and not snapshot['identities']:
        return DeliveryPlan(user_id=canonical_user_id, platform=platform, external_user_id=str(canonical_user_id))
    return DeliveryPlan(user_id=canonical_user_id, platform=platform, external_user_id=None)


async def send_text_to_user(
    user_id: int,
    text: str,
    *,
    senders: SenderRegistry,
    fallback: str = MessengerPlatform.TELEGRAM.value,
    **kwargs: Any,
) -> Any:
    plan = build_delivery_plan(int(user_id), fallback=fallback, preferred_platform=None)
    sender = senders.get(plan.platform)
    if sender is None:
        raise UnsupportedMessengerDelivery(f'No sender registered for platform={plan.platform}')
    if not plan.external_user_id:
        raise UnsupportedMessengerDelivery(f'No external user id for user_id={user_id}, platform={plan.platform}')
    return await sender.send_text(plan.external_user_id, text, **kwargs)
