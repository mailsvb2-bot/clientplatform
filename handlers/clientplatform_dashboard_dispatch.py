from __future__ import annotations

"""Keep the optimized owner resume path compatible with dashboard overrides."""

from types import ModuleType

from aiogram.fsm.context import FSMContext
from aiogram.types import Message


def install_dynamic_dashboard_dispatch(control_module: ModuleType) -> None:
    """Preserve profile onboarding before rendering the current dashboard.

    The original resume function validates that the business profile exists,
    restores the activity-description step when it does not, clears the FSM for
    ready businesses and then resolves ``_send_dashboard`` dynamically from the
    control module. A later simple or test dashboard override is therefore
    honored without bypassing the required onboarding guard.
    """

    if bool(getattr(control_module, "_dynamic_dashboard_dispatch_installed", False)):
        return

    guarded_resume = control_module._resume_business

    async def dispatch_resume(
        message: Message,
        *,
        user_id: int,
        business_id: str,
        state: FSMContext,
    ) -> None:
        await guarded_resume(
            message,
            user_id=user_id,
            business_id=business_id,
            state=state,
        )

    control_module._resume_business = dispatch_resume
    control_module._dynamic_dashboard_dispatch_installed = True


__all__ = ["install_dynamic_dashboard_dispatch"]
