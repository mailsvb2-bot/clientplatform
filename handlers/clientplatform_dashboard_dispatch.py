from __future__ import annotations

"""Keep the optimized owner resume path compatible with explicit dashboard overrides."""

from types import ModuleType

from aiogram.fsm.context import FSMContext
from aiogram.types import Message


def install_dynamic_dashboard_dispatch(control_module: ModuleType) -> None:
    """Honor a later `_send_dashboard` override without slowing the normal path.

    The production path keeps calling the optimized resume function directly.
    A test or extension that deliberately replaces `_send_dashboard` remains a
    supported seam and receives the resumed business after the FSM is cleared.
    """

    if bool(getattr(control_module, "_dynamic_dashboard_dispatch_installed", False)):
        return

    optimized_resume = control_module._resume_business
    optimized_dashboard = control_module._send_dashboard

    async def dispatch_resume(
        message: Message,
        *,
        user_id: int,
        business_id: str,
        state: FSMContext,
    ) -> None:
        current_dashboard = control_module._send_dashboard
        if current_dashboard is optimized_dashboard:
            await optimized_resume(
                message,
                user_id=user_id,
                business_id=business_id,
                state=state,
            )
            return

        await state.clear()
        await current_dashboard(
            message,
            user_id=user_id,
            business_id=business_id,
        )

    control_module._resume_business = dispatch_resume
    control_module._dynamic_dashboard_dispatch_installed = True


__all__ = ["install_dynamic_dashboard_dispatch"]
