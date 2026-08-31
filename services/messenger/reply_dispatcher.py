from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from clientplatform.application.control_callbacks import uuid_token
from clientplatform.domain.customer_interactions import CustomerInteractionMessage
from clientplatform.runtime.messenger_switch_links import StaffMessengerSwitchLinkService
from clientplatform.runtime.native_messenger_setup_links import NativeMessengerSetupLinkService
from clientplatform.runtime.secrets import EnvironmentCredentialProvider

from runtime.messenger_senders import MaxBotSender, VkBotSender, MessengerTransportError
from runtime import messenger_max_ui as max_ui
from runtime.ingress_flags import payment_http_enabled
from runtime.messenger_vk_ui import (
    vk_demo_kind_keyboard_json,
    vk_score_scale_keyboard_json,
    vk_weather_city_keyboard_json,
    vk_weather_keyboard_json,
    with_vk_keyboard,
    keyboard_for_reply_kind,
)
from services.events import log_event
from services.messenger.audio_delivery import send_next_audio_to_user, send_replay_audio_to_user
from services.messenger.audio_progress import confirm_pending_audio_delivery
from services.messenger.outbound import SenderRegistry, UnsupportedMessengerDelivery
from services.messenger.package_payment_ui import package_payment_text
from services.messenger.progress_charts import build_vk_mood_progress_chart_path
from services.messenger.text_ui import MessengerReply, PAYMENT_UNAVAILABLE_TEXT
from services.mood_text_flow import complete_pre_score_and_send, complete_post_score_and_send_next
from services.privacy_export_links import issue_privacy_export_url, privacy_export_ttl_minutes
from services.weather import get_weather_text_async, set_city

log = logging.getLogger(__name__)


SCORE_SCALE_MARKERS = (
    "шкала оценки",
    "шкала состояния",
    "оцените состояние",
    "оцените своё состояние",
    "оцените свое состояние",
    "состояние после прослушивания",
    "состояние до практики",
)


def _vk_kwargs(platform: str, kwargs: dict[str, Any], canonical_user_id: int, text: str = "") -> dict[str, Any]:
    enriched = dict(kwargs)
    if text:
        enriched.setdefault("_text_for_keyboard", text)
    return with_vk_keyboard(platform, enriched, user_id=canonical_user_id)


def _looks_like_score_scale(text: str) -> bool:
    raw = str(text or "").casefold().replace("−", "-").replace("ё", "е")
    if "-10" not in raw or "10" not in raw:
        return False
    return any(marker in raw for marker in SCORE_SCALE_MARKERS)


def _is_checkout_surface_text(text: str) -> bool:
    stripped = str(text or "").lstrip()
    if stripped.startswith("💳"):
        return True
    if not stripped.startswith("🎁"):
        return False
    return (
        stripped.startswith("🎁 Подарить Метротерапию")
        or stripped.startswith("🎁 Кому подарить Метротерапию?")
        or "/pay/yookassa" in stripped
    )


def _canonical_payment_text(platform: str, canonical_user_id: int, external_user_id: str, text: str) -> str:
    """Apply the current payment policy when a text reply is actually delivered.

    Durable outbox rows can outlive a deploy. A checkout reply created while
    payment ingress was enabled must therefore degrade safely if the current
    runtime has PAYMENT_HTTP_ENABLED=0.
    """
    stripped = str(text or "").lstrip()
    if not payment_http_enabled() and _is_checkout_surface_text(stripped):
        return PAYMENT_UNAVAILABLE_TEXT
    if stripped.startswith("💳"):
        return package_payment_text(user_id=canonical_user_id, platform=platform, external_user_id=external_user_id)
    if stripped.startswith("🎁"):
        # Gift text is stateful: command -> recipient prompt -> recipient-bound checkout links.
        # Rebuilding it here would lose recipient_hint and turn a ready checkout back into the prompt.
        return text
    return text


async def _send_progress_chart_file(
    *,
    platform: str,
    sender: Any,
    external_user_id: str,
    chart_path: Path,
    caption: str,
    canonical_user_id: int,
) -> None:
    if platform in {"vk", "max"} and hasattr(sender, "send_image_file"):
        await sender.send_image_file(
            external_user_id,
            chart_path,
            caption=caption,
            **_vk_kwargs(platform, {}, canonical_user_id, text=caption),
        )
        return

    if hasattr(sender, "send_document_file"):
        await sender.send_document_file(
            external_user_id,
            chart_path,
            caption=caption,
            **_vk_kwargs(platform, {}, canonical_user_id),
        )
        return

    raise UnsupportedMessengerDelivery(
        f"No document/image sender for progress chart on platform={platform}"
    )


