from __future__ import annotations

"""Recover an incomplete owner onboarding step when volatile FSM state is lost."""

import asyncio
import importlib

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from clientplatform.application.activity import get_business_profile
from clientplatform.application.bookings import list_customer_businesses
from clientplatform.application.tenancy import list_accessible_businesses
from clientplatform.domain.activity import ActivityNotFound
from clientplatform.domain.tenancy import PlatformRole

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_onboarding_recovery")
router.message.filter(control.ClientPlatformControlEnabled())


class IncompleteActivityDescriptionFilter(BaseFilter):
    """Match only an unambiguous, write-authorized owner onboarding recovery.

    A missing profile is durable evidence that onboarding is incomplete, but a
    plain text message is not evidence that the user is currently in the owner
    workspace. Recovery therefore fails closed for dual-role customer accounts
    and for staff roles that cannot manage the business profile. The user can
    always use /start to select the intended workspace explicitly.
    """

    async def __call__(
        self,
        message: Message,
        state: FSMContext,
    ) -> bool | dict[str, str]:
        text = str(message.text or "").strip()
        if not text or text.startswith("/"):
            return False

        current_state = await state.get_state()
        expected_state = control.ClientPlatformControlState.activity_description.state
        if current_state not in {None, expected_state}:
            return False

        if current_state == expected_state:
            data = await state.get_data()
            if str(data.get("business_id") or "").strip():
                return False

        user_id = control._user_id(message)
        accesses = await asyncio.to_thread(
            list_accessible_businesses,
            user_id=user_id,
        )
        if len(accesses) != 1:
            return False

        access = accesses[0]
        if access.membership.role not in {
            PlatformRole.OWNER,
            PlatformRole.ADMINISTRATOR,
        }:
            return False

        # A member may simultaneously be a customer of another specialist.
        # With no surviving FSM/workspace marker, consuming arbitrary text as an
        # owner-profile answer would steal the customer's message. Do not guess.
        customer_links = await asyncio.to_thread(
            list_customer_businesses,
            telegram_user_id=user_id,
        )
        if customer_links:
            return False

        business_id = str(access.business.id)
        actor = await control._actor(user_id, business_id)
        try:
            await asyncio.to_thread(get_business_profile, actor=actor)
        except ActivityNotFound:
            return {"recovered_business_id": business_id}
        return False


@router.message(F.text, IncompleteActivityDescriptionFilter())
async def recover_activity_description(
    message: Message,
    state: FSMContext,
    recovered_business_id: str,
) -> None:
    """Recreate the exact FSM context and continue through the canonical handler."""

    await state.set_state(control.ClientPlatformControlState.activity_description)
    await state.update_data(
        business_id=recovered_business_id,
        editing_activity=False,
    )
    await control.receive_activity_description(message, state)


__all__ = [
    "IncompleteActivityDescriptionFilter",
    "recover_activity_description",
    "router",
]
