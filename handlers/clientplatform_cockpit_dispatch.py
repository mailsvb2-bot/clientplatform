from __future__ import annotations

import asyncio

from clientplatform.application.cockpit_action_routing import CockpitActionStartRoute
from clientplatform.application.cockpit_events import resolve_cockpit_events
from clientplatform.application.native_member_interactions import render_native_member_interaction
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.connections import ConnectionPlatform

from . import clientplatform_one_click_experience as one_click
from . import clientplatform_creative_studio as creative_studio
from . import clientplatform_sales as sales
from . import clientplatform_sales_operations as sales_operations
from .clientplatform_message_target import ClientPlatformMessageTarget


async def send_cockpit_section(
    target: ClientPlatformMessageTarget,
    *,
    user_id: int,
    business_id: str,
    section: str,
) -> None:
    """Render one Cockpit section through the existing canonical Telegram UI."""

    if section == "creative":
        await creative_studio.send_creative_studio_menu(
            target,
            user_id=user_id,
            business_id=business_id,
        )
        return
    if section == "events":
        snapshot = await asyncio.to_thread(
            resolve_cockpit_events,
            telegram_user_id=user_id,
            requested_business_id=business_id,
            limit=5,
        )
        lines = ["🎥 Вебинары", ""]
        if snapshot.items:
            lines.append("Последние мероприятия:")
            for item in snapshot.items[:5]:
                revenue = ", ".join(row.display for row in item.revenue) or "—"
                lines.extend(
                    [
                        f"• {item.title} · {item.local_start}",
                        (
                            f"  регистрации {item.registered} · входы {item.join_clicked} · "
                            f"участие {item.attendance_confirmed} · оффер {item.offer_clicked} · "
                            f"оплаты {item.paid} · выручка {revenue}"
                        ),
                    ]
                )
        else:
            lines.append("Пока нет опубликованных мероприятий.")
        followups_enabled = bool(getattr(snapshot, "commercial_followups_enabled", False))
        followups_effective = bool(
            getattr(snapshot, "commercial_followups_effective", followups_enabled)
        )
        followups_available = bool(
            getattr(snapshot, "commercial_followups_platform_available", True)
        )
        followup_segments = set(
            getattr(
                snapshot,
                "commercial_followup_segments",
                ("no_show", "join_signal_unpaid", "attended_unpaid", "offer_clicked_unpaid"),
            )
        )
        followup_channels = set(
            getattr(snapshot, "commercial_followup_channels", ("email", "max", "vk"))
        )
        if followups_enabled and not followups_effective:
            followup_status = "Автоматические сообщения после мероприятия: 🟡 ВКЛ, временно приостановлены"
        elif followups_enabled:
            followup_status = "Автоматические сообщения после мероприятия: 🟢 ВКЛ"
        else:
            followup_status = "Автоматические сообщения после мероприятия: ⚪️ ВЫКЛ"
        lines.extend(["", followup_status])
        segment_labels = (
            ("no_show", "Зарегистрировались, но не пришли"),
            ("join_signal_unpaid", "Перешли к эфиру, участие не подтверждено"),
            ("attended_unpaid", "Были на вебинаре, но не купили"),
            ("offer_clicked_unpaid", "Открыли предложение, но не купили"),
        )
        lines.append("Кому писать:")
        lines.extend(
            f"{'✅' if key in followup_segments else '▫️'} {label}"
            for key, label in segment_labels
        )
        channel_labels = (("email", "Email"), ("max", "MAX"), ("vk", "VK"))
        lines.append(
            "Каналы: "
            + " · ".join(
                f"{'✅' if key in followup_channels else '▫️'} {label}"
                for key, label in channel_labels
            )
        )
        if not followups_available:
            if followups_enabled:
                lines.append(
                    "Платформа временно остановила отправку; настройка бизнеса сохранена. "
                    "Вы можете выключить её сейчас, чтобы сообщения не возобновились автоматически."
                )
            else:
                lines.append("Автосерия временно отключена на уровне платформы.")
        if snapshot.limitations:
            lines.extend(["", *snapshot.limitations])
        token = one_click.control._uuid_token(business_id)
        rows: list[list[tuple[str, str]]] = []
        if snapshot.can_manage:
            rows.append([("🎥 Создать вебинар", f"cpev:new:{token}")])
            if followups_enabled:
                rows.append([("🔴 Выключить автосообщения", f"cpev:followups:off:{token}")])
            elif bool(getattr(snapshot, "can_enable_commercial_followups", False)):
                rows.append([("🟢 Включить автосообщения", f"cpev:followups:on:{token}")])
            can_expand = bool(getattr(snapshot, "can_expand_commercial_followups", False))
            for key, label in segment_labels:
                active = key in followup_segments
                if active or can_expand:
                    rows.append([
                        (
                            f"{'✅' if active else '▫️'} {label}",
                            f"cpev:seg:{key}:{'off' if active else 'on'}:{token}",
                        )
                    ])
            channel_row = []
            for key, label in channel_labels:
                active = key in followup_channels
                if active or can_expand:
                    channel_row.append((
                        f"{'✅' if active else '▫️'} {label}",
                        f"cpev:ch:{key}:{'off' if active else 'on'}:{token}",
                    ))
            if channel_row:
                rows.append(channel_row)
        rows.append([("📈 К росту", f"cpo:content:{token}")])
        rows.append([("🏠 В кабинет", f"cpj:home:{token}")])
        await target.answer(
            "\n".join(lines),
            reply_markup=one_click.control._keyboard(rows),
        )
        return
    await one_click.send_one_click_section(
        target,
        user_id=user_id,
        business_id=business_id,
        section=section,
    )


