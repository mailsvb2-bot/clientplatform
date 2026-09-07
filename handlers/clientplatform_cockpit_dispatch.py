from __future__ import annotations

from clientplatform.application.cockpit_action_routing import CockpitActionStartRoute
from clientplatform.application.native_member_interactions import render_native_member_interaction
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.connections import ConnectionPlatform

from . import clientplatform_one_click_experience as one_click
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

    await one_click.send_one_click_section(
        target,
        user_id=user_id,
        business_id=business_id,
        section=section,
    )


async def _send_canonical_reactivation(
    target: ClientPlatformMessageTarget,
    *,
    user_id: int,
    business_id: str,
) -> None:
    actor = resolve_tenant_context(user_id=user_id, business_id=business_id)
    interaction = render_native_member_interaction(
        actor=actor,
        raw_text="cpm:reactivate",
        interaction_key=f"cockpit:{business_id}:reactivation",
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

    if route.section == "reactivation":
        await _send_canonical_reactivation(
            target,
            user_id=user_id,
            business_id=route.business_id,
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