async def _send_progress_chart_or_notice(
    *,
    platform: str,
    sender: Any,
    external_user_id: str,
    canonical_user_id: int,
) -> None:
    chart_path = build_vk_mood_progress_chart_path(canonical_user_id)
    if chart_path is None:
        await sender.send_text(
            external_user_id,
            "📈 Пока недостаточно данных для графика. Пройдите цикл: шкала ДО → аудио → Прослушал → шкала ПОСЛЕ.",
            **_vk_kwargs(platform, {}, canonical_user_id),
        )
        return
    try:
        await _send_progress_chart_file(
            platform=platform,
            sender=sender,
            external_user_id=external_user_id,
            chart_path=chart_path,
            caption="📈 Ваш график изменения состояния",
            canonical_user_id=canonical_user_id,
        )
        log.info("%s progress chart sent: user_id=%s path=%s", platform.upper(), canonical_user_id, chart_path)
    except (MessengerTransportError, UnsupportedMessengerDelivery, OSError):
        log.exception("%s progress chart send failed", platform.upper())
        await sender.send_text(
            external_user_id,
            "⚠️ График построен, но не удалось отправить его в этот мессенджер.",
            **_vk_kwargs(platform, {}, canonical_user_id),
        )


async def _send_mood_flow_result_notice(
    *,
    platform: str,
    sender: Any,
    external_user_id: str,
    canonical_user_id: int,
    message: str,
    prompt_done: bool = False,
) -> None:
    if not str(message or "").strip():
        return
    kwargs: dict[str, Any] = {}
    if prompt_done:
        if platform == "vk":
            keyboard_json = keyboard_for_reply_kind("post_audio") or keyboard_for_reply_kind("state_period")
            if keyboard_json is not None:
                kwargs["keyboard_json"] = keyboard_json
        elif platform == "max":
            attachment = max_ui.post_audio_attachment()
            if attachment is not None:
                kwargs["attachments"] = [attachment]
    await sender.send_text(
        external_user_id,
        message,
        **_vk_kwargs(platform, kwargs, canonical_user_id, text=message),
    )


async def _handle_pre_score_flow(
    *,
    platform: str,
    sender: Any,
    registry: SenderRegistry,
    external_user_id: str,
    canonical_user_id: int,
    score: int,
) -> None:
    try:
        result = await complete_pre_score_and_send(
            canonical_user_id,
            platform=platform,
            score=int(score),
            senders=registry,
        )
    except (MessengerTransportError, UnsupportedMessengerDelivery, OSError):
        log.exception("%s pre-score audio delivery failed", platform.upper())
        await sender.send_text(
            external_user_id,
            "⚠️ Оценку сохранил, но не смог отправить аудио. Напишите: continue",
            **_vk_kwargs(platform, {}, canonical_user_id),
        )
        return

    if not result.ok:
        await sender.send_text(external_user_id, result.message, **_vk_kwargs(platform, {}, canonical_user_id))
        return

    # Audio delivery is complete at this point. The post-audio notice/keyboard is
    # a separate UX surface and must never turn a successful native audio send
    # into native_audio_failed or a VK link fallback.
    if platform in {"vk", "max"}:
        try:
            await _send_mood_flow_result_notice(
                platform=platform,
                sender=sender,
                external_user_id=external_user_id,
                canonical_user_id=canonical_user_id,
                message=result.message,
                prompt_done=result.prompt_done,
            )
        except (MessengerTransportError, UnsupportedMessengerDelivery, RuntimeError, ValueError, TypeError, OSError):
            log.exception("%s post-audio notice failed after successful audio delivery", platform.upper())


async def _handle_post_score_flow(
    *,
    platform: str,
    sender: Any,
    registry: SenderRegistry,
    external_user_id: str,
    canonical_user_id: int,
    score: int,
) -> None:
    try:
        result = await complete_post_score_and_send_next(
            canonical_user_id,
            platform=platform,
            score=int(score),
            senders=registry,
        )
        kwargs: dict[str, Any] = {}
        if platform == "vk":
            keyboard_json = keyboard_for_reply_kind("state_period")
            if keyboard_json is not None:
                kwargs["keyboard_json"] = keyboard_json
        await sender.send_text(
            external_user_id,
            result.message,
            **_vk_kwargs(platform, kwargs, canonical_user_id, text=result.message),
        )
        # Do not auto-build/upload progress chart after every post-score.
        # VK photo upload is intentionally available for explicit progress requests,
        # but doing it automatically makes the chat feel slow.
    except (MessengerTransportError, UnsupportedMessengerDelivery, OSError):
        log.exception("%s post-score flow failed", platform.upper())
        await sender.send_text(
            external_user_id,
            "⚠️ Оценку после прослушивания сохранил, но не смог завершить цикл. Напишите: progress",
            **_vk_kwargs(platform, {}, canonical_user_id),
        )


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
                                {"command": _scoped_clientplatform_command(button.command, business_id=business_id)},
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
                rendered.append(
                    {"type": "link", "text": button.label, "url": link}
                )
            else:
                rendered.append(
                    {
                        "type": "callback",
                        "text": button.label,
                        "payload": _scoped_clientplatform_command(button.command, business_id=business_id),
                    }
                )
        rows.append(rendered)
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def _reply_requests_replay(reply: MessengerReply) -> tuple[bool, int | None]:
    meta = reply.meta or {}
    raw_replay = str(meta.get("replay") or "").strip().lower()
    requested = raw_replay in {"1", "true", "yes", "on"} or meta.get("replay_anchor") is not None
    anchor: int | None = None
    if meta.get("replay_anchor") is not None:
        try:
            anchor = int(str(meta.get("replay_anchor") or "").strip())
        except (TypeError, ValueError):
            anchor = None
    return requested, anchor


