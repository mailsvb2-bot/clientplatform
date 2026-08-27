from __future__ import annotations

"""Result-first owner dashboard with one primary action and progressive disclosure."""

import asyncio

from aiogram.types import Message

from clientplatform.application.growth_cockpit import GrowthAction, get_growth_cockpit
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.tenancy import TenantAccessDenied, TenantPermissionDenied

from . import clientplatform_control as control
from . import clientplatform_goal_first_safety as goal_contract
from . import clientplatform_growth as growth
from . import clientplatform_one_click_experience as one_click

# This module is imported by the single canonical handler composition path in
# handlers.__init__. Compose the Growth Cockpit there as a child of the existing
# simple owner experience instead of registering a second top-level bot brain.
if not bool(getattr(one_click.simple, "_growth_cockpit_composed", False)):
    one_click.simple.router.include_router(growth.router)
    one_click.simple._growth_cockpit_composed = True


def _without_advertising(**_kwargs):
    return None


def _owner_next_action(actor) -> GrowthAction | None:
    try:
        return get_growth_cockpit(
            actor=actor,
            period_days=7,
            advertising_loader=_without_advertising,
        ).next_action
    except (TenantAccessDenied, TenantPermissionDenied, ValueError):
        return None
    except OSError:
        return None
    except RuntimeError:
        return None


def _primary_action(business_id: str, next_action: GrowthAction | None = None) -> tuple[str, str]:
    token = control._uuid_token(business_id)
    if next_action is not None:
        if next_action.action_key == "sales_handoff":
            return "🙋 Ответить клиентам", f"cps:sh:{token}"
        if next_action.action_key.startswith("sales_plan:"):
            return "💬 Продолжить работу с клиентом", f"cps:sw:{token}"
        if next_action.action_key == "attribution_review":
            return "💰 Проверить источники оплат", f"cpy:a:{token}:7"
    action = goal_contract.ACQUIRE_CLIENTS
    return action.label, action.callback(token)


def _goal_keyboard(business_id: str, next_action: GrowthAction | None = None):
    """Show one result-first action and progressively disclose everything else."""

    token = control._uuid_token(business_id)
    primary = _primary_action(business_id, next_action)
    return control._keyboard(
        [
            [primary],
            [("⋯ Все возможности", f"cpo:more:{token}")],
        ]
    )


async def send_goal_dashboard(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    actor, access, profile, _capabilities, customers, programs, slots = (
        await one_click.simple._business_snapshot(
            user_id=user_id,
            business_id=business_id,
        )
    )
    next_action = await asyncio.to_thread(_owner_next_action, actor)
    open_slots = [item for item in slots if item.slot.status == BookingSlotStatus.OPEN]
    primary_label, _primary_callback = _primary_action(business_id, next_action)

    if next_action is None or next_action.action_key == "none":
        main_title = "Можно заняться ростом бизнеса"
        main_reason = "Срочных задач сейчас нет — ClientPlatform подготовит следующий шаг для привлечения клиентов."
    else:
        main_title = next_action.title
        main_reason = next_action.reason

    await message.answer(
        f"🏠 {access.business.name}\n\n"
        f"{profile.activity_description}\n\n"
        "Главное сейчас\n"
        f"• {main_title}\n"
        f"  {main_reason}\n\n"
        f"Клиентов: {len(customers)} · свободных времён: {len(open_slots)} · "
        f"материалов и программ: {len(programs)}\n\n"
        f"Нажмите «{primary_label}» — открою нужный шаг. "
        "Остальные функции сохранены в «Все возможности».",
        reply_markup=_goal_keyboard(business_id, next_action),
    )


__all__ = ["_goal_keyboard", "send_goal_dashboard"]
