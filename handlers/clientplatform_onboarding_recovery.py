from __future__ import annotations

"""Recover an incomplete owner onboarding step when volatile FSM state is lost."""

import asyncio
import importlib

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from clientplatform.application.activity import get_business_profile
from clientplatform.application.tenancy import list_accessible_businesses
from clientplatform.domain.activity import ActivityNotFound

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_onboarding_recovery")
router.message.filter(control.ClientPlatformControlEnabled())


class IncompleteActivityDescriptionFilter(BaseFilter):
    """Match only the durable onboarding step that can be reconstructed safely.

    The owner/business relationship and the missing profile are persisted in the
    database. A plain-text answer can therefore continue after an Aiogram FSM
    reset without guessing a tenant or hijacking unrelated messages.
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

        business_id = str(accesses[0].business.id)
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
