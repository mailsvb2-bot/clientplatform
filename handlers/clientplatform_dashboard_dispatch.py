from __future__ import annotations

"""Keep the optimized owner resume path compatible with dashboard overrides."""

import asyncio
from types import ModuleType

from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from clientplatform.domain.activity import BusinessProfileStatus


def install_dynamic_dashboard_dispatch(control_module: ModuleType) -> None:
    """Route DRAFT onboarding explicitly while preserving READY safety behavior.

    The interaction-safety layer owns command-like-name repair and the optimized
    READY dashboard. U-007 owns the DRAFT confirmation/first-result lifecycle.
    The two boundaries are composed by state, not by relying on import/install
    order or a previously captured resume callable.
    """

    if bool(getattr(control_module, "_dynamic_dashboard_dispatch_installed", False)):
        return

    # Interaction safety is installed before optional lesson/media and managed
    # bot lifecycle routers. Compose their callback namespaces into the same
    # state/navigation policy before any user interaction can be dispatched.
    from . import clientplatform_button_surface_contract as button_surface_contract
    from . import clientplatform_interaction_safety as interaction_safety

    button_surface_contract.install_button_surface_contract(interaction_safety)

    guarded_resume = control_module._resume_business

    async def dispatch_resume(
        message: Message,
        *,
        user_id: int,
        business_id: str,
        state: FSMContext,
    ) -> None:
        actor = await control_module._actor(user_id, business_id)
        try:
            profile = await asyncio.to_thread(
                control_module.get_business_profile,
                actor=actor,
            )
        except control_module.ActivityNotFound:
            await guarded_resume(
                message,
                user_id=user_id,
                business_id=business_id,
                state=state,
            )
            return

        if getattr(profile, "status", BusinessProfileStatus.READY) == BusinessProfileStatus.DRAFT:
            accesses = await asyncio.to_thread(
                control_module.list_accessible_businesses,
                user_id=user_id,
            )
            access = next(
                (
                    item
                    for item in accesses
                    if str(item.business.id) == str(business_id)
                ),
                None,
            )
            if access is not None and interaction_safety._command_like(str(access.business.name)):
                await guarded_resume(
                    message,
                    user_id=user_id,
                    business_id=business_id,
                    state=state,
                )
                return

            await state.clear()
            structured = await asyncio.to_thread(
                control_module.get_business_profile_details,
                actor=actor,
            )
            if structured.confirmed:
                await control_module._send_onboarding_first_result(
                    message,
                    business_id=business_id,
                )
            else:
                await control_module._send_onboarding_review(
                    message,
                    actor=actor,
                    business_id=business_id,
                )
            return

        await guarded_resume(
            message,
            user_id=user_id,
            business_id=business_id,
            state=state,
        )

    control_module._resume_business = dispatch_resume
    control_module._dynamic_dashboard_dispatch_installed = True


__all__ = ["install_dynamic_dashboard_dispatch"]
