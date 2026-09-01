from __future__ import annotations

import json
import logging
from typing import Any

from clientplatform.application.control_callbacks import uuid_token
from clientplatform.domain.customer_interactions import CustomerInteractionMessage
from clientplatform.runtime.messenger_switch_links import StaffMessengerSwitchLinkService
from clientplatform.runtime.native_messenger_setup_links import NativeMessengerSetupLinkService
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from runtime.messenger_senders import MaxBotSender, MessengerTransportError, VkBotSender
from services.messenger.outbound import SenderRegistry
from services.messenger.text_ui import MessengerReply
from services.privacy_export_links import issue_privacy_export_url, privacy_export_ttl_minutes

log = logging.getLogger(__name__)


def _clientplatform_runtime_button_links(
    interaction: CustomerInteractionMessage,
    *,
    business_id: str,
) -> dict[str, str]:
    setup_links = NativeMessengerSetupLinkService(
        credential_provider=EnvironmentCredentialProvider(),
    )
    switch_links = StaffMessengerSwitchLinkService()
    resolved: dict[str, str] = {}
    for row in interaction.rows:
        for button in row:
            command = button.command
            if command.startswith("cpm:setup:"):
                url = setup_links.resolve_command_url(
                    command=command,
                    business_id=business_id,
                )
            elif command.startswith("cpm:switch:"):
                url = switch_links.resolve_command_url(
                    command=command,
                    business_id=business_id,
                )
            else:
                continue
            if url is None or not str(url).startswith("https://"):
                raise ValueError("ClientPlatform interaction link could not be resolved")
            resolved[command] = str(url)
    return resolved


def _scoped_clientplatform_command(command: str, *, business_id: str) -> str:
    raw = str(command or "").strip()
    if not business_id or not raw.startswith("cpm:"):
        return raw
    if raw.startswith(("cpm:setup:", "cpm:switch:")):
        return raw
    scoped = f"cpw:act:{uuid_token(business_id)}:{raw}"
    if len(scoped) > 180:
        raise ValueError("scoped ClientPlatform interaction command is too long")
    return scoped


def _vk_clientplatform_keyboard(
    interaction: CustomerInteractionMessage,
    *,
    button_links: dict[str, str],
    business_id: str,
) -> str:
    rows: list[list[dict[str, Any]]] = []
    for row in interaction.rows:
        rendered: list[dict[str, Any]] = []
        for button in row:
            link = button_links.get(button.command)
            if link is not None:
                rendered.append(
                    {
                        "action": {
                            "type": "open_link",
                            "link": link,
                            "label": button.label,
                        }
                    }
                )
            else:
                rendered.append(
                    {
                        "action": {
                            "type": "text",
                            "label": button.label,
                            "payload": json.dumps(
                                {
                                    "command": _scoped_clientplatform_command(
                                        button.command,
                                        business_id=business_id,
                                    )
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                        "color": "secondary",
                    }
                )
        rows.append(rendered)
    return json.dumps(
        {"one_time": True, "inline": True, "buttons": rows},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _max_clientplatform_attachments(
    interaction: CustomerInteractionMessage,
    *,
    button_links: dict[str, str],
    business_id: str,
) -> list[dict[str, Any]]:
    if not interaction.rows:
        return []
    rows: list[list[dict[str, Any]]] = []
    for row in interaction.rows:
        rendered: list[dict[str, Any]] = []
        for button in row:
            link = button_links.get(button.command)
            if link is not None:
                rendered.append({"type": "link", "text": button.label, "url": link})
            else:
                rendered.append(
                    {
                        "type": "callback",
                        "text": button.label,
                        "payload": _scoped_clientplatform_command(
                            button.command,
                            business_id=business_id,
                        ),
                    }
                )
        rows.append(rendered)
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


async def _send_clientplatform_interaction(
    *,
    platform: str,
    sender: Any,
    external_user_id: str,
    reply: MessengerReply,
) -> None:
    meta = dict(reply.meta or {})
    raw_interaction = str(meta.get("interaction") or "").strip()
    business_id = str(meta.get("business_id") or "").strip()
    if not raw_interaction:
        raise MessengerTransportError(
            "ClientPlatform interaction reply is missing canonical metadata"
        )
    interaction = CustomerInteractionMessage.from_json(raw_interaction)
    try:
        link_commands = {
            button.command
            for row in interaction.rows
            for button in row
            if button.command.startswith(("cpm:setup:", "cpm:switch:"))
        }
        if link_commands and not business_id:
            raise ValueError("ClientPlatform linked interaction is missing business scope")
        button_links = (
            _clientplatform_runtime_button_links(
                interaction,
                business_id=business_id,
            )
            if business_id
            else {}
        )
    except (RuntimeError, ValueError) as exc:
        log.error(
            "ClientPlatform interaction link materialization failed",
            extra={"business_id": business_id, "platform": platform},
        )
        raise MessengerTransportError(
            "ClientPlatform interaction link is expired or unavailable"
        ) from exc

    if platform == "vk":
        await sender.send_text(
            external_user_id,
            interaction.text,
            keyboard_json=_vk_clientplatform_keyboard(
                interaction,
                button_links=button_links,
                business_id=business_id,
            ),
        )
        return
    if platform == "max":
        await sender.send_text(
            external_user_id,
            interaction.text,
            attachments=_max_clientplatform_attachments(
                interaction,
                button_links=button_links,
                business_id=business_id,
            ),
        )
        return
    raise MessengerTransportError(
        f"Unsupported ClientPlatform interaction platform: {platform}"
    )


async def _send_privacy_export(
    *,
    platform: str,
    sender: Any,
    external_user_id: str,
    canonical_user_id: int,
) -> None:
    try:
        url = issue_privacy_export_url(canonical_user_id, platform=platform)
    except (RuntimeError, OSError, TypeError, ValueError):
        log.exception("%s privacy export link failed", platform.upper())
        url = ""
    if not url:
        await sender.send_text(
            external_user_id,
            "Не удалось подготовить безопасную ссылку на экспорт. Повторите позже.",
        )
        return
    ttl = privacy_export_ttl_minutes()
    await sender.send_text(
        external_user_id,
        (
            "Одноразовая ссылка на экспорт Ваших данных:\n"
            f"{url}\n\n"
            f"Ссылка действует не более {ttl} минут и позволяет скачать архив один раз. "
            "Предпросмотр мессенджера не расходует ссылку."
        ),
    )


async def send_reply_bundle(
    platform: str,
    external_user_id: str,
    canonical_user_id: int,
    replies: list[MessengerReply],
) -> None:
    registry = SenderRegistry(max=MaxBotSender(), vk=VkBotSender())
    sender = registry.get(platform)
    if sender is None:
        raise MessengerTransportError(f"No sender for {platform}")

    for reply in replies:
        if reply.kind == "clientplatform_interaction":
            await _send_clientplatform_interaction(
                platform=platform,
                sender=sender,
                external_user_id=external_user_id,
                reply=reply,
            )
            continue
        if reply.kind == "text":
            text = str(reply.text or "").strip()
            if text:
                await sender.send_text(external_user_id, text)
            continue
        if reply.kind == "privacy_export":
            await _send_privacy_export(
                platform=platform,
                sender=sender,
                external_user_id=external_user_id,
                canonical_user_id=canonical_user_id,
            )
            continue
        raise MessengerTransportError(
            f"Unsupported ClientPlatform reply kind: {reply.kind}"
        )


__all__ = ["send_reply_bundle"]