async def _send_privacy_export(
    *,
    platform: str,
    sender: Any,
    external_user_id: str,
    canonical_user_id: int,
) -> None:
    try:
        url = await asyncio.to_thread(
            issue_privacy_export_url,
            canonical_user_id,
            platform=platform,
        )
    except (RuntimeError, OSError, TypeError, ValueError):
        log.exception("%s one-time privacy export link failed", platform.upper())
        url = ""
    if not url:
        await sender.send_text(
            external_user_id,
            "⚠️ Безопасная выдача экспорта сейчас недоступна. Повторите позже или обратитесь в поддержку.",
            **_vk_kwargs(platform, {}, canonical_user_id),
        )
        return

    ttl = privacy_export_ttl_minutes()
    text = (
        "🔐 Одноразовая ссылка на экспорт Ваших данных:\n"
        f"{url}\n\n"
        f"Ссылка действует не более {ttl} минут и позволяет скачать архив один раз. "
        "Сначала откроется страница подтверждения; предпросмотр мессенджера не расходует ссылку. "
        "Архив сжат, но не зашифрован — храните его в защищённом месте."
    )
    await sender.send_text(
        external_user_id,
        text,
        **_vk_kwargs(platform, {}, canonical_user_id, text=text),
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
                kwargs = {
                    "keyboard_json": _vk_clientplatform_keyboard(
                        interaction,
                        button_links=button_links,
                        business_id=business_id,
                    )
                }
            elif platform == "max":
                kwargs = {
                    "attachments": _max_clientplatform_attachments(
                        interaction,
                        button_links=button_links,
                        business_id=business_id,
                    )
                }
            else:
                raise MessengerTransportError(
                    f"Unsupported ClientPlatform interaction platform: {platform}"
                )
            await sender.send_text(
                external_user_id,
                interaction.text,
                **_vk_kwargs(
                    platform,
                    kwargs,
                    canonical_user_id,
                    text=interaction.text,
                ),
            )
            continue

        if reply.kind == "text":
            text = _canonical_payment_text(platform, canonical_user_id, external_user_id, reply.text)
            if not str(text or "").strip():
                continue
            kwargs: dict[str, Any] = {}
            keyboard_kind = (reply.meta or {}).get("vk_keyboard") or (reply.meta or {}).get("keyboard")
            if platform == "vk":
                if keyboard_kind == "score_scale":
                    kwargs["keyboard_json"] = vk_score_scale_keyboard_json(
                        int((reply.meta or {}).get("session_id") or 0),
                        stage=str((reply.meta or {}).get("stage") or "pre"),
                    )
                elif keyboard_kind:
                    keyboard_json = keyboard_for_reply_kind(keyboard_kind, reply.meta or {})
                    if keyboard_json is not None:
                        kwargs["keyboard_json"] = keyboard_json
                elif _looks_like_score_scale(text):
                    kwargs["keyboard_json"] = vk_score_scale_keyboard_json()
            elif platform == "max":
                if keyboard_kind == "score_scale":
                    kwargs["attachments"] = [
                        max_ui.score_scale_attachment(
                            int((reply.meta or {}).get("session_id") or 0),
                            stage=str((reply.meta or {}).get("stage") or "pre"),
                        )
                    ]
                elif keyboard_kind:
                    attachment = max_ui.attachment_for_reply_kind(keyboard_kind, reply.meta or {})
                    if attachment is not None:
                        kwargs["attachments"] = [attachment]
            await sender.send_text(external_user_id, text, **_vk_kwargs(platform, kwargs, canonical_user_id, text=text))
            continue

        if reply.kind == "privacy_export":
            await _send_privacy_export(
                platform=platform,
                sender=sender,
                external_user_id=external_user_id,
                canonical_user_id=canonical_user_id,
            )
            continue

        if reply.kind == "next_audio":
            replay_requested, replay_anchor = _reply_requests_replay(reply)
            try:
                if replay_requested:
                    result = await send_replay_audio_to_user(
                        canonical_user_id,
                        senders=registry,
                        target_platform=platform,
                        fallback=platform,
                        anchor=replay_anchor,
                    )
                else:
                    result = await send_next_audio_to_user(
                        canonical_user_id,
                        senders=registry,
                        target_platform=platform,
                        fallback=platform,
                    )
                log.info(
                    "%s %s delivery result: user_id=%s transport=%s item=%s",
                    platform.upper(),
                    "replay_audio" if replay_requested else "next_audio",
                    canonical_user_id,
                    result.transport,
                    getattr(getattr(result, "item", None), "anchor", None),
                )
                if result.transport == "none":
                    await sender.send_text(external_user_id, result.message, **_vk_kwargs(platform, {}, canonical_user_id))
            except (MessengerTransportError, UnsupportedMessengerDelivery, OSError):
                log.exception("%s cross-channel audio delivery failed", platform.upper())
                await sender.send_text(
                    external_user_id,
                    "⚠️ Не удалось отправить следующее аудио в этот мессенджер. "
                    "Для MAX/ВКонтакте нужен публичный адрес MESSENGER_PUBLIC_BASE_URL, "
                    "чтобы бот мог присылать безопасную ссылку на следующий файл.",
                    **_vk_kwargs(platform, {}, canonical_user_id),
                )
            continue

        if reply.kind == "weather_show":
            txt = await get_weather_text_async(canonical_user_id, timeout_sec=2.0)
            await sender.send_text(
                external_user_id,
                txt + "\n\nМожно нажать «🏙 Изменить город» или отправить команду: город.",
                **_vk_kwargs(platform, {"keyboard_json": vk_weather_keyboard_json()} if platform == "vk" else {}, canonical_user_id),
            )
            continue

        if reply.kind == "weather_set_city":
            city = (reply.meta or {}).get("city", "").strip()
            txt = await asyncio.to_thread(set_city, canonical_user_id, city)
            await sender.send_text(
                external_user_id,
                txt,
                **_vk_kwargs(platform, {"keyboard_json": vk_weather_keyboard_json()} if platform == "vk" else {}, canonical_user_id),
            )
            continue

        if reply.kind == "progress_chart":
            await _send_progress_chart_or_notice(
                platform=platform,
                sender=sender,
                external_user_id=external_user_id,
                canonical_user_id=canonical_user_id,
            )
            continue

        if reply.kind in {"pre_score_result", "auto_pre_score"}:
            score = int((reply.meta or {}).get("score", "0") or 0)
            await _handle_pre_score_flow(
                platform=platform,
                sender=sender,
                registry=registry,
                external_user_id=external_user_id,
                canonical_user_id=canonical_user_id,
                score=score,
            )
            continue

        if reply.kind in {"post_score_result", "auto_post_score"}:
            score = int((reply.meta or {}).get("score", "0") or 0)
            await _handle_post_score_flow(
                platform=platform,
                sender=sender,
                registry=registry,
                external_user_id=external_user_id,
                canonical_user_id=canonical_user_id,
                score=score,
            )
            continue

        if reply.kind == "audio_confirmed_next":
            result = confirm_pending_audio_delivery(canonical_user_id, platform=platform)
            if result is None:
                await sender.send_text(
                    external_user_id,
                    "ℹ️ Сейчас нет аудио, ожидающего подтверждения.",
                    **_vk_kwargs(platform, {}, canonical_user_id),
                )
                continue
            await sender.send_text(
                external_user_id,
                f"✅ Подтвердил аудио №{result.anchor} — {result.title}.",
                **_vk_kwargs(platform, {}, canonical_user_id),
            )
            try:
                sent = await send_next_audio_to_user(
                    canonical_user_id,
                    senders=registry,
                    target_platform=platform,
                    fallback=platform,
                )
                if sent.transport == "none":
                    await sender.send_text(external_user_id, sent.message, **_vk_kwargs(platform, {}, canonical_user_id))
            except (MessengerTransportError, UnsupportedMessengerDelivery, OSError):
                log.exception("%s next audio after confirm failed", platform.upper())
                await sender.send_text(
                    external_user_id,
                    "⚠️ Подтверждение сохранено, но следующее аудио не отправилось. Напишите: continue",
                    **_vk_kwargs(platform, {}, canonical_user_id),
                )
            continue

        log_event(canonical_user_id, f"{platform}_unsupported_reply_kind", {"kind": reply.kind})