async def _send_canonical_native_interaction(
    target: ClientPlatformMessageTarget,
    *,
    user_id: int,
    business_id: str,
    raw_text: str,
    interaction_key: str,
) -> None:
    actor = resolve_tenant_context(user_id=user_id, business_id=business_id)
    interaction = render_native_member_interaction(
        actor=actor,
        raw_text=raw_text,
        interaction_key=f"cockpit:{business_id}:{interaction_key}",
        current_platform=ConnectionPlatform.TELEGRAM,
    )
    rows = [
        [(button.label, button.command) for button in row]
        for row in interaction.rows
    ]
    await target.answer(
        interaction.text,
        reply_markup=one_click.control._keyboard(rows),
    )


async def send_cockpit_action_route(
    target: ClientPlatformMessageTarget,
    *,
    user_id: int,
    route: CockpitActionStartRoute,
) -> None:
    """Dispatch a validated Cockpit route without duplicating Telegram surfaces."""

    if route.section == "reactivation" or route.kind == "r":
        await _send_canonical_native_interaction(
            target,
            user_id=user_id,
            business_id=route.business_id,
            raw_text="cpm:reactivate",
            interaction_key="reactivation",
        )
        return
    if route.section == "ad-spend" or route.kind == "d":
        await _send_canonical_native_interaction(
            target,
            user_id=user_id,
            business_id=route.business_id,
            raw_text="cpm:ad-spend",
            interaction_key="ad-spend",
        )
        return
    if route.section is not None:
        await send_cockpit_section(
            target,
            user_id=user_id,
            business_id=route.business_id,
            section=route.section,
        )
        return
    if route.kind == "h":
        await sales.send_sales_handoff_view(
            target, user_id=user_id, business_id=route.business_id
        )
        return
    if route.kind == "w":
        await sales.send_sales_work_view(
            target, user_id=user_id, business_id=route.business_id
        )
        return
    if route.kind == "l" and route.lead_id is not None:
        await sales_operations.send_sales_lead_view(
            target,
            user_id=user_id,
            business_id=route.business_id,
            lead_id=route.lead_id,
        )
        return
    raise ValueError("unsupported cockpit action route")


__all__ = ["send_cockpit_action_route", "send_cockpit_section"]
