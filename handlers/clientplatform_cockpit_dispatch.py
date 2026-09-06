from __future__ import annotations

from clientplatform.application.cockpit_action_routing import CockpitActionStartRoute

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


async def send_cockpit_action_route(
    target: ClientPlatformMessageTarget,
    *,
    user_id: int,
    route: CockpitActionStartRoute,
) -> None:
    """Dispatch a validated Cockpit route without duplicating Telegram surfaces."""

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
